"""优雅停机：逆序释放、并发只释放一次、失败汇总、幂等。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
    ShutdownError,
)


def make_kernel(specs):
    """specs: {name: (deps, teardown)}；工厂返回实例名字符串。"""
    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    for name, (deps, teardown) in specs.items():
        registry.register(
            PluginSpec(
                name,
                lambda ctx, n=name: f"instance-{n}",
                frozenset(deps),
                teardown=teardown,
            )
        )
    return AssemblyKernel(config_loader=loader, registry=registry)


def test_teardown_runs_in_exact_reverse_assembly_order(case_log):
    # 注册顺序故意与拓扑顺序不同，验证顺序只由依赖图决定
    specs = {
        "web": (("svc",), None),
        "db": ((), None),
        "svc": (("db",), None),
    }
    released: list[str] = []

    def hook(name):
        def td(instance):
            assert instance == f"instance-{name}"
            released.append(name)
        return td

    specs = {n: (d, hook(n)) for n, (d, _) in specs.items()}
    kernel = make_kernel(specs)
    case_log("逆序释放", {"依赖图": "db<-svc<-web", "注册顺序": list(specs)},
             "装配顺序 db,svc,web -> 释放必须严格为 web,svc,db")

    kernel.start()
    kernel.stop()

    assert released == ["web", "svc", "db"]
    assert not kernel.is_started()


def test_concurrent_stop_releases_exactly_once(case_log):
    threads = 10
    case_log("并发停机", f"{threads} 个线程同时 stop()",
             "teardown 只执行一轮，所有调用方拿到同一成功结果")
    counts = {n: 0 for n in ("a", "b", "c")}
    lock = threading.Lock()

    def hook(name):
        def td(instance):
            with lock:
                counts[name] += 1
            time.sleep(0.02)
        return td

    specs = {"a": ((), hook("a")), "b": (("a",), hook("b")), "c": (("b",), hook("c"))}
    kernel = make_kernel(specs)
    kernel.start()
    barrier = threading.Barrier(threads)
    results: list[object] = []

    def worker():
        barrier.wait()
        try:
            kernel.stop()
            results.append("ok")
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda _: worker(), range(threads)))

    assert counts == {"a": 1, "b": 1, "c": 1}
    assert results == ["ok"] * threads
    assert not kernel.is_started()


def test_teardown_failure_aggregated_and_others_still_released(case_log):
    case_log(
        "释放失败汇总",
        "svc 的 teardown 抛错，web/db 正常",
        "失败不跳过其他组件；所有并发调用方收到同一个 ShutdownError，"
        "failures 可按组件名区分",
    )
    released: list[str] = []

    def ok(name):
        return lambda instance: released.append(name)

    def bad(instance):
        released.append("svc")
        raise RuntimeError("svc exploded")

    specs = {
        "db": ((), ok("db")),
        "svc": (("db",), bad),
        "web": (("svc",), ok("web")),
    }
    kernel = make_kernel(specs)
    kernel.start()

    errors: list[BaseException] = []

    def worker():
        try:
            kernel.stop()
        except ShutdownError as exc:
            errors.append(exc)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: worker(), range(4)))

    # 其余组件仍按逆序释放
    assert released == ["web", "svc", "db"]
    # 所有调用方拿到同一个可区分的汇总异常
    assert len(errors) == 4
    assert all(e is errors[0] for e in errors)
    assert set(errors[0].failures) == {"svc"}
    assert isinstance(errors[0].failures["svc"], RuntimeError)
    assert "svc" in str(errors[0])
    assert not kernel.is_started()


def test_stop_is_idempotent_after_success(case_log):
    case_log("幂等停机", "成功停机后再次 stop()",
             "第二轮不重复释放，静默返回")
    released: list[str] = []
    specs = {"only": ((), lambda instance: released.append("only"))}
    kernel = make_kernel(specs)
    kernel.start()
    kernel.stop()
    kernel.stop()
    assert released == ["only"]


def test_stop_without_start_is_noop(case_log):
    case_log("未启动即停机", "从未 start 的内核调用 stop()",
             "无组件可释放，静默返回")
    kernel = make_kernel({"only": ((), lambda i: None)})
    kernel.stop()
    assert not kernel.is_started()
