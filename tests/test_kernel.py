"""装配内核：依赖注入装配、配置注入、并发启动、失败重试。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigValidationError,
    FieldSpec,
    InitializationFailedError,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
)


def make_kernel(edges, factories, config_map=None, schema_fields=()):
    schema = ConfigSchema(tuple(schema_fields))
    sources = [MappingSource(config_map or {}, name="test", priority=0)]
    loader = LayeredConfigLoader(schema, sources)
    registry = PluginRegistry()
    for name, deps in edges.items():
        registry.register(PluginSpec(name, factories[name], frozenset(deps)))
    return AssemblyKernel(config_loader=loader, registry=registry)


def test_plugins_assembled_in_dependency_order_with_context(case_log):
    edges = {"db": (), "svc": ("db",), "web": ("svc",)}
    built: list[str] = []
    received: dict[str, object] = {}

    def factory(name):
        def f(ctx):
            built.append(name)
            received[name] = ctx
            return f"instance-{name}"
        return f

    factories = {n: factory(n) for n in edges}
    kernel = make_kernel(edges, factories)
    case_log("依赖注入装配", edges,
             "按拓扑顺序实例化；下游工厂可通过 ctx.get 取得已装配依赖")

    kernel.start()
    assert built == ["db", "svc", "web"]
    assert received["svc"].get("db") == "instance-db"
    assert received["web"].get("svc") == "instance-svc"
    assert kernel.get("web") == "instance-web"
    with pytest.raises(KeyError):
        received["db"].get("svc")  # db 装配时 svc 尚不存在


def test_config_injected_into_factories(case_log):
    case_log("配置注入", {"greeting": "hi"},
             "工厂可通过 ctx.config 读取已解析的分层配置")
    seen: dict[str, str] = {}

    def f(ctx):
        seen["greeting"] = ctx.config.get("greeting")
        return object()

    kernel = make_kernel(
        {"only": ()},
        {"only": f},
        config_map={"greeting": "hi"},
        schema_fields=(FieldSpec("greeting", str, default="x"),),
    )
    kernel.start()
    assert seen["greeting"] == "hi"
    assert kernel.config.get("greeting") == "hi"


def test_concurrent_start_runs_bootstrap_once(case_log):
    threads = 12
    case_log("并发启动", f"{threads} 个线程同时 start()",
             "装配只执行一次，每个工厂恰好被调用一次")
    counts = {n: 0 for n in ("a", "b", "c")}
    lock = threading.Lock()

    def slow_factory(name):
        def f(ctx):
            with lock:
                counts[name] += 1
            time.sleep(0.05)
            return name
        return f

    edges = {"a": (), "b": ("a",), "c": ("b",)}
    kernel = make_kernel(edges, {n: slow_factory(n) for n in edges})
    barrier = threading.Barrier(threads)

    def worker():
        barrier.wait()
        kernel.start()

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda _: worker(), range(threads)))

    assert counts == {"a": 1, "b": 1, "c": 1}
    assert kernel.is_started()


def test_factory_failure_leaves_no_partial_state_and_retries(case_log):
    case_log(
        "工厂失败重试",
        "插件 b 的工厂首次抛错，a 已装配成功",
        "失败后内核回到未启动状态，已装配实例被丢弃；重试时从头装配",
    )
    built_a: list[str] = []
    attempts = {"b": 0}

    def fa(ctx):
        built_a.append("a")
        return "a-instance"

    def fb(ctx):
        attempts["b"] += 1
        if attempts["b"] == 1:
            raise ValueError("factory exploded")
        return "b-instance"

    kernel = make_kernel({"a": (), "b": ("a",)}, {"a": fa, "b": fb})

    with pytest.raises(InitializationFailedError) as exc_info:
        kernel.start()
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert not kernel.is_started()
    assert built_a == ["a"]  # a 装配过一次，但其结果未被保留

    # 重试：从头装配，a 会再次构建
    kernel.start()
    assert kernel.is_started()
    assert built_a == ["a", "a"]
    assert kernel.get("b") == "b-instance"


def test_config_error_surfaces_as_distinguishable_type(case_log):
    case_log("配置错误暴露", {"port": "not-an-int"},
             "配置校验失败 -> 启动抛 ConfigValidationError（可区分），"
             "而非 InitializationFailedError")
    kernel = make_kernel(
        {"only": ()},
        {"only": lambda ctx: object()},
        config_map={"port": "not-an-int"},
        schema_fields=(FieldSpec("port", int, default=1),),
    )
    with pytest.raises(ConfigValidationError):
        kernel.start()
    assert not kernel.is_started()


def test_lazy_get_triggers_start(case_log):
    case_log("惰性启动", "未显式 start，直接 get('only')",
             "首次访问触发一次性装配")
    kernel = make_kernel({"only": ()}, {"only": lambda ctx: "ready"})
    assert not kernel.is_started()
    assert kernel.get("only") == "ready"
    assert kernel.is_started()
    with pytest.raises(KeyError):
        kernel.get("nope")
