"""应用装配内核：分层配置 + 依赖注入式插件 + 惰性启动 + 停机/重启/热重载。

启动流程（:meth:`AssemblyKernel.start`）：

1. 用 :class:`~fastapi_assembly_kernel.config.LayeredConfigLoader` 解析配置
   （配置错误以可区分的 ConfigError 暴露，内核不吞错）；
2. 用 :class:`~fastapi_assembly_kernel.registry.PluginRegistry`
   解析确定性装配顺序（缺失依赖/环/重复注册在注册或解析期即被拒绝）；
3. 按顺序调用各插件工厂，工厂只看到依赖与配置；
4. 任一工厂失败：丢弃已构建的全部实例与配置结果，回到未启动状态，
   下次触发（start/get）从头重试——不保留任何半装配状态。

代（generation）模型：每一次"启动到停机"是一代。代内配置与插件实例
一经发布不再变化（热重载只会把配置整体替换为另一份完整快照）；
:meth:`shutdown` 退役当前代并按装配逆序释放组件，随后任何访问都在
新一代上重新装配——重启不残留上一代的状态或半成品引用。

并发语义：

* start/get 由 :class:`~fastapi_assembly_kernel.lifecycle.LazyResource`
  驱动，多线程 / asyncio 混合触发只装配一次；
* shutdown 由 :class:`~fastapi_assembly_kernel.lifecycle.SettledOnce`
  驱动，多个入口并发触发只真正释放一次，其余调用方等待同一结果；
* 停机与装配/访问并发时，调用方要么看到停机前完整的一代，要么看到
  重启后完整的新一代，要么收到失败，绝不观察到中间态。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any

from .errors import KernelError
from .lifecycle import InitState, LazyResource, SettledOnce
from .registry import FactoryContext
from .shutdown import ShutdownSession

if TYPE_CHECKING:
    from .config import LayeredConfigLoader, ResolvedConfig
    from .registry import PluginRegistry

__all__ = ["AssemblyKernel", "get_kernel", "set_kernel"]

logger = logging.getLogger("fastapi_assembly_kernel.kernel")

# 进程内默认内核（由 FastAPI 集成或应用入口通过 set_kernel 设置）
_default_kernel: "AssemblyKernel | None" = None


class _GenerationRetiredError(KernelError):
    """内部信号：装配开始时所在代已被停机退役，调用方应切换到新一代。"""


class _Generation:
    """一代运行状态：配置快照、插件实例与各自的一次性原语。"""

    __slots__ = ("lazy", "stop_once", "retired", "config", "plugins")

    def __init__(self) -> None:
        self.lazy: LazyResource[None]
        self.stop_once: SettledOnce
        self.retired: bool = False
        self.config: "ResolvedConfig | None" = None
        self.plugins: dict[str, Any] = {}


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
        # _state_lock 保护 _gen / _stopping 与代的 retired 标志；
        # _reload_lock 序列化热重载的"加载->整体切换"临界区。
        self._state_lock = threading.Lock()
        self._reload_lock = threading.Lock()
        self._stopping: SettledOnce | None = None
        self._gen = self._new_generation()

    # ------------------------------------------------------------------
    # 代管理
    # ------------------------------------------------------------------

    def _new_generation(self) -> _Generation:
        gen = _Generation()
        gen.lazy = LazyResource(
            lambda: self._bootstrap(gen),
            name="assembly-kernel",
            # 配置/依赖等内核错误本身即可区分，原样传播不做包装
            passthrough=(KernelError,),
        )
        gen.stop_once = SettledOnce(
            lambda: self._stop(gen), name="assembly-kernel-shutdown"
        )
        return gen

    def _active_generation(self) -> _Generation:
        """返回一个已启动且未退役的代（必要时触发装配）。

        与停机并发时：若当前代在装配期间被退役，先汇入该代的释放
        （保证刚构建的组件不泄漏），再切换到新一代重试。
        """
        while True:
            with self._state_lock:
                gen = self._gen
            try:
                gen.lazy.get()
            except _GenerationRetiredError:
                continue  # 装配开始时代已退役 -> 换新一代重来
            with self._state_lock:
                if not gen.retired:
                    return gen
            # 装配完成前代被退役：汇入该代停机，释放刚构建的组件
            gen.stop_once.run()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def _bootstrap(self, gen: _Generation) -> None:
        """单代单次启动尝试；仅由该代的 LazyResource 调用。

        使用局部变量构建，只有全部成功后才发布到代上，
        因此失败时不会留下任何半装配状态。
        """
        with self._state_lock:
            if gen.retired:
                raise _GenerationRetiredError("所在代已停机退役，放弃装配")
            # 新一代开始装配后，上一代的停机记录不再对后续 shutdown 生效
            self._stopping = None
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

        # 全部成功 -> 一次性发布到本代
        gen.config = config
        gen.plugins = built
        logger.info("内核装配完成，共 %d 个插件", len(built))

    def start(self) -> None:
        """同步启动（并发调用只执行一次；失败后再次调用可安全重试）。"""
        self._active_generation()

    async def astart(self) -> None:
        """异步启动（与同步入口共享同一次启动）。"""
        await asyncio.to_thread(self.start)

    # ------------------------------------------------------------------
    # 停机与重启
    # ------------------------------------------------------------------

    def _stop(self, gen: _Generation) -> None:
        """释放一代组件；仅由该代的 SettledOnce 调用一次。"""
        # 等待进行中的装配落定（成功或失败），保证已构建组件一定被释放
        while gen.lazy.state == InitState.INITIALIZING:
            try:
                gen.lazy.get()
            except Exception:
                break
        if not gen.lazy.is_ready():
            logger.info("当前代未成功启动，无需释放组件")
            return
        ShutdownSession(registry=self._registry, plugins=gen.plugins).run()

    def shutdown(self) -> None:
        """优雅停机：退役当前代并按装配逆序释放其组件。

        * 并发/重复调用共享同一次释放与同一个结果（含失败）；
        * 释放失败以 :class:`~fastapi_assembly_kernel.errors.ShutdownError`
          汇总抛出，且不会重试已执行过的释放；
        * 停机后再次 start/get 会在新一代上重新装配（安全重启）。
        """
        with self._state_lock:
            if self._stopping is not None:
                once = self._stopping
            else:
                gen = self._gen
                gen.retired = True
                self._gen = self._new_generation()
                self._stopping = gen.stop_once
                once = gen.stop_once
        once.run()

    async def ashutdown(self) -> None:
        """异步版 :meth:`shutdown`（共享同一次释放）。"""
        await asyncio.to_thread(self.shutdown)

    def restart(self) -> None:
        """停机后重新装配：使用当前最新配置，不残留上一代状态。

        停机阶段的失败（ShutdownError）会原样传播且不触发重新装配。
        """
        self.shutdown()
        self.start()

    async def arestart(self) -> None:
        """异步版 :meth:`restart`。"""
        await asyncio.to_thread(self.restart)

    # ------------------------------------------------------------------
    # 运行期配置热重载
    # ------------------------------------------------------------------

    def reload_config(self) -> "ResolvedConfig":
        """按既有来源优先级重新加载配置并整体切换。

        全部来源解析、校验通过后才用新快照原子替换当前配置；
        任何一个来源缺失/格式非法/类型不匹配，当前配置原封不动，
        错误以可区分的 ConfigError 传播（不含敏感值）。
        并发读取在任何时刻只看到切换前或切换后完整的一份。
        """
        with self._reload_lock:
            while True:
                gen = self._active_generation()
                new_config = self._loader.load()
                with self._state_lock:
                    if gen.retired:
                        continue  # 代已切换，在新一代上重试
                    gen.config = new_config  # 原子发布完整快照
                logger.info("配置热重载完成: %r", new_config)
                return new_config

    async def areload_config(self) -> "ResolvedConfig":
        """异步版 :meth:`reload_config`。"""
        return await asyncio.to_thread(self.reload_config)

    # ------------------------------------------------------------------
    # 访问
    # ------------------------------------------------------------------

    def get(self, plugin_name: str) -> Any:
        """惰性保证内核已启动，然后返回插件实例。

        插件不存在属于装配设计错误，启动成功后必然可查，否则抛 KeyError。
        """
        gen = self._active_generation()
        try:
            return gen.plugins[plugin_name]
        except KeyError:
            raise KeyError(f"插件未注册: {plugin_name!r}") from None

    async def aget(self, plugin_name: str) -> Any:
        """异步版 :meth:`get`。"""
        return await asyncio.to_thread(self.get, plugin_name)

    def is_started(self) -> bool:
        with self._state_lock:
            gen = self._gen
        return gen.lazy.is_ready()

    @property
    def config(self) -> "ResolvedConfig":
        """当前代的配置快照；访问会惰性触发启动。

        热重载只会把该属性整体替换为另一份完整快照，
        读取方不会看到半新半旧或空配置。
        """
        gen = self._active_generation()
        config = gen.config
        assert config is not None
        return config

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
