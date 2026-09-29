"""运行期配置热重载：整体切换、失败保持原样、并发快照一致性、敏感值保护。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigSource,
    ConfigSourceFormatError,
    ConfigSourceMissingError,
    ConfigValidationError,
    FieldSpec,
    FileSource,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
)


class MutableSource(ConfigSource):
    """可在运行期改变内容的测试来源。"""

    def __init__(self, mapping, *, name, priority, required=False):
        super().__init__(name=name, priority=priority, required=required)
        self._mapping = dict(mapping)

    def set(self, mapping):
        self._mapping = dict(mapping)

    def load(self):
        if self.required and not self._mapping:
            raise ConfigSourceMissingError(f"必需配置来源 {self.name!r} 为空")
        return dict(self._mapping)


SCHEMA = ConfigSchema(
    (
        FieldSpec("host", str, default="localhost"),
        FieldSpec("port", int, default=80),
        FieldSpec("token", str, default="default-token", secret=True),
    )
)


def make_kernel(source):
    loader = LayeredConfigLoader(SCHEMA, [source])
    registry = PluginRegistry()
    registry.register(PluginSpec("marker", lambda ctx: object()))
    return AssemblyKernel(config_loader=loader, registry=registry)


def test_reload_success_switches_whole_snapshot(case_log):
    source = MutableSource(
        {"host": "a.example", "port": 8080, "token": "tok-1"},
        name="mutable", priority=0,
    )
    kernel = make_kernel(source)
    kernel.start()
    before = kernel.config
    case_log("热重载成功",
             {"旧": {"host": "a.example", "port": 8080},
              "新": {"host": "b.example", "port": 9090}},
             "重载后读到完整新快照；重载前拿到的旧快照对象保持不变")

    source.set({"host": "b.example", "port": 9090, "token": "tok-2"})
    new_config = kernel.reload_config()

    assert kernel.config is new_config
    assert new_config.get("host") == "b.example"
    assert new_config.get("port") == 9090
    # 旧快照不被篡改
    assert before.get("host") == "a.example"
    assert before.get("port") == 8080


def test_reload_type_mismatch_keeps_current_config(case_log):
    source = MutableSource({"port": 8080}, name="mutable", priority=0)
    kernel = make_kernel(source)
    kernel.start()
    before = kernel.config
    case_log("热重载类型不匹配", {"port": "not-an-int"},
             "抛 ConfigValidationError（可区分），当前配置原封不动")

    source.set({"port": "not-an-int"})
    with pytest.raises(ConfigValidationError):
        kernel.reload_config()

    assert kernel.config is before
    assert kernel.config.get("port") == 8080


def test_reload_missing_required_source_keeps_current_config(case_log):
    source = MutableSource(
        {"host": "a.example"}, name="mutable", priority=0, required=True
    )
    kernel = make_kernel(source)
    kernel.start()
    before = kernel.config
    case_log("热重载必需来源缺失", "required 来源被清空",
             "抛 ConfigSourceMissingError（可区分），当前配置原封不动")

    source.set({})
    with pytest.raises(ConfigSourceMissingError):
        kernel.reload_config()

    assert kernel.config is before


def test_reload_malformed_file_keeps_current_config(case_log, tmp_path):
    conf = tmp_path / "app.json"
    conf.write_text('{"host": "a.example", "port": 8080}', encoding="utf-8")
    source = FileSource(conf, priority=0, required=True)
    kernel = make_kernel(source)
    kernel.start()
    before = kernel.config
    case_log("热重载格式非法", "配置文件被改写为非法 JSON",
             "抛 ConfigSourceFormatError（可区分），当前配置原封不动")

    conf.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigSourceFormatError):
        kernel.reload_config()

    assert kernel.config is before
    assert kernel.config.get("host") == "a.example"


def test_reload_error_never_leaks_secret_values(case_log, caplog):
    source = MutableSource(
        {"token": "super-secret-value"}, name="mutable", priority=0
    )
    kernel = make_kernel(source)
    kernel.start()
    case_log("敏感值保护", {"token": "super-secret-value", "port": "bad"},
             "重载失败的异常信息与日志中不得出现任何敏感值")

    source.set({"token": "another-secret", "port": "bad"})
    with pytest.raises(ConfigValidationError) as exc_info:
        kernel.reload_config()

    exposed = str(exc_info.value) + "".join(
        r.getMessage() for r in caplog.records
    )
    assert "super-secret-value" not in exposed
    assert "another-secret" not in exposed
    assert "port" in str(exc_info.value)  # 字段名可以出现


def test_concurrent_readers_see_consistent_snapshots(case_log):
    readers, rounds = 8, 50
    case_log("并发读写快照一致",
             f"1 个写线程做 {rounds} 轮热重载，{readers} 个读线程持续读取",
             "每次读到的 host/port 必须来自同一轮快照（配对一致），"
             "不得出现半新半旧、空配置或退回默认值")
    source = MutableSource({"host": "h-0", "port": 0}, name="mutable", priority=0)
    kernel = make_kernel(source)
    kernel.start()

    stop = threading.Event()
    violations: list[str] = []

    def reader():
        while not stop.is_set():
            snap = kernel.config
            host, port = snap.get("host"), snap.get("port")
            if host != f"h-{port}":
                violations.append(f"host={host!r} port={port!r}")

    def writer():
        for i in range(1, rounds + 1):
            source.set({"host": f"h-{i}", "port": i})
            kernel.reload_config()
        stop.set()

    with ThreadPoolExecutor(readers + 1) as pool:
        futures = [pool.submit(reader) for _ in range(readers)]
        futures.append(pool.submit(writer))
        for f in futures:
            f.result()

    assert violations == []
    assert kernel.config.get("port") == rounds


def test_reload_priority_rules_still_apply(case_log):
    low = MutableSource({"host": "low", "port": 1}, name="low", priority=0)
    high = MutableSource({"port": 2}, name="high", priority=10)
    loader = LayeredConfigLoader(SCHEMA, [low, high])
    kernel = AssemblyKernel(
        config_loader=loader, registry=PluginRegistry()
    )
    kernel.start()
    case_log("重载保持来源优先级",
             {"低优先级": {"host": "low", "port": 1}, "高优先级": {"port": 2}},
             "重载后高优先级来源仍覆盖低优先级；未提供的字段回落默认值")

    low.set({"host": "low-2", "port": 100})
    high.set({"port": 200})
    kernel.reload_config()

    assert kernel.config.get("host") == "low-2"
    assert kernel.config.get("port") == 200
    assert kernel.config.get("token") == "default-token"  # schema 默认值
