"""停机后安全重启：最新配置、无残留状态、等待方只见完整结果。"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigValidationError,
    FieldSpec,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
)
from fastapi_assembly_kernel.config import FileSource


def make_kernel(config_path, factories=None, teardowns=None):
    schema = ConfigSchema((FieldSpec("greeting", str, default="hello"),))
    sources = [
        MappingSource({}, name="defaults", priority=0),
        FileSource(config_path, priority=10),
    ]
    loader = LayeredConfigLoader(schema, sources)
    registry = PluginRegistry()
    factories = factories or {}
    teardowns = teardowns or {}
    for name, factory in factories.items():
        registry.register(PluginSpec(name, factory, teardown=teardowns.get(name)))
    return AssemblyKernel(config_loader=loader, registry=registry)


def test_restart_uses_latest_config(tmp_path, case_log):
    cfg = tmp_path / "app.json"
    cfg.write_text(json.dumps({"greeting": "v1"}), encoding="utf-8")
    kernel = make_kernel(cfg)
    case_log("重启使用最新配置", {"第一轮": "v1", "停机后修改文件": "v2"},
             "重启重新走一遍分层加载，拿到当前最新配置")
    kernel.start()
    assert kernel.config.get("greeting") == "v1"
    kernel.stop()

    cfg.write_text(json.dumps({"greeting": "v2"}), encoding="utf-8")
    kernel.start()
    assert kernel.config.get("greeting") == "v2"


def test_restart_leaves_no_leftover_state(tmp_path, case_log):
    case_log("重启 无残留", "第一轮装配的实例在停机时被释放",
             "重启后全部实例重建（新对象），旧实例被 teardown 且不再可达")
    cfg = tmp_path / "app.json"
    cfg.write_text("{}", encoding="utf-8")
    torn_down: list[object] = []

    def factory(ctx):
        return object()

    def teardown(instance):
        torn_down.append(instance)

    kernel = make_kernel(cfg, factories={"only": factory}, teardowns={"only": teardown})
    kernel.start()
    first = kernel.get("only")
    kernel.stop()
    assert torn_down == [first]

    kernel.start()
    second = kernel.get("only")
    assert second is not first
    assert torn_down == [first]  # 旧实例只被释放过一次
    kernel.stop()
    assert torn_down == [first, second]


def test_callers_waiting_during_restart_get_full_state(tmp_path, case_log):
    threads = 8
    case_log(
        "重启期间等待的调用方",
        f"停机后 {threads} 个线程并发 get()，装配被人为放慢",
        "等待方要么拿到重启后完整可用的状态，要么收到失败；"
        "所有调用方拿到同一个新实例，装配只执行一次",
    )
    cfg = tmp_path / "app.json"
    cfg.write_text("{}", encoding="utf-8")
    build_count = {"n": 0}
    lock = threading.Lock()

    def slow_factory(ctx):
        with lock:
            build_count["n"] += 1
        time.sleep(0.05)
        return object()

    kernel = make_kernel(cfg, factories={"only": slow_factory})
    kernel.start()
    first = kernel.get("only")
    kernel.stop()
    assert not kernel.is_started()

    barrier = threading.Barrier(threads)
    results: list[object] = []

    def worker():
        barrier.wait()
        results.append(kernel.get("only"))

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda _: worker(), range(threads)))

    assert build_count["n"] == 2  # 第一轮 + 重启各一次
    restarted = results[0]
    assert all(r is restarted for r in results)
    assert restarted is not first
    assert kernel.is_started()


def test_failed_restart_broadcasts_failure_and_recovers(tmp_path, case_log):
    case_log(
        "重启失败广播与恢复",
        "停机后把配置改坏，多个线程并发 start()；之后修复配置再启动",
        "失败轮所有调用方收到同一个可区分错误；修复后从头完整重试，"
        "不残留半成品引用",
    )
    cfg = tmp_path / "app.json"
    cfg.write_text(json.dumps({"greeting": "ok"}), encoding="utf-8")
    builds: list[str] = []

    def factory(ctx):
        builds.append(ctx.config.get("greeting"))
        return object()

    # 额外的慢速来源：拉长每轮启动窗口，保证并发调用方进入同一轮
    class SlowSource(MappingSource):
        def load(self):
            time.sleep(0.05)
            return super().load()

    schema = ConfigSchema((FieldSpec("greeting", str, default="hello"),))
    loader = LayeredConfigLoader(
        schema,
        [
            MappingSource({}, name="defaults", priority=0),
            SlowSource({}, name="slow", priority=5),
            FileSource(cfg, priority=10),
        ],
    )
    registry = PluginRegistry()
    registry.register(PluginSpec("only", factory))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()
    kernel.stop()

    # 配置改坏 -> 重启失败（用慢速来源保证 4 个线程都进入同一轮启动）
    cfg.write_text(json.dumps({"greeting": 123}), encoding="utf-8")
    errors: list[BaseException] = []
    entered = threading.Barrier(4)

    def worker():
        entered.wait()
        try:
            kernel.start()
        except ConfigValidationError as exc:
            errors.append(exc)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: worker(), range(4)))

    assert len(errors) == 4
    assert all(e is errors[0] for e in errors)
    assert not kernel.is_started()

    # 修复配置 -> 从头完整重试成功
    cfg.write_text(json.dumps({"greeting": "recovered"}), encoding="utf-8")
    kernel.start()
    assert kernel.config.get("greeting") == "recovered"
    assert builds == ["ok", "recovered"]  # 失败轮未构建任何插件
