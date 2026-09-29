"""插件注册、依赖校验与确定性拓扑装配顺序解析。

确定性保证（不依赖字典迭代顺序或注册/导入顺序）：

* 缺失依赖检测覆盖全部已注册插件，错误信息按名称排序；
* 环检测按名称字典序遍历邻接点，报告一条确定的环路径；
* 拓扑排序采用 Kahn 算法，就绪集合用最小堆（按插件名）选取，
  因此同层插件严格按名称字典序输出。

实例化不在本模块完成：本模块只负责给出稳定的装配顺序，
:class:`~fastapi_assembly_kernel.kernel.AssemblyKernel` 按顺序调用工厂。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .errors import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
)

__all__ = [
    "PluginSpec",
    "PluginRegistry",
    "FactoryContext",
]

# 工厂签名：(装配上下文) -> 组件实例
PluginFactory = Callable[["FactoryContext"], Any]

# 释放钩子签名：(组件实例) -> None；在停机时按逆装配顺序调用
TeardownHook = Callable[[Any], None]


@dataclass(frozen=True)
class FactoryContext:
    """传给插件工厂的装配上下文。

    plugins 只包含装配顺序中先于当前插件、已成功实例化的组件，
    以只读映射暴露；工厂可通过 :meth:`get` 取用声明过的依赖。
    config 为已解析的分层配置（可选，由装配内核注入）。
    """

    plugins: Mapping[str, Any] = field(default_factory=dict)
    config: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugins", MappingProxyType(dict(self.plugins)))

    def get(self, name: str) -> Any:
        if name not in self.plugins:
            raise KeyError(
                f"依赖 {name!r} 尚未装配（工厂只能访问 depends_on 中声明的依赖）"
            )
        return self.plugins[name]


@dataclass(frozen=True)
class PluginSpec:
    """插件声明：名称、依赖名称集合、工厂与可选的释放钩子。

    teardown 在优雅停机时以组件实例为参数被调用，
    调用顺序为装配顺序的严格逆序（先装配的后释放）。
    """

    name: str
    factory: PluginFactory
    depends_on: frozenset[str] = frozenset()
    teardown: TeardownHook | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("插件名必须是非空字符串")
        if not callable(self.factory):
            raise ValueError(f"插件 {self.name!r} 的 factory 必须可调用")
        if self.teardown is not None and not callable(self.teardown):
            raise ValueError(f"插件 {self.name!r} 的 teardown 必须可调用或为 None")
        deps = frozenset(self.depends_on)
        if not all(isinstance(d, str) and d for d in deps):
            raise ValueError(f"插件 {self.name!r} 的依赖名必须是非空字符串")
        if self.name in deps:
            # 自依赖是环的最小形式，注册期立即拒绝，原因归类为循环依赖
            raise CircularDependencyError(
                f"插件 {self.name!r} 不能依赖自身: {self.name} -> {self.name}"
            )
        object.__setattr__(self, "depends_on", deps)


class PluginRegistry:
    """注册插件并解析确定性的装配顺序。"""

    def __init__(self) -> None:
        self._specs: dict[str, PluginSpec] = {}

    def register(self, spec: PluginSpec) -> None:
        """注册插件；同名重复注册立即抛 :class:`DuplicatePluginError`。"""
        if spec.name in self._specs:
            raise DuplicatePluginError(f"插件重复注册: {spec.name!r}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> PluginSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"未注册的插件: {name!r}") from None

    def names(self) -> list[str]:
        return sorted(self._specs)

    def resolve_order(self) -> list[str]:
        """返回确定的装配顺序（依赖在前，同层按名称字典序）。

        先做缺失依赖校验（可在任意环之外给出明确原因），再做环检测，
        最后用最小堆 Kahn 排序。
        """
        self._check_missing_dependencies()
        self._check_cycles()
        return self._topological_order()

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def _check_missing_dependencies(self) -> None:
        known = set(self._specs)
        missing: dict[str, list[str]] = {}
        for name in sorted(known):
            deps = self._specs[name].depends_on
            absent = sorted(deps - known)
            if absent:
                missing[name] = absent
        if missing:
            lines = [
                f"插件 {owner!r} 缺失依赖 {deps}"
                for owner, deps in sorted(missing.items())
            ]
            raise MissingDependencyError(
                "存在未注册的插件依赖:\n  " + "\n  ".join(lines)
            )

    def _check_cycles(self) -> None:
        # 按名称排序的邻接表，保证从同一张图中找出的环路径确定
        adj = {
            name: sorted(self._specs[name].depends_on)
            for name in self._specs
        }
        color: dict[str, int] = {}  # 0=未见 1=在栈中 2=完成
        stack: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = 1
            stack.append(node)
            for dep in adj[node]:
                if color.get(dep, 0) == 1:
                    start = stack.index(dep)
                    return stack[start:] + [dep]
                if color.get(dep, 0) == 0:
                    found = dfs(dep)
                    if found is not None:
                        return found
            stack.pop()
            color[node] = 2
            return None

        for name in sorted(adj):
            if color.get(name, 0) == 0:
                cycle = dfs(name)
                if cycle is not None:
                    raise CircularDependencyError(
                        "检测到插件循环依赖: " + " -> ".join(cycle)
                    )

    # ------------------------------------------------------------------
    # 确定性拓扑排序（最小堆 Kahn）
    # ------------------------------------------------------------------

    def _topological_order(self) -> list[str]:
        indegree = {
            name: len(self._specs[name].depends_on) for name in self._specs
        }
        dependents: dict[str, list[str]] = {name: [] for name in self._specs}
        for name, spec in self._specs.items():
            for dep in spec.depends_on:
                dependents[dep].append(name)
        for lst in dependents.values():
            lst.sort()

        ready: list[str] = [n for n, d in indegree.items() if d == 0]
        heapq.heapify(ready)

        order: list[str] = []
        while ready:
            name = heapq.heappop(ready)
            order.append(name)
            for child in dependents[name]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        return order
