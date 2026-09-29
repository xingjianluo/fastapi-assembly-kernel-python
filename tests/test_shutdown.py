"""优雅停机：逆序释放、并发只释放一次、失败汇总。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
    ShutdownError,
)


def make_kernel(specs, config_map=None):
    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource(config_map or {}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    for spec in specs:
        registry.register(spec)
    return AssemblyKernel(config_loader=loader, registry=registry)


def test_teardown_runs_in_reverse_dependency_order(case_log):
    edges = {"db": (), "svc": ("db",), "web": ("svc",)}
    released: list[str] = []

    def spec(name):
        return PluginSpec(
            name,
            lambda ctx: name,
            frozenset(edges[name]),
            teardown=lambda instance: released.append(instance),
        )

    # 注册顺序故意打乱，验证释放顺序只由依赖关系决定
    kernel = make_kernel([spec("web"), spec("db"), spec("svc")])
    kernel.start()
    case_log("逆序释放", {"装配链": "db->svc->web", "注册顺序": ["web", "db", "svc"]},
             "释放顺序必须是装配拓扑序的逆序 web->svc->db，与注册顺序无关")

    kernel.shutdown()

    assert released == ["web", "svc", "db"]
    assert not kernel.is_started()


def test_teardown_order_deterministic_regardless_of_registration(case_log):
    edges = {"a": (), "b": (), "c": ("a", "b"), "d": ("c",)}
    orders: list[tuple[str, ...]] = []
    case_log("释放顺序确定性", edges,
             "两种不同注册顺序下，释放顺序完全一致（拓扑逆序、同层按名字典序）")

    for registration in (["a", "b", "c", "d"], ["d", "c", "b", "a"]):
        released: list[str] = []
        specs = [
            PluginSpec(
                n,
                lambda ctx, n=n: n,
                frozenset(edges[n]),
                teardown=lambda inst, released=released: released.append(inst),
            )
            for n in registration
        ]
        kernel = make_kernel(specs)
        kernel.start()
        kernel.shutdown()
        orders.append(tuple(released))

    assert orders[0] == orders[1] == ("d", "c", "b", "a")


def test_concurrent_shutdown_releases_exactly_once(case_log):
    threads = 10
    case_log("并发停机", f"{threads} 个线程同时 shutdown()",
             "释放钩子总共只执行一次，所有调用方都正常返回")
    counts = {"a": 0, "b": 0}
    lock = threading.Lock()

    def spec(name, deps=()):
        def teardown(instance):
            with lock:
                counts[name] += 1
        return PluginSpec(name, lambda ctx: name, frozenset(deps), teardown=teardown)

    kernel = make_kernel([spec("a"), spec("b", ("a",))])
    kernel.start()
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()
        kernel.shutdown()

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda _: worker(), range(threads)))

    assert counts == {"a": 1, "b": 1}
    # 停机后的再次调用也只共享同一次结果，不重复释放
    kernel.shutdown()
    assert counts == {"a": 1, "b": 1}


def test_teardown_failure_aggregated_and_others_still_released(case_log):
    released: list[str] = []

    def ok(name):
        return PluginSpec(
            name, lambda ctx: name, frozenset(),
            teardown=lambda inst: released.append(inst),
        )

    def bad(name, deps):
        def teardown(inst):
            released.append(name)
            raise RuntimeError(f"{name} exploded")
        return PluginSpec(name, lambda ctx: name, frozenset(deps), teardown=teardown)

    kernel = make_kernel([ok("a"), bad("b", ("a",)), ok("c")])
    kernel.start()
    case_log("释放失败汇总", "b 的 teardown 抛 RuntimeError，a/c 正常",
             "b 失败不跳过 a、c 的释放；最终抛 ShutdownError 且 failures 可按插件名区分")

    with pytest.raises(ShutdownError) as exc_info:
        kernel.shutdown()

    assert released == ["c", "b", "a"]  # 其余组件全部照常释放
    failures = exc_info.value.failures
    assert list(failures) == ["b"]
    assert isinstance(failures["b"], RuntimeError)
    assert "b" in str(exc_info.value)


def test_concurrent_shutdown_shares_same_failure(case_log):
    threads = 6
    case_log("并发停机共享失败", f"b 释放失败，{threads} 个线程同时 shutdown()",
             "释放只执行一次；所有调用方收到同一个 ShutdownError，且不重试释放")
    counts = {"a": 0, "b": 0}
    lock = threading.Lock()

    def teardown_ok(inst):
        with lock:
            counts["a"] += 1

    def teardown_bad(inst):
        with lock:
            counts["b"] += 1
        raise RuntimeError("b exploded")

    kernel = make_kernel([
        PluginSpec("a", lambda ctx: "a", frozenset(), teardown=teardown_ok),
        PluginSpec("b", lambda ctx: "b", frozenset({"a"}), teardown=teardown_bad),
    ])
    kernel.start()
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def worker():
        barrier.wait()
        try:
            kernel.shutdown()
        except ShutdownError as exc:
            errors.append(exc)

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda _: worker(), range(threads)))

    assert counts == {"a": 1, "b": 1}  # 失败不会触发释放重试
    assert len(errors) == threads
    assert all(exc is errors[0] for exc in errors)  # 同一个异常实例

    # 失败结果被记住：后续 shutdown 仍得到同一失败，不重复释放
    with pytest.raises(ShutdownError) as exc_info:
        kernel.shutdown()
    assert exc_info.value is errors[0]
    assert counts == {"a": 1, "b": 1}
