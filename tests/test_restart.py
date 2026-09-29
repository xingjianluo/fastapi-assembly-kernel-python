"""停机后安全重启：最新配置、无残留状态、并发调用方只见完整状态。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigSource,
    ConfigValidationError,
    FieldSpec,
    LayeredConfigLoader,
    PluginRegistry,
    PluginSpec,
    ShutdownError,
)


class MutableSource(ConfigSource):
    """可在运行期改变内容的测试来源。"""

    def __init__(self, mapping, *, name="mutable", priority=0):
        super().__init__(name=name, priority=priority)
        self._mapping = dict(mapping)

    def set(self, mapping):
        self._mapping = dict(mapping)

    def load(self):
        return dict(self._mapping)


SCHEMA = ConfigSchema((FieldSpec("greeting", str, default="hello"),))


def make_kernel(source, factories=None, edges=None):
    loader = LayeredConfigLoader(SCHEMA, [source])
    registry = PluginRegistry()
    edges = edges or {"only": ()}
    factories = factories or {"only": lambda ctx: object()}
    for name, deps in edges.items():
        registry.register(PluginSpec(name, factories[name], frozenset(deps)))
    return AssemblyKernel(config_loader=loader, registry=registry)


def test_restart_uses_latest_config_and_fresh_instances(case_log):
    source = MutableSource({"greeting": "first"})
    built: list[object] = []

    def factory(ctx):
        inst = (ctx.config.get("greeting"), object())
        built.append(inst)
        return inst

    kernel = make_kernel(source, factories={"only": factory})
    kernel.start()
    first_instance = kernel.get("only")
    case_log("停机后重启",
             {"第一代配置": "first", "停机后修改配置": "second"},
             "重启使用当前最新配置；实例全部重新构建，不残留上一代引用")

    kernel.shutdown()
    assert not kernel.is_started()

    source.set({"greeting": "second"})
    kernel.start()

    second_instance = kernel.get("only")
    assert kernel.config.get("greeting") == "second"
    assert second_instance[0] == "second"
    assert second_instance is not first_instance
    assert len(built) == 2  # 两代各装配一次


def test_restart_method_roundtrip(case_log):
    source = MutableSource({"greeting": "v1"})
    released: list[str] = []
    loader = LayeredConfigLoader(SCHEMA, [source])
    registry = PluginRegistry()
    registry.register(PluginSpec(
        "only", lambda ctx: object(), frozenset(),
        teardown=lambda inst: released.append("only"),
    ))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()
    case_log("restart 往返", "start -> 改配置 -> restart()",
             "restart = 优雅停机 + 用最新配置重新装配")

    source.set({"greeting": "v2"})
    kernel.restart()

    assert released == ["only"]
    assert kernel.is_started()
    assert kernel.config.get("greeting") == "v2"


def test_shutdown_without_start_is_noop_then_startable(case_log):
    source = MutableSource({})
    kernel = make_kernel(source)
    case_log("未启动即停机", "从未 start 直接 shutdown",
             "无组件可释放，停机为 no-op；之后仍可正常启动")

    kernel.shutdown()
    assert not kernel.is_started()
    kernel.start()
    assert kernel.is_started()


def test_shutdown_after_failed_start_releases_nothing(case_log):
    source = MutableSource({"greeting": 123})  # 类型不匹配，装配必失败
    released: list[str] = []
    loader = LayeredConfigLoader(SCHEMA, [source])
    registry = PluginRegistry()
    registry.register(PluginSpec(
        "only", lambda ctx: object(), frozenset(),
        teardown=lambda inst: released.append("only"),
    ))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    case_log("装配失败后停机", "配置类型不匹配导致 start 失败，随后 shutdown",
             "失败装配不发布任何组件，停机无释放；修复配置后可正常重启")

    with pytest.raises(ConfigValidationError):
        kernel.start()
    kernel.shutdown()
    assert released == []

    source.set({"greeting": "recovered"})
    kernel.start()
    assert kernel.config.get("greeting") == "recovered"


def test_restart_propagates_shutdown_failure(case_log):
    source = MutableSource({})

    def bad_teardown(inst):
        raise RuntimeError("release exploded")

    loader = LayeredConfigLoader(SCHEMA, [source])
    registry = PluginRegistry()
    registry.register(PluginSpec(
        "only", lambda ctx: object(), frozenset(), teardown=bad_teardown
    ))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()
    case_log("restart 遭遇释放失败", "teardown 抛错后调用 restart()",
             "ShutdownError 原样传播，不触发重新装配")

    with pytest.raises(ShutdownError):
        kernel.restart()
    assert not kernel.is_started()
    # 停机失败被记住，但不妨碍显式重新启动新一代
    kernel.start()
    assert kernel.is_started()


def test_concurrent_access_during_restart_sees_only_full_states(case_log):
    restarts, readers = 5, 6
    pair_schema = ConfigSchema((
        FieldSpec("host", str, default="h-0"),
        FieldSpec("port", int, default=0),
    ))
    source = MutableSource({"host": "h-0", "port": 0})
    loader = LayeredConfigLoader(pair_schema, [source])
    registry = PluginRegistry()
    # 实例携带本代配置的两个字段，用于校验"看到的必然是完整一代"
    registry.register(PluginSpec(
        "svc",
        lambda ctx: (ctx.config.get("host"), ctx.config.get("port")),
        frozenset(),
        teardown=lambda inst: None,
    ))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()
    case_log("重启期间并发访问",
             f"{restarts} 轮 restart（每轮换配置），{readers} 个线程持续 get",
             "单次 get 拿到的实例必须内部自洽（host 与 port 属于同一代），"
             "不得观察到半新半旧的中间态；抛错也是允许的结果")

    stop = threading.Event()
    violations: list[str] = []
    errors: list[BaseException] = []

    def reader():
        while not stop.is_set():
            try:
                host, port = kernel.get("svc")
            except Exception as exc:  # 失败是允许的结果之一
                errors.append(exc)
                continue
            if host != f"h-{port}":
                violations.append(f"host={host!r} port={port!r}")

    def restarter():
        for i in range(1, restarts + 1):
            source.set({"host": f"h-{i}", "port": i})
            kernel.restart()
        stop.set()

    with ThreadPoolExecutor(readers + 1) as pool:
        futures = [pool.submit(reader) for _ in range(readers)]
        futures.append(pool.submit(restarter))
        for f in futures:
            f.result()

    assert violations == []
    assert kernel.config.get("port") == restarts
    assert kernel.get("svc") == (f"h-{restarts}", restarts)
