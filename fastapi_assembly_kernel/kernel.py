"""应用装配内核：分层配置 + 依赖注入式插件 + 可重启的生命周期状态机。

生命周期状态机（``threading.Condition`` 保护）::

    STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED -> ...

* 启动（:meth:`start`）：多个入口并发触发只装配一次；本轮等待方
  共享同一结果（成功或同一个失败异常）；失败后下一次调用从头重试，
  不保留任何半装配状态（装配在局部变量中构建，全部成功才一次性发布）。
* 停机（:meth:`stop`）：按装配顺序的严格逆序调用各插件的 teardown
  （依赖别人的先释放，被别人依赖的后释放）；停机以"轮次"为单位，
  停机进行中再触发的停机会**绑定并并入当前这一轮**，与发起方拿到
  同一个轮次结果（成功或同一个 :class:`ShutdownError`）；即使该轮
  结束后内核已被其他入口重新拉起，并入方也只返回本轮结果，绝不会
  对新一代发起第二轮释放。单个组件释放失败不跳过其他组件。
* 停机与重启并发：停机进行中触发的 start/get/config 等待本轮停机
  彻底结束后，按当前最新配置装配出完整可用的新一代；实例的发布与
  取用在同一个临界区完成，调用方要么拿到完整可用的新实例，要么
  收到明确失败，绝不会拿到刚被本轮停机释放的实例或观察到中间态。
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
from typing import TYPE_CHECKING, Any, Callable, TypeVar

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

T = TypeVar("T")

# 进程内默认内核（由 FastAPI 集成或应用入口通过 set_kernel 设置）
_default_kernel: "AssemblyKernel | None" = None


class KernelState:
    """内核生命周期状态。"""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class _StopRound:
    """一轮停机的身份与结果盒子。

    * 发起方在进入 STOPPING 时创建一个新的轮次对象；停机进行中到达
      的所有 stop 调用方绑定的都是**同一个对象**；
    * 释放结束后在锁内写入结果并唤醒全部等待方；
    * 绑定到某轮的调用方只读取本轮结果。这样即使该轮结束后内核已被
      其他入口重新拉起（甚至新一代又开始停机），并入旧轮的调用方也
      只会返回旧轮结果，绝不会对新一代发起第二轮释放。
    """

    __slots__ = ("done", "error")

    def __init__(self) -> None:
        self.done: bool = False
        self.error: ShutdownError | None = None


class AssemblyKernel:
    """业务模块复用的应用装配内核（可启动、停机、热重载、重启）。"""

    def __init__(
        self,
        *,
        config_loader: "LayeredConfigLoader",
        registry: "PluginRegistry",
    ) -> None:
        self._loader = config_loader
        self._registry = registry
        self._cond = threading.Condition()
        self._reload_lock = threading.Lock()
        self._state: str = KernelState.STOPPED
        self._plugins: dict[str, Any] = {}
        self._order: list[str] = []
        self._config: "ResolvedConfig | None" = None
        self._start_error: BaseException | None = None
        self._stop_error: ShutdownError | None = None
        # 最近一轮停机的结果盒子：RUNNING 时为上一轮的旧盒子，
        # 进入新一轮 STOPPING 时会被替换为全新的对象
        self._stop_round: _StopRound | None = None

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

    def _start_and_select(self, select: Callable[[], T]) -> T:
        """启动状态机 + RUNNING 临界区内取值的统一实现。

        ``start``/``get``/``config`` 共用同一个等待循环，区别只在到达
        RUNNING 时要取走什么：

        * 停机进行中到达的调用方等待本轮停机彻底结束，随后要么成为
          新一代的唯一装配方，要么并入同一轮启动共享结果；
        * ``select`` 在持锁状态下执行，因此"装配结果发布"与"调用方
          取走实例"之间不存在空隙——等待方拿到的一定是完整可用的
          新一代实例，不会拿到刚被停机释放的实例。
        """
        with self._cond:
            waited = False
            while True:
                if self._state == KernelState.RUNNING:
                    return select()
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
                # 同一临界区内取走完整结果，避免发布后、返回前被停机穿插
                return select()

    def start(self) -> None:
        """同步启动（并发调用只装配一次；失败后再次调用可安全重试）。

        停机进行中到达的 start 会等待停机彻底结束，再按当前最新配置
        装配新一代（安全重启），不会与正在进行的停机轮次交叉。
        """
        self._start_and_select(lambda: None)

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
                # 日志只记录组件名与异常类型：teardown 异常消息可能携带
                # 密钥等敏感内容（如连接串、令牌），不进入日志；
                # 原始异常保留在 ShutdownError.failures 中供调用方按
                # 组件名程序化处理，汇总异常消息同样只含名字与类型。
                logger.error(
                    "组件 %r 释放失败（原始异常已收集到停机汇总，"
                    "消息不写入日志）: %s",
                    name,
                    type(exc).__name__,
                )
                failures[name] = exc
        if failures:
            return ShutdownError(failures)
        logger.info("优雅停机完成，共释放 %d 个组件", len(order))
        return None

    def stop(self) -> None:
        """优雅停机：同一轮停机只真正释放一轮，后来者并入在跑的轮次。

        * 释放按逆装配顺序执行，单个组件失败不跳过其他组件；
        * 停机进行中到达的调用方**绑定并并入当前这一轮**，与发起方
          拿到同一个轮次结果（成功，或同一个 :class:`ShutdownError`，
          含逐组件失败明细）；即使本轮结束后内核已被其他入口重新拉起，
          并入方也只返回本轮结果，绝不会对新一代发起第二轮释放；
        * 已成功停机后再调用为幂等空操作；停机失败后（未重启前）的
          stop() 收到同一个 :class:`ShutdownError`。
        """
        with self._cond:
            while True:
                if self._state == KernelState.RUNNING:
                    # 由本调用方发起唯一一轮释放：创建全新的轮次盒子，
                    # 后来者绑定的就是这一个
                    round_ = _StopRound()
                    self._stop_round = round_
                    self._state = KernelState.STOPPING
                    break
                if self._state == KernelState.STOPPING:
                    # 并入正在进行的这一轮：绑定后只等待/读取本轮结果
                    round_ = self._stop_round  # type: ignore[assignment]
                    assert round_ is not None
                    while not round_.done:
                        self._cond.wait()
                    if round_.error is not None:
                        raise round_.error
                    return
                if self._state == KernelState.STARTING:
                    # 启动未完成时的停机：等这一代命运落定。装配失败则
                    # 内核回到 STOPPED，视为已停机直接返回；装配成功则
                    # 由本调用方发起停机（走上方 RUNNING 分支）。
                    self._cond.wait()
                    continue
                # STOPPED：从未启动 / 已成功停机 / 停机失败 / 启动失败
                if self._stop_error is not None:
                    raise self._stop_error
                return

        error = self._teardown_all()
        with self._cond:
            # 无论成功与否都丢弃上一轮的实例与配置引用，
            # 保证重启不残留上一次运行的状态或半成品引用
            self._plugins = {}
            self._order = []
            self._config = None
            self._state = KernelState.STOPPED
            self._stop_error = error
            round_.done = True
            round_.error = error
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

        停机进行中到达的 get 等待本轮停机结束后取走**新一代**的完整
        实例；实例取用在状态锁临界区内完成，因此不会拿到刚被本轮
        停机释放的实例。插件未注册属于装配设计错误，启动成功后必然
        可查，否则抛 KeyError。
        """
        return self._start_and_select(lambda: self._select_plugin(plugin_name))

    async def aget(self, plugin_name: str) -> Any:
        """异步版 :meth:`get`。"""
        return await asyncio.to_thread(self.get, plugin_name)

    def _select_plugin(self, plugin_name: str) -> Any:
        """在持有状态锁时取走已发布的插件实例（调用方保证 RUNNING）。"""
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
        新的读取看到新的完整一份。停机进行中到达的读取会等待停机
        结束后取走新一代配置，不会读到已被清空的中间态。
        """
        return self._start_and_select(lambda: self._config)  # type: ignore[return-value]

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
