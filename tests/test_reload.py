"""运行期配置热重载：整体切换、失败保持原配置、并发快照一致性。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigSourceFormatError,
    ConfigSourceMissingError,
    ConfigValidationError,
    FieldSpec,
    KernelNotRunningError,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
)
from fastapi_assembly_kernel.config import FileSource


def make_kernel(config_path, extra_fields=(), defaults=None, required_file=False):
    schema = ConfigSchema(
        (
            FieldSpec("host", str, default="localhost"),
            FieldSpec("port", int, default=8000),
            *extra_fields,
        )
    )
    sources = [
        MappingSource(defaults or {}, name="defaults", priority=0),
        FileSource(config_path, priority=10, required=required_file),
    ]
    loader = LayeredConfigLoader(schema, sources)
    return AssemblyKernel(config_loader=loader, registry=PluginRegistry())


def write_config(path, **values):
    path.write_text(json.dumps(values), encoding="utf-8")


def test_reload_success_swaps_whole_config(tmp_path, case_log):
    cfg = tmp_path / "app.json"
    write_config(cfg, host="a.example", port=9001)
    kernel = make_kernel(cfg)
    case_log("热重载成功", {"v1": {"host": "a.example", "port": 9001},
                          "v2": {"host": "b.example", "port": 9002}},
             "全部来源解析校验通过 -> 整体切换；旧快照对象不受影响")
    kernel.start()
    before = kernel.config
    assert before.get("host") == "a.example"

    write_config(cfg, host="b.example", port=9002)
    returned = kernel.reload_config()

    after = kernel.config
    assert after.get("host") == "b.example"
    assert after.get("port") == 9002
    assert returned is after
    # 已发布的旧快照不可变，仍是完整旧的一份
    assert before.get("host") == "a.example"
    assert before.get("port") == 9001


def test_reload_failure_keeps_current_config(tmp_path, case_log):
    case_log("热重载失败保持原配置",
             "依次注入：必需来源缺失 / 非法 JSON / 类型不匹配",
             "每次失败当前配置原封不动（同一对象），错误类型可区分")
    cfg = tmp_path / "app.json"
    write_config(cfg, host="stable.example", port=9001)
    kernel = make_kernel(cfg, required_file=True)
    kernel.start()
    current = kernel.config

    # 1) 必需来源缺失
    cfg.unlink()
    with pytest.raises(ConfigSourceMissingError):
        kernel.reload_config()
    assert kernel.config is current

    # 2) 文件内容非法
    cfg.write_text("{ not json !!!", encoding="utf-8")
    with pytest.raises(ConfigSourceFormatError):
        kernel.reload_config()
    assert kernel.config is current

    # 3) 字段类型不匹配
    write_config(cfg, host="b.example", port="not-an-int")
    with pytest.raises(ConfigValidationError):
        kernel.reload_config()
    assert kernel.config is current
    assert kernel.config.get("host") == "stable.example"
    assert kernel.config.get("port") == 9001


def test_reload_error_never_leaks_secret(tmp_path, case_log, caplog):
    secret_value = "s3cr3t-token-xyz"
    case_log("重载报错不泄漏敏感值", {"api_key": "类型错误的敏感值"},
             "异常信息与日志只含字段名/来源名，绝不出现字段值")
    cfg = tmp_path / "app.json"
    write_config(cfg, api_key=secret_value)
    kernel = make_kernel(
        cfg, extra_fields=(FieldSpec("api_key", int, required=True, secret=True),)
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        kernel.start()  # 首次启动即因 secret 字段类型不匹配失败
    assert secret_value not in str(exc_info.value)

    # 运行期重载失败同样不泄漏
    kernel2 = make_kernel(
        cfg, extra_fields=(FieldSpec("api_key", str, required=True, secret=True),)
    )
    write_config(cfg, api_key=secret_value)
    kernel2.start()
    write_config(cfg, api_key=12345)  # str 字段收到 int -> 类型不匹配
    with caplog.at_level("DEBUG"):
        with pytest.raises(ConfigValidationError) as exc_info2:
            kernel2.reload_config()
    assert secret_value not in str(exc_info2.value)
    assert secret_value not in caplog.text
    assert kernel2.config.get("api_key") == secret_value  # 原配置原封不动


def test_reload_requires_running_kernel(tmp_path, case_log):
    case_log("未运行不可重载", "未 start / 已 stop 的内核调用 reload_config()",
             "抛 KernelNotRunningError（可区分），不触发隐式装配")
    cfg = tmp_path / "app.json"
    write_config(cfg, host="a.example")
    kernel = make_kernel(cfg)
    with pytest.raises(KernelNotRunningError):
        kernel.reload_config()
    assert not kernel.is_started()
    kernel.start()
    kernel.stop()
    with pytest.raises(KernelNotRunningError):
        kernel.reload_config()


def test_concurrent_read_write_snapshot_consistency(tmp_path, case_log):
    readers, rounds = 8, 30
    case_log(
        "并发读写快照一致性",
        f"{readers} 个读线程循环读配置，写线程热重载 {rounds} 轮",
        "任何一次读取要么看到切换前完整一份，要么看到切换后完整一份；"
        "host/port 必须来自同一代配置，不得出现半新半旧或空配置",
    )
    cfg = tmp_path / "app.json"
    write_config(cfg, host="host-0", port=8000)
    kernel = make_kernel(cfg)
    kernel.start()

    violations: list[str] = []
    stop_flag = threading.Event()

    def reader():
        while not stop_flag.is_set():
            snap = kernel.config
            host, port = snap.get("host"), snap.get("port")
            if host is None or port is None:
                violations.append(f"空配置: {host=} {port=}")
                continue
            # host 与 port 必须属于同一代：host-i 必须搭配 8000+i
            if port != 8000 + int(host.split("-")[1]):
                violations.append(f"半新半旧: {host=} {port=}")

    def writer():
        for i in range(1, rounds + 1):
            write_config(cfg, host=f"host-{i}", port=8000 + i)
            kernel.reload_config()

    with ThreadPoolExecutor(readers + 1) as pool:
        futures = [pool.submit(reader) for _ in range(readers)]
        w = pool.submit(writer)
        w.result()
        stop_flag.set()
        for f in futures:
            f.result()

    assert violations == []
    assert kernel.config.get("host") == f"host-{rounds}"
    assert kernel.config.get("port") == 8000 + rounds
