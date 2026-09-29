"""并发安全的惰性一次性初始化原语。

语义：

* 多个调用方（多线程，或通过 :meth:`aget` 进入的多个 asyncio 任务）
  同时触发时，initializer 只执行一次；其余调用方阻塞/等待结果；
* 初始化成功前任何调用方都拿不到实例——等待方要么拿到同一成功结果，
  要么收到同一个失败错误，绝不观察到半初始化对象；
* initializer 失败时：已持有的半成品引用被丢弃、状态回到 FAILED，
  下一次调用重新执行 initializer（安全重试），不会残留部分状态；
* 异步入口 :meth:`aget` 委托到同一套线程原语（``asyncio.to_thread``），
  因此线程与协程混合触发时仍然保证"只初始化一次"。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable, Generic, TypeVar

from .errors import InitializationFailedError

T = TypeVar("T")

__all__ = ["LazyResource", "InitState", "SettledOnce"]


class InitState:
    PENDING = "pending"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"


class LazyResource(Generic[T]):
    """惰性一次性初始化资源。

    passthrough：声明为"本身即可区分"的异常类型（如配置/依赖错误），
    这些异常不被包装为 InitializationFailedError，而是原样传播给
    发起方与所有等待方；其余异常统一包装为 InitializationFailedError。
    """

    def __init__(
        self,
        initializer: Callable[[], T],
        *,
        name: str = "resource",
        passthrough: tuple[type[BaseException], ...] = (),
    ) -> None:
        if not callable(initializer):
            raise ValueError("initializer 必须可调用")
        self._initializer = initializer
        self._name = name
        self._passthrough = passthrough
        self._cond = threading.Condition()
        self._state: str = InitState.PENDING
        self._value: T | None = None
        self._error: BaseException | None = None

    # ------------------------------------------------------------------

    def get(self) -> T:
        """同步获取；首个调用方执行初始化，其余调用方等待同一结果。

        失败广播：本轮在 INITIALIZING 期间等待的调用方全部收到同一个
        失败异常（passthrough 类型原样，否则为
        :class:`InitializationFailedError`）；之后状态回到 FAILED，
        由下一次全新的 :meth:`get` 触发重试（等待方不会代为重试）。
        """
        with self._cond:
            waited = False
            while True:
                if self._state == InitState.READY:
                    return self._value  # type: ignore[return-value]
                if self._state == InitState.INITIALIZING:
                    waited = True
                    self._cond.wait()
                    continue
                if self._state == InitState.FAILED and waited:
                    # 本轮失败的等待方：共享失败，不触发重试
                    raise self._error  # type: ignore[misc]
                # PENDING，或 FAILED 之后的一次全新触发 -> 开始/重试
                self._state = InitState.INITIALIZING
                self._value = None
                self._error = None
                break

        # 在锁外执行初始化，避免长时间持锁阻塞其他调用方
        try:
            value = self._initializer()
        except BaseException as exc:
            # 丢弃半成品引用，状态收敛为 FAILED，唤醒全部等待方共享失败。
            # passthrough 类型原样传播；其余包装为 InitializationFailedError。
            if isinstance(exc, self._passthrough):
                error: BaseException = exc
            else:
                wrapped = InitializationFailedError(
                    f"资源 {self._name!r} 初始化失败: {type(exc).__name__}: {exc}"
                )
                wrapped.__cause__ = exc
                error = wrapped
            with self._cond:
                self._state = InitState.FAILED
                self._value = None
                self._error = error
                self._cond.notify_all()
            if isinstance(exc, Exception) and not isinstance(exc, self._passthrough):
                raise error from exc
            raise
        else:
            with self._cond:
                self._state = InitState.READY
                self._value = value
                self._error = None
                self._cond.notify_all()
            return value

    async def aget(self) -> T:
        """异步获取；与所有同步/异步调用共享同一次初始化。"""
        return await asyncio.to_thread(self.get)

    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._cond:
            return self._state

    def is_ready(self) -> bool:
        with self._cond:
            return self._state == InitState.READY


class SettledOnce:
    """只执行一次的收尾原语（用于停机释放）。

    与 :class:`LazyResource` 的差别：失败后**不重试**。无论并发还是
    先后调用，动作至多执行一次，所有调用方共享同一个结果——
    成功则全部返回，失败则全部收到同一个异常实例。
    """

    def __init__(self, action: Callable[[], None], *, name: str = "settled") -> None:
        if not callable(action):
            raise ValueError("action 必须可调用")
        self._action = action
        self._name = name
        self._cond = threading.Condition()
        self._running = False
        self._done = False
        self._error: BaseException | None = None

    def run(self) -> None:
        """执行（或等待/共享）唯一一次动作。"""
        with self._cond:
            while self._running:
                self._cond.wait()
            if self._done:
                if self._error is not None:
                    raise self._error
                return
            self._running = True
        try:
            self._action()
        except BaseException as exc:
            with self._cond:
                self._running = False
                self._done = True
                self._error = exc
                self._cond.notify_all()
            raise
        with self._cond:
            self._running = False
            self._done = True
            self._cond.notify_all()

    def is_done(self) -> bool:
        with self._cond:
            return self._done
