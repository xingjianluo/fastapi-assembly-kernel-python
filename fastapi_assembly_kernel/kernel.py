"""应用装配内核：分层配置 + 依赖注入式插件 + 惰性一次性启动。

启动流程（:meth:`AssemblyKernel.start`）：

1. 用 :class:`~fastapi_assembly_kernel.config.LayeredConfigLoader` 解析配置
   （配置错误以可区分的 ConfigError 暴露，内核不吞错）；
2. 用 :class:`~fastapi_assembly_kernel.registry.PluginRegistry`
   解析确定性装配顺序（缺失依赖/环/重复注册在注册或解析期即被拒绝）；
3. 按顺序调用各插件工厂，工厂只看到依赖与配置；
4. 任一工厂失败：丢弃已构建的全部实例与配置结果，回到未启动状态，
   下次触发（start/get）从头重试——不保留任何半装配状态。

惰性与并发：start 由 :class:`~fastapi_assembly_kernel.lifecycle.LazyResource`
驱动，因此多个入口（线程 / asyncio 任务）并发触发只启动一次，
任何调用方都不会观察到半启动内核。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .errors import KernelError
from .lifecycle import LazyResource
from .registry import FactoryContext

if TYPE_CHECKING:
    from .config import LayeredConfigLoader, ResolvedConfig
    from .registry import PluginRegistry

__all__ = ["AssemblyKernel", "get_kernel"]

logger = logging.getLogger("fastapi_assembly_kernel.kernel")

# 进程内默认内核（由 FastAPI 集成或应用入口通过 set_kernel 设置）
_default_kernel: "AssemblyKernel | None" = None


class AssemblyKernel:
    """业务模块复用的应用装配内核。"""

    def __init__(
        self,
        *,
        config_loader: "LayeredConfigLoader",
        registry: "PluginRegistry",
    ) -> None:
        self._loader = config_loader
        self._registry = registry
        self._plugins: dict[str, Any] = {}
        self._config: "ResolvedConfig | None" = None
        self._lazy: LazyResource[None] = LazyResource(
            self._bootstrap,
            name="assembly-kernel",
            # 配置/依赖等内核错误本身即可区分，原样传播不做包装
            passthrough=(KernelError,),
        )

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def _bootstrap(self) -> None:
        """单次启动尝试；仅由 LazyResource 调用。

        使用局部变量构建，只有全部成功后才发布到实例属性，
        因此失败时实例上不会留下任何半装配状态。
        """
        logger.info("开始装配内核")
        config = self._loader.load()
        logger.info("配置解析完成: %r", config)

        order = self._registry.resolve_order()
        logger.info("插件装配顺序: %s", order)

        built: dict[str, Any] = {}
        for name in order:
            spec = self._registry.get(name)
            ctx = FactoryContext(plugins=built, config=config)
            logger.debug("实例化插件 %r（依赖 %s）", name, sorted(spec.depends_on))
            instance = spec.factory(ctx)
            built[name] = instance

        # 全部成功 -> 一次性发布
        self._config = config
        self._plugins = built
        logger.info("内核装配完成，共 %d 个插件", len(built))

    def start(self) -> None:
        """同步启动（并发调用只执行一次；失败后再次调用可安全重试）。"""
        self._lazy.get()

    async def astart(self) -> None:
        """异步启动（与同步入口共享同一次启动）。"""
        await self._lazy.aget()

    # ------------------------------------------------------------------
    # 访问
    # ------------------------------------------------------------------

    def get(self, plugin_name: str) -> Any:
        """惰性保证内核已启动，然后返回插件实例。

        插件不存在属于装配设计错误，启动成功后必然可查，否则抛 KeyError。
        """
        self.start()
        try:
            return self._plugins[plugin_name]
        except KeyError:
            raise KeyError(f"插件未注册: {plugin_name!r}") from None

    async def aget(self, plugin_name: str) -> Any:
        """异步版 :meth:`get`。"""
        await self.astart()
        try:
            return self._plugins[plugin_name]
        except KeyError:
            raise KeyError(f"插件未注册: {plugin_name!r}") from None

    def is_started(self) -> bool:
        return self._lazy.is_ready()

    @property
    def config(self) -> "ResolvedConfig":
        """访问配置会惰性触发启动。"""
        self.start()
        assert self._config is not None
        return self._config

    def plugin_names(self) -> list[str]:
        """已注册插件名（字典序），不需要启动即可查看。"""
        return self._registry.names()


def set_kernel(kernel: "AssemblyKernel | None") -> None:
    """设置进程内默认内核（供 FastAPI 集成使用）。"""
    global _default_kernel
    _default_kernel = kernel


def get_kernel() -> "AssemblyKernel":
    """FastAPI 依赖入口：返回由应用装配阶段设置的默认内核。"""
    if _default_kernel is None:
        raise RuntimeError(
            "默认内核尚未设置：请先调用 create_app(kernel) 或 set_kernel(kernel)"
        )
    return _default_kernel
