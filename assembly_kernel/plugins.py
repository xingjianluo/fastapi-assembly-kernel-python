"""插件注册与依赖装配。

插件以声明式的名称、依赖与工厂函数注册；装配时先做完整性校验
（重复注册 / 缺失依赖），再基于确定性拓扑排序产出装配顺序：

* 每个插件的依赖项先于自身被实例化，实例按名称注入工厂；
* 拓扑排序使用 Kahn 算法，同一就绪层内按插件名排序，结果只取决于
  名称与依赖声明，与注册顺序、字典哈希、导入顺序均无关；
* 检测到环时以 :class:`CircularDependencyError` 拒绝，错误中包含
  从环上字典序最小节点出发的确定环路径，例如 ``a -> b -> c -> a``；
* 工厂抛出的异常包装为 :class:`PluginError`（保留 ``__cause__``），
  保证失败原因与注册/依赖类错误同样可区分、可定位。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import (
    CircularDependencyError,
    DuplicatePluginError,
    MissingDependencyError,
    PluginError,
)

__all__ = [
    "Plugin",
    "PluginRegistry",
    "AssembledContext",
    "topological_order",
    "assemble",
]

_logger = logging.getLogger("assembly_kernel.plugins")

# 工厂接收“依赖名 -> 已实例化依赖”的映射，返回插件实例。
PluginFactory = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class Plugin:
    """插件声明。

    :param name: 全局唯一名称。
    :param requires: 依赖的其他插件名称；声明次序不影响装配结果。
    :param factory: 依赖就绪后调用的工厂函数；为 ``None`` 时实例记为 ``None``
        （适合纯“能力位”声明节点）。
    """

    name: str
    requires: frozenset[str] = frozenset()
    factory: PluginFactory | None = None


class PluginRegistry:
    """插件注册表：负责注册期校验，内部以名称为键。"""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        """注册插件；同名重复注册抛 :class:`DuplicatePluginError`。"""
        if plugin.name in self._plugins:
            raise DuplicatePluginError(plugin.name)
        if not isinstance(plugin.name, str) or not plugin.name:
            raise PluginError("插件名称必须是非空字符串")
        self._plugins[plugin.name] = plugin
        _logger.debug(
            "插件注册完成 name=%s requires=%s",
            plugin.name,
            sorted(plugin.requires),
        )

    def get(self, name: str) -> Plugin | None:
        """按名称获取插件声明，不存在返回 ``None``。"""
        return self._plugins.get(name)

    def names(self) -> list[str]:
        """返回按名称排序后的全部插件名（确定性顺序）。"""
        return sorted(self._plugins)

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: object) -> bool:
        return name in self._plugins


class AssembledContext:
    """装配结果：持有按拓扑顺序实例化的插件。"""

    def __init__(self, instances: Mapping[str, Any], order: Sequence[str]) -> None:
        self._instances: dict[str, Any] = dict(instances)
        self._order: list[str] = list(order)

    @property
    def order(self) -> list[str]:
        """拓扑装配顺序（返回副本，外部修改不影响内核状态）。"""
        return list(self._order)

    def get(self, name: str) -> Any:
        """按名称获取已装配实例；未装配抛 :class:`PluginError`。"""
        if name not in self._instances:
            raise PluginError(f"插件 '{name}' 不在装配结果中")
        return self._instances[name]

    def names(self) -> list[str]:
        """已装配插件名（按拓扑顺序）。"""
        return list(self._order)

    def __contains__(self, name: object) -> bool:
        return name in self._instances

    def __repr__(self) -> str:
        return f"AssembledContext(order={self._order!r})"


def topological_order(registry: PluginRegistry) -> list[str]:
    """基于名称与依赖声明计算确定性拓扑序。

    :raises MissingDependencyError: 某插件依赖了未注册名称
        （按插件名、依赖名排序后报告第一个，保证确定性）。
    :raises CircularDependencyError: 依赖图存在环。
    """
    plugins = {name: registry.get(name) for name in registry.names()}

    # 1) 完整性检查：所有依赖必须存在。按 (插件名, 依赖名) 排序后报告首个问题。
    missing = sorted(
        (name, dep)
        for name, plugin in plugins.items()
        for dep in plugin.requires
        if dep not in plugins
    )
    if missing:
        plugin_name, dep = missing[0]
        raise MissingDependencyError(plugin_name, dep)

    # 2) Kahn 算法；就绪队列按名称排序，保证大规模图下顺序确定且稳定。
    indegree = {
        name: len({dep for dep in plugin.requires if dep in plugins})
        for name, plugin in plugins.items()
    }
    dependents: dict[str, set[str]] = {name: set() for name in plugins}
    for name, plugin in plugins.items():
        for dep in plugin.requires:
            dependents[dep].add(name)

    ready: deque[str] = deque(
        name for name in sorted(plugins) if indegree[name] == 0
    )
    order: list[str] = []
    while ready:
        name = ready.popleft()
        order.append(name)
        # 新就绪节点必须重新排序后并入，杜绝任何字典迭代顺序的影响。
        newly_ready: list[str] = []
        for dependent in sorted(dependents[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly_ready.append(dependent)
        if newly_ready:
            ready.extend(sorted(newly_ready))
            ready = deque(sorted(ready))

    if len(order) != len(plugins):
        cycle = _extract_cycle(plugins, order)
        raise CircularDependencyError(cycle)

    return order


def assemble(registry: PluginRegistry) -> AssembledContext:
    """校验、排序并按拓扑序实例化全部插件。

    每个工厂收到 ``{依赖名: 依赖实例}`` 的只读映射；任一工厂失败则
    整体装配失败并抛 :class:`PluginError`（不返回部分结果）。
    """
    order = topological_order(registry)
    instances: dict[str, Any] = {}
    for name in order:
        plugin = registry.get(name)
        assert plugin is not None  # 拓扑序中的名称必然来自注册表
        deps = {dep: instances[dep] for dep in sorted(plugin.requires)}
        _logger.debug("开始实例化插件 name=%s deps=%s", name, sorted(deps))
        if plugin.factory is None:
            instances[name] = None
            continue
        try:
            instances[name] = plugin.factory(deps)
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001 - 需要统一包装以保证可区分
            raise PluginError(
                f"插件 '{name}' 工厂执行失败: {type(exc).__name__}"
            ) from exc
    _logger.info("插件装配完成 order=%s", order)
    return AssembledContext(instances, order)


def _extract_cycle(
    plugins: Mapping[str, Plugin], resolved: Sequence[str]
) -> list[str]:
    """从拓扑排序未能消费的节点中提取一条确定的环路径。

    步骤：
    1. 未决节点集合中，反复剥离“在未决子图内入度为 0”的节点，
       剩下的恰好是参与环（强连通分量）的节点；
    2. 取字典序最小节点为起点，沿已排序邻接边做 DFS，找到首条回到
       起点的路径并闭合之，得到唯一确定的环表达。
    """
    unresolved = set(plugins) - set(resolved)

    # 仅统计两端都在未决集合内的边（剥离挂在环外的下游节点）。
    indegree = {
        name: sum(
            1 for dep in plugins[name].requires if dep in unresolved
        )
        for name in unresolved
    }
    peeled = True
    while peeled:
        peeled = False
        for name in sorted(unresolved):
            if indegree[name] == 0:
                unresolved.discard(name)
                for dep_of in plugins:
                    if name in plugins[dep_of].requires and dep_of in unresolved:
                        indegree[dep_of] -= 1
                peeled = True
                break

    if not unresolved:  # 理论上不可达，防御性处理
        return sorted(plugins)

    start = min(unresolved)
    stack: list[str] = [start]
    on_stack = {start}

    def dfs(node: str) -> list[str] | None:
        for dep in sorted(plugins[node].requires):
            if dep not in unresolved:
                continue
            if dep == start:
                return stack + [start]
            if dep not in on_stack:
                stack.append(dep)
                on_stack.add(dep)
                found = dfs(dep)
                if found is not None:
                    return found
                on_stack.discard(dep)
                stack.pop()
        return None

    cycle = dfs(start)
    if cycle is None:  # 防御性回退
        return [start, start]
    return cycle
