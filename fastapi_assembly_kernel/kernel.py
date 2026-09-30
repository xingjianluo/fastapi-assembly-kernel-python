"""应用装配内核：分层配置 + 依赖注入式插件 + 可重启的生命周期状态机。

生命周期状态机（``threading.Condition`` 保护）::

    STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED -> ...

* 启动（:meth:`start`）：多个入口并发触发只装配一次；本轮等待方
  共享同一结果（成功或同一个失败异常）；失败后下一次调用从头重试，
  不保留任何半装配状态（装配在局部变量中构建，全部成功才一次性发布）。
* 停机（:meth:`stop`）：按装配顺序的严格逆序调用各插件的 teardown
  （依赖别人的先释放，被别人依赖的后释放）；停机进行中再次触发时，
  后来者**并入当前这一轮**——等待该轮结束并拿到同一结果，绝不发起
  第二轮释放（即使停机结束瞬间内核已被其他入口重新拉起）；单个组件
  释放失败不跳过其他组件，失败汇总为 :class:`ShutdownError` 抛出。
* 停机与拉起并发：停机期间触发的 start/惰性访问在停机彻底结束后，
  按当前最新配置装配出全新的一代实例；装配发布与等待方放行是同一个
  临界区，等待方拿到的一定是完整可用、且不会再被旧停机轮释放的实例。
* 重启：停机后再次 start 即重新装配——配置重新解析（拿到当前最新
  配置），插件全部重建，不残留上一轮运行的实例或半成品引用；
  重启进行中的调用方要么拿到重启后完整可用的状态，要么收到失败。
* 运行期配置热重载（:meth:`reload_config`）：按既有来源优先级重新
  解析全部来源，只有全部解析、校验通过才一次性原子切换；任一来源
  缺失/格式非法/类型不匹配都保持当前配置原封不动，并以可区分的
  ConfigError 子类抛出。配置对象不可变、切换是单次引用替换，
  并发读取要么看到切换前完整的一份，要么看到切换后完整的一份。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from .errors import (
    InitializationFailedError,
    KernelError,
    KernelNotRunningError,
    ShutdownError,
)

if TYPE_CHECKING:
    from .config import LayeredConfigLoader, ResolvedConfig
    from .registry import PluginRegistry

__all__ = ["AssemblyKernel", "get_kernel", "set_kernel", "KernelState"]

logger = logging.getLogger("fastapi_assembly_kernel.kernel")

# 进程内默认内核（由 FastAPI 集成或应用入口通过 set_kernel 设置）
_default_kernel: "AssemblyKernel | None" = None


class KernelState:
    """内核生命周期状态。"""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class _StopRound:
    """一轮停机的身份与结果载体。

    并入本轮的调用方持有的是同一个对象：在条件变量上等待"活动轮
    不再是自己并入的这一轮"，轮次结束后读取同一个 :attr:`error`
    （成功为 None）。以对象身份而非状态名判定等待结束，这样停机
    刚结束、内核已被其他入口重新拉起时，后来者也不会误把新一代
    实例再释放一遍。

    :param observer: 可选观测回调（仅供测试做确定性时序编排，
        在持有内核条件变量时同步调用，**必须非阻塞**）；事件：
        ``"stop_round_joined"``（有调用方并入进行中的一轮）。
    """

    __slots__ = ("error", "_observer")

    def __init__(self, observer: "Callable[[str], None] | None" = None) -> None:
        self.error: ShutdownError | None = None
        self._observer = observer

    def finish(self, error: ShutdownError | None) -> None:
        """写入本轮结果（在持有条件变量的临界区内调用）。"""
        self.error = error


class AssemblyKernel:
    """业务模块复用的应用装配内核（可启动、停机、热重载、重启）。"""

    def __init__(
        self,
        *,
        config_loader: "LayeredConfigLoader",
        registry: "PluginRegistry",
        observer: "Callable[[str], None] | None" = None,
    ) -> None:
        self._loader = config_loader
        self._registry = registry
        self._observer = observer
        self._cond = threading.Condition()
        self._reload_lock = threading.Lock()
        self._state: str = KernelState.STOPPED
        self._plugins: dict[str, Any] = {}
        self._order: list[str] = []
        self._config: "ResolvedConfig | None" = None
        self._start_error: BaseException | None = None
        self._stop_error: ShutdownError | None = None
        # 进行中的停机轮：非 None 表示有且仅有一轮停机正在释放；
        # 后来的 stop() 并入这一轮，而不是发起新一轮
        self._active_stop: _StopRound | None = None

    def _notify(self, event: str) -> None:
        """观测钩子（测试用）；生产环境 observer 为 None，零开销。"""
        if self._observer is not None:
            self._observer(event)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def _bootstrap(self) -> None:
        """单次装配尝试；仅在 STARTING 状态下由唯一发起方执行。

        使用局部变量构建，只有全部成功后才发布到实例属性，
        因此失败时实例上不会留下任何半装配状态。
        """
        logger.info("开始装配内核")
        config = self._loader.load()
        logger.info("配置解析完成: %r", config)

        order = self._registry.resolve_order()
        logger.info("插件装配顺序: %s", order)

        from .registry import FactoryContext

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
        self._order = order
        logger.info("内核装配完成，共 %d 个插件", len(built))

    def start(self) -> None:
        """同步启动（并发调用只装配一次；失败后再次调用可安全重试）。

        停机后进行中的 start 会等待停机完成再重新装配（安全重启）。
        """
        with self._cond:
            waited = False
            while True:
                if self._state == KernelState.RUNNING:
                    return
                if self._state in (KernelState.STARTING, KernelState.STOPPING):
                    waited = True
                    self._cond.wait()
                    continue
                # STOPPED：本轮启动失败的等待方共享失败，不代为重试
                if self._start_error is not None and waited:
                    raise self._start_error
                # PENDING 启动，或失败/停机后的一次全新触发
                self._state = KernelState.STARTING
                self._start_error = None
                self._stop_error = None  # 新一轮运行，既往停机结果不再相关
                break

        # 在锁外装配，避免长时间持锁阻塞其他调用方
        try:
            self._bootstrap()
        except BaseException as exc:
            # 配置/依赖等内核错误本身即可区分，原样传播不做包装；
            # 其余异常统一包装为 InitializationFailedError。
            if isinstance(exc, KernelError):
                error: BaseException = exc
            else:
                wrapped = InitializationFailedError(
                    f"内核装配失败: {type(exc).__name__}: {exc}"
                )
                wrapped.__cause__ = exc
                error = wrapped
            with self._cond:
                self._state = KernelState.STOPPED
                self._start_error = error
                self._cond.notify_all()
            if isinstance(exc, Exception) and not isinstance(exc, KernelError):
                raise error from exc
            raise
        else:
            with self._cond:
                self._state = KernelState.RUNNING
                self._cond.notify_all()

    async def astart(self) -> None:
        """异步启动（与同步入口共享同一次装配）。"""
        await asyncio.to_thread(self.start)

    # ------------------------------------------------------------------
    # 停机
    # ------------------------------------------------------------------

    def _teardown_all(self) -> ShutdownError | None:
        """按装配顺序的严格逆序释放全部组件；失败不跳过其他组件。"""
        plugins = self._plugins
        order = self._order
        logger.info("开始优雅停机，释放顺序: %s", list(reversed(order)))
        failures: dict[str, BaseException] = {}
        for name in reversed(order):
            spec = self._registry.get(name)
            if spec.teardown is None:
                continue
            try:
                spec.teardown(plugins[name])
                logger.debug("组件 %r 已释放", name)
            except Exception as exc:  # noqa: BLE001 - 释放失败必须被收集而非传播
                # 只记录组件名与异常类型名，不记录异常文本：
                # teardown 抛出的异常消息可能携带密钥等敏感内容
                logger.error("组件 %r 释放失败: %s", name, type(exc).__name__)
                failures[name] = exc
        if failures:
            return ShutdownError(failures)
        logger.info("优雅停机完成，共释放 %d 个组件", len(order))
        return None

    def stop(self) -> None:
        """优雅停机：后来者并入进行中的同一轮，所有调用方拿同一结果。

        * 停机进行中再次触发：后来者**并入当前这一轮**，等待该轮
          结束并拿到同一个结果（成功或同一个 :class:`ShutdownError`），
          不会等结束后再发起第二轮释放——即使本轮结束的瞬间内核已被
          其他入口重新拉起；
        * 释放按逆装配顺序执行，单个组件失败不跳过其他组件；
        * 有组件释放失败时，本轮所有调用方（含并入方）收到同一个
          :class:`ShutdownError`（含逐组件失败明细）；
        * 已成功停机后再次调用为幂等空操作。
        """
        with self._cond:
            while True:
                active = self._active_stop
                if active is not None:
                    # 一轮停机正在进行：并入它，等这一轮结束。
                    # 以轮次对象身份判定结束：等待期间内核即使已被
                    # 别的入口重新拉起，本调用方也只拿本轮结果，绝不
                    # 把新一代实例再释放一遍。
                    self._notify("stop_round_joined")
                    self._cond.wait_for(lambda: self._active_stop is not active)
                    if active.error is not None:
                        raise active.error
                    return
                if self._state == KernelState.STOPPED:
                    if self._stop_error is not None:
                        raise self._stop_error
                    return
                if self._state == KernelState.STARTING:
                    # 一轮装配正在进行：等它有结果再决定——
                    # 启动成功则停这一代；启动失败则无物可释放，直接返回
                    self._cond.wait()
                    continue
                # RUNNING -> 由本调用方执行唯一一轮释放
                round_ = _StopRound(self._observer)
                self._active_stop = round_
                self._state = KernelState.STOPPING
                break

        error = self._teardown_all()
        with self._cond:
            # 无论成功与否都丢弃上一轮的实例与配置引用，
            # 保证重启不残留上一次运行的状态或半成品引用
            self._plugins = {}
            self._order = []
            self._config = None
            self._state = KernelState.STOPPED
            self._stop_error = error
            # 先收敛轮次（写入结果、唤醒并入方）再广播状态变更
            self._active_stop = None
            round_.finish(error)
            self._cond.notify_all()
        if error is not None:
            raise error

    async def astop(self) -> None:
        """异步停机（与同步入口共享同一轮释放）。"""
        await asyncio.to_thread(self.stop)

    # ------------------------------------------------------------------
    # 运行期配置热重载
    # ------------------------------------------------------------------

    def reload_config(self) -> "ResolvedConfig":
        """按既有来源优先级重新解析配置并整体原子切换。

        只有所有来源都解析、校验通过才切换；任一来源失败都保持当前
        配置原封不动，可区分的 ConfigError 子类原样抛出（错误信息
        只含字段名/来源名，绝不含敏感值）。同一时刻只进行一轮重载。
        """
        with self._reload_lock:
            with self._cond:
                if self._state != KernelState.RUNNING:
                    raise KernelNotRunningError(
                        f"内核未在运行（状态 {self._state}），无法重载配置"
                    )
            # 解析失败 -> ConfigError 原样抛出，当前配置原封不动
            new_config = self._loader.load()
            with self._cond:
                if self._state != KernelState.RUNNING:
                    raise KernelNotRunningError(
                        "配置重载期间内核已停机，新配置未发布"
                    )
                # 单次引用替换：并发读取要么看到旧的完整一份，要么看到新的
                self._config = new_config
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
        with self._cond:
            return self._state == KernelState.RUNNING

    @property
    def state(self) -> str:
        with self._cond:
            return self._state

    @property
    def config(self) -> "ResolvedConfig":
        """访问配置会惰性触发启动。

        返回的是不可变快照：热重载切换后，已拿到的旧快照不受影响，
        新的读取看到新的完整一份。
        """
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
