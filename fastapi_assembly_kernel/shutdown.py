"""优雅停机：按装配逆序释放组件的停机会话。

规则：

* 释放顺序为装配顺序的逆序——依赖别人的组件先释放，
  被别人依赖的组件后释放；顺序由注册表的确定性拓扑序决定，
  不受注册或遍历顺序影响；
* 每个组件的释放钩子至多执行一次（由内核按"代"保证，
  并发触发停机的调用方共享同一次释放与同一个结果）；
* 某个组件释放失败不会中断或跳过其余组件的释放；
  全部尝试完成后以 :class:`~fastapi_assembly_kernel.errors.ShutdownError`
  汇总抛出，失败明细按插件名可区分；
* 没有声明 teardown 钩子的组件视为无需释放，直接跳过。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from .errors import ShutdownError

if TYPE_CHECKING:
    from .registry import PluginRegistry

__all__ = ["ShutdownSession"]

logger = logging.getLogger("fastapi_assembly_kernel.shutdown")


class ShutdownSession:
    """一次停机会话：对一代已装配组件执行确定性的逆序释放。

    会话本身只描述"怎么释放一代组件"；同一进程内"只释放一次、
    并发调用共享同一结果"由内核按代（generation）用一次性原语保证。
    """

    def __init__(
        self,
        *,
        registry: "PluginRegistry",
        plugins: Mapping[str, Any],
    ) -> None:
        self._registry = registry
        self._plugins = dict(plugins)

    def shutdown_order(self) -> list[str]:
        """确定性的释放顺序：装配拓扑序的逆序，仅保留已装配组件。"""
        assembled = set(self._plugins)
        return [
            name
            for name in reversed(self._registry.resolve_order())
            if name in assembled
        ]

    def run(self) -> None:
        """执行逆序释放；失败汇总为 :class:`ShutdownError`，不跳过其余组件。"""
        order = self.shutdown_order()
        logger.info("开始停机会话，释放顺序: %s", order)

        failures: dict[str, BaseException] = {}
        for name in order:
            spec = self._registry.get(name)
            teardown = spec.teardown
            if teardown is None:
                logger.debug("组件 %r 无释放钩子，跳过", name)
                continue
            try:
                teardown(self._plugins[name])
            except Exception as exc:
                # 单个组件失败只记录，不中断其余组件的释放
                failures[name] = exc
                logger.warning(
                    "组件 %r 释放失败: %s: %s", name, type(exc).__name__, exc
                )
            else:
                logger.debug("组件 %r 释放完成", name)

        if failures:
            summary = ", ".join(
                f"{name}={type(exc).__name__}"
                for name, exc in sorted(failures.items())
            )
            raise ShutdownError(
                f"停机过程中 {len(failures)}/{len(order)} 个组件释放失败: {summary}",
                failures=failures,
            )
        logger.info("停机会话完成，共释放 %d 个组件", len(order))
