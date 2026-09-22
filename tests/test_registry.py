"""插件注册与依赖解析：重复注册、缺失依赖、循环依赖、确定性顺序。"""

from __future__ import annotations

import random

import pytest

from fastapi_assembly_kernel import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
    PluginError,
    PluginRegistry,
    PluginSpec,
)


def noop_factory(ctx):
    return object()


def build_registry(edges: dict[str, tuple[str, ...]], order: list[str]) -> PluginRegistry:
    reg = PluginRegistry()
    for name in order:
        reg.register(PluginSpec(name, noop_factory, frozenset(edges[name])))
    return reg


EDGES = {
    "db": (),
    "cache": (),
    "auth": ("db",),
    "svc": ("db", "cache"),
    "web": ("svc", "auth"),
    "zebra": ("db",),
    "alpha": ("cache",),
}


def test_duplicate_registration_rejected(case_log):
    case_log("重复注册", "两次注册插件 'a'",
             "同名重复注册 -> DuplicatePluginError（注册期立即拒绝）")
    reg = PluginRegistry()
    reg.register(PluginSpec("a", noop_factory))
    with pytest.raises(DuplicatePluginError):
        reg.register(PluginSpec("a", noop_factory))


def test_missing_dependency_rejected(case_log):
    edges = {"a": ("x",), "b": ("y", "x")}
    case_log("缺失依赖", edges,
             "依赖未注册 -> MissingDependencyError，错误信息列出全部缺失项")
    reg = build_registry(edges, ["a", "b"])
    with pytest.raises(MissingDependencyError) as exc_info:
        reg.resolve_order()
    msg = str(exc_info.value)
    assert "'a'" in msg and "'x'" in msg and "'b'" in msg and "'y'" in msg


def test_self_dependency_is_cycle(case_log):
    case_log("自依赖", "a depends_on a",
             "自依赖是环的最小形式 -> CircularDependencyError（声明期拒绝）")
    with pytest.raises(CircularDependencyError):
        PluginSpec("a", noop_factory, frozenset({"a"}))


def test_cycle_rejected_with_path(case_log):
    edges = {"a": ("b",), "b": ("c",), "c": ("a",)}
    case_log("循环依赖", edges,
             "a->b->c->a 构成环 -> CircularDependencyError，信息包含环路径")
    reg = build_registry(edges, ["a", "b", "c"])
    with pytest.raises(CircularDependencyError) as exc_info:
        reg.resolve_order()
    msg = str(exc_info.value)
    assert "a -> b -> c -> a" in msg


def test_two_node_cycle(case_log):
    edges = {"p": ("q",), "q": ("p",)}
    case_log("双节点环", edges, "p<->q -> CircularDependencyError")
    reg = build_registry(edges, ["p", "q"])
    with pytest.raises(CircularDependencyError):
        reg.resolve_order()


def test_order_is_deterministic_regardless_of_registration_order(case_log):
    case_log(
        "装配顺序确定性",
        f"依赖图 {EDGES}，注册顺序随机打乱 30 次",
        "拓扑排序用最小堆按名称选取，输出与注册/字典顺序无关",
    )
    expected = build_registry(EDGES, list(EDGES)).resolve_order()
    pos = {n: i for i, n in enumerate(expected)}
    for name, deps in EDGES.items():
        for dep in deps:
            assert pos[dep] < pos[name], f"{dep} 必须先于 {name}"
    for seed in range(30):
        shuffled = list(EDGES)
        random.Random(seed).shuffle(shuffled)
        assert build_registry(EDGES, shuffled).resolve_order() == expected


def test_large_graph_deterministic(case_log):
    """大规模依赖图（1000 节点分层 DAG）顺序确定且合法。"""
    n = 1000
    edges: dict[str, tuple[str, ...]] = {}
    for i in range(n):
        # 每个节点依赖编号更小的 2 个节点 -> 无环
        deps = tuple(f"n{j}" for j in (i - 1, i - 7) if j >= 0)
        edges[f"n{i}"] = deps
    case_log("大规模依赖图", f"{n} 节点分层 DAG",
             "两次解析结果一致，且每个依赖都排在使用者之前")
    reg = build_registry(edges, list(edges))
    order1 = reg.resolve_order()
    order2 = reg.resolve_order()
    assert order1 == order2
    assert len(order1) == n
    pos = {name: i for i, name in enumerate(order1)}
    for name, deps in edges.items():
        for dep in deps:
            assert pos[dep] < pos[name]


def test_plugin_errors_distinguishable(case_log):
    case_log("插件错误可区分", "三类插件错误",
             "Duplicate/Missing/Circular 互不包含，且同属 PluginError")
    for cls in (DuplicatePluginError, MissingDependencyError, CircularDependencyError):
        assert issubclass(cls, PluginError)
    assert not issubclass(DuplicatePluginError, MissingDependencyError)
    assert not issubclass(MissingDependencyError, CircularDependencyError)
    assert not issubclass(CircularDependencyError, DuplicatePluginError)
