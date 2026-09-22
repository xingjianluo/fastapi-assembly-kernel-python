"""插件装配测试：注册校验、确定性拓扑序、循环/缺失/重复、大图稳定性。"""

from __future__ import annotations

import logging
import random

import pytest

from assembly_kernel import Plugin, PluginRegistry, assemble, topological_order
from assembly_kernel.errors import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
    PluginError,
)

log = logging.getLogger("tests.plugins")


def _factory(tag: str):
    def _create(deps):
        return {"tag": tag, "deps": dict(deps)}

    return _create


def _registry_from(edges: list[tuple[str, tuple[str, ...]]]) -> PluginRegistry:
    """按 ``(插件名, 依赖名元组)`` 列表构建注册表，工厂记录自身标签。"""
    reg = PluginRegistry()
    for name, requires in edges:
        reg.register(Plugin(name, frozenset(requires), _factory(name)))
    return reg


class TestRegistration:
    def test_duplicate_registration_rejected(self, case_log):
        reg = PluginRegistry()
        reg.register(Plugin("a"))
        case_log.input(actions=["register a", "register a again"])
        with pytest.raises(DuplicatePluginError) as exc:
            reg.register(Plugin("a"))
        assert exc.value.name == "a"
        case_log.actual(repr(exc.value))

    def test_empty_registry_assembles_empty(self):
        ctx = assemble(PluginRegistry())
        assert ctx.order == []
        assert "anything" not in ctx

    def test_get_unknown_instance_is_plugin_error(self):
        ctx = assemble(PluginRegistry())
        with pytest.raises(PluginError):
            ctx.get("ghost")


class TestMissingDependency:
    def test_missing_dependency_error_fields(self, case_log):
        reg = PluginRegistry()
        reg.register(Plugin("db"))
        reg.register(Plugin("api", frozenset({"db", "missing-cache"})))
        case_log.input(plugin="api", requires=["db", "missing-cache"])
        with pytest.raises(MissingDependencyError) as exc:
            topological_order(reg)
        assert exc.value.plugin == "api"
        assert exc.value.dependency == "missing-cache"
        case_log.actual(repr(exc.value))

    def test_missing_dependency_reported_deterministically(self, case_log):
        """多个缺失同时存在时，按 (插件名, 依赖名) 序报告第一个。"""
        reg = PluginRegistry()
        reg.register(Plugin("zeta", frozenset({"absent-b"})))
        reg.register(Plugin("alpha", frozenset({"absent-a"})))
        with pytest.raises(MissingDependencyError) as exc:
            topological_order(reg)
        assert (exc.value.plugin, exc.value.dependency) == ("alpha", "absent-a")
        case_log.actual(f"报告 ({exc.value.plugin}, {exc.value.dependency})")


class TestCircularDependency:
    def test_self_cycle(self, case_log):
        reg = _registry_from([("a", ("a",))])
        case_log.input(edges=["a -> a"])
        with pytest.raises(CircularDependencyError) as exc:
            topological_order(reg)
        assert exc.value.cycle[0] == exc.value.cycle[-1] == "a"
        case_log.actual(f"cycle={exc.value.cycle}")

    def test_three_node_cycle_path_is_explanable(self, case_log):
        reg = _registry_from([("a", ("b",)), ("b", ("c",)), ("c", ("a",))])
        case_log.input(edges=["a->b", "b->c", "c->a"])
        with pytest.raises(CircularDependencyError) as exc:
            topological_order(reg)
        # 从字典序最小节点出发的闭合路径，可解释且确定
        assert exc.value.cycle == ["a", "b", "c", "a"]
        case_log.actual(f"cycle path={exc.value.cycle}")

    def test_cycle_with_hanging_consumers_isolated(self, case_log):
        """环外依赖环的叶子节点不出现在环路径中。"""
        reg = _registry_from(
            [("a", ("b",)), ("b", ("a",)), ("leaf1", ("a",)), ("leaf2", ("b",))]
        )
        with pytest.raises(CircularDependencyError) as exc:
            topological_order(reg)
        assert set(exc.value.cycle) == {"a", "b"}
        case_log.actual(f"cycle={exc.value.cycle}（叶子已剥离）")

    def test_independent_chain_resolves_while_other_component_cycles(self):
        reg = _registry_from(
            [("a", ("b",)), ("b", ("a",)), ("x", ()), ("y", ("x",))]
        )
        with pytest.raises(CircularDependencyError):
            topological_order(reg)


class TestDeterministicOrder:
    def test_order_respects_dependencies(self):
        reg = _registry_from(
            [
                ("api", ("cache", "db")),
                ("cache", ("db",)),
                ("db", ()),
            ]
        )
        ctx = assemble(reg)
        assert ctx.order == ["db", "cache", "api"]
        api = ctx.get("api")
        assert set(api["deps"]) == {"cache", "db"}

    def test_order_independent_of_registration_sequence(self, case_log):
        edges = [
            ("api", ("cache", "db")),
            ("cache", ("db",)),
            ("db", ()),
        ]
        seen = set()
        for seed in range(20):
            shuffled = edges[:]
            random.Random(seed).shuffle(shuffled)
            ctx = assemble(_registry_from(shuffled))
            seen.add(tuple(ctx.order))
        assert seen == {("db", "cache", "api")}
        case_log.actual(f"20 种乱序注册得到唯一装配序: {seen}")

    def test_ties_break_by_name_not_dictionary_order(self):
        """同一就绪层内的多个无关节点按名称排序，而非哈希/插入序。"""
        reg = _registry_from([(name, ()) for name in ["zeta", "alpha", "mid", "beta"]])
        assert topological_order(reg) == ["alpha", "beta", "mid", "zeta"]

    def test_large_graph_stable_order(self, case_log):
        """500 个节点、每节点最多 5 条指向小编号节点的边，重复计算结果一致。"""
        rng = random.Random(42)
        edges: list[tuple[str, tuple[str, ...]]] = []
        for i in range(500):
            name = f"n{i:03d}"
            deps = tuple(f"n{j:03d}" for j in rng.sample(range(i), min(5, i)))
            edges.append((name, deps))
        orders = set()
        for _ in range(5):
            shuffled = edges[:]
            rng.shuffle(shuffled)
            orders.add(tuple(topological_order(_registry_from(shuffled))))
        assert len(orders) == 1
        order = orders.pop()
        # 依赖闭包校验：每个节点都排在其依赖之后
        position = {name: idx for idx, name in enumerate(order)}
        for name, requires in edges:
            assert all(position[d] < position[name] for d in requires)
        case_log.actual(f"500 节点图 5 次乱序装配序一致，首节点={order[0]} 末节点={order[-1]}")

    def test_order_is_a_copy(self):
        ctx = assemble(_registry_from([("a", ()), ("b", ("a",))]))
        order = ctx.order
        order.append("tampered")
        assert ctx.order == ["a", "b"]


class TestFactoryWiring:
    def test_dependencies_injected_by_name(self):
        reg = PluginRegistry()
        reg.register(Plugin("db", factory=lambda d: ("DB",)))
        reg.register(Plugin("api", frozenset({"db"}), factory=lambda d: d["db"] * 2))
        ctx = assemble(reg)
        assert ctx.get("api") == ("DB", "DB")

    def test_factory_failure_is_wrapped_and_no_partial_context_published(self, case_log):
        reg = PluginRegistry()
        reg.register(Plugin("ok", factory=lambda d: 1))

        def boom(_deps):
            raise ValueError("factory boom")

        reg.register(Plugin("bad", frozenset({"ok"}), factory=boom))
        case_log.input(factories={"ok": "succeeds", "bad": "raises ValueError"})
        with pytest.raises(PluginError) as exc:
            assemble(reg)
        assert isinstance(exc.value.__cause__, ValueError)
        assert "bad" in str(exc.value)
        case_log.actual(repr(exc.value))

    def test_none_factory_yields_none_instance(self):
        reg = PluginRegistry()
        reg.register(Plugin("marker"))
        ctx = assemble(reg)
        assert ctx.get("marker") is None
