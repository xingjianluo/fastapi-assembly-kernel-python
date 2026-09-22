"""并发安全的惰性一次性初始化。

:class:`LazyResource` 包装一个无参初始化函数与可选关闭函数。

状态机::

    PENDING ──get()──▶ INITIALIZING ──成功──▶ READY
                          │  ▲
                        失败  │ 下一次 get() 重试
                          ▼  │
                        FAILED

语义保证：

* ``get()`` 首次调用时执行一次初始化，之后所有调用拿到同一个实例；
* 多线程/多入口并发触发时，只有一个线程执行初始化，其余线程在
  条件变量上等待，初始化全程只执行一次；
* 初始化抛异常时：实例绝不会被发布（任何调用方都观察不到半初始化
  结果），本次所有等待方收到包装后的 :class:`InitializationError``
  （``__cause__`` 为同一原始异常），资源转入 ``FAILED``；下一次
  ``get()`` 调用会安全地重新执行初始化；
* ``shutdown()`` 幂等：等待在途初始化结束后调用关闭函数并回到
  ``PENDING``；初始化函数自身负责清理其失败前产生的内部副作用，
  内核只保证“不发布”半成品。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import Enum
from typing import Generic, TypeVar

from .errors import InitializationError

__all__ = ["ResourceState", "LazyResource"]

_logger = logging.getLogger("assembly_kernel.lifecycle")

T = TypeVar("T")

InitFn = Callable[[], T]
ShutdownFn = Callable[[T], None]


class ResourceState(str, Enum):
    """资源生命周期状态。"""

    PENDING = "pending"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"


class LazyResource(Generic[T]):
    """线程安全的惰性一次性资源句柄。"""

    def __init__(
        self,
        name: str,
        init: InitFn[T],
        *,
        shutdown: ShutdownFn[T] | None = None,
    ) -> None:
        if not callable(init):
            raise TypeError("init 必须是可调用对象")
        self._name = name
        self._init = init
        self._shutdown = shutdown
        self._cond = threading.Condition()
        self._state: ResourceState = ResourceState.PENDING
        self._instance: T | None = None
        self._last_error: BaseException | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> ResourceState:
        """当前状态的快照（持锁读取）。"""
        with self._cond:
            return self._state

    def get(self) -> T:
        """获取资源；必要时惰性执行初始化。

        :raises InitializationError: 初始化失败（本线程为初始化者，或
            在本次初始化进行期间到达并等待）。
        """
        with self._cond:
            if self._state is ResourceState.READY:
                return self._instance  # type: ignore[return-value]
            if self._state is ResourceState.INITIALIZING:
                # 等待另一个线程完成本轮初始化，绝不自行再跑一次。
                self._wait_outcome()
                return self._consume_outcome()
            # PENDING 或 FAILED：本线程承担本轮初始化（FAILED 即重试）。
            if self._state is ResourceState.FAILED:
                _logger.info("资源上次初始化失败，开始重试 name=%s", self._name)
            self._state = ResourceState.INITIALIZING
            self._last_error = None

        # 初始化在锁外执行：慢任务不占用锁，且避免用户回调自锁。
        try:
            value = self._init()
        except Exception as exc:
            with self._cond:
                # 关键：不发布任何半成品引用，状态回到可重试的 FAILED。
                self._instance = None
                self._last_error = exc
                self._state = ResourceState.FAILED
                self._cond.notify_all()
            _logger.warning("资源初始化失败 name=%s error=%s", self._name, type(exc).__name__)
            raise InitializationError(self._name) from exc

        with self._cond:
            self._instance = value
            self._state = ResourceState.READY
            self._cond.notify_all()
        _logger.info("资源初始化完成 name=%s", self._name)
        return value

    def is_ready(self) -> bool:
        """是否已成功初始化。"""
        with self._cond:
            return self._state is ResourceState.READY

    def shutdown(self) -> None:
        """若已初始化则调用关闭函数并回到 ``PENDING``；幂等。

        有初始化在途时先等待其结束：成功则随之关闭，失败则直接回到
        可重试状态。关闭函数本身抛出的异常会传播给调用方，但无论其
        是否成功，内核侧状态都已回到 ``PENDING`` 且实例引用已清除。
        """
        with self._cond:
            if self._state is ResourceState.INITIALIZING:
                self._wait_outcome()
            if self._state is not ResourceState.READY:
                # PENDING / FAILED：没有需要关闭的已发布实例。
                self._state = ResourceState.PENDING
                self._last_error = None
                self._instance = None
                return
            instance = self._instance
            shutdown_fn = self._shutdown
            self._instance = None
            self._state = ResourceState.PENDING
            self._last_error = None

        if shutdown_fn is not None:
            try:
                shutdown_fn(instance)  # type: ignore[arg-type]
            except Exception as exc:
                raise InitializationError(self._name, "关闭失败") from exc
        _logger.info("资源已关闭 name=%s", self._name)

    def reset(self) -> None:
        """强制丢弃当前状态并回到 ``PENDING``（测试/运维用途）；幂等。

        与 :meth:`shutdown` 不同，此方法**不**调用关闭函数；在途初始化
        会先等待结束，其结果（成功或失败）被直接丢弃。
        """
        with self._cond:
            if self._state is ResourceState.INITIALIZING:
                self._wait_outcome()
            self._state = ResourceState.PENDING
            self._instance = None
            self._last_error = None

    # -- 内部（调用时必须持有条件变量锁）--------------------------------

    def _wait_outcome(self) -> None:
        """阻塞至本轮初始化结束（READY 或 FAILED）。"""
        while self._state is ResourceState.INITIALIZING:
            self._cond.wait()

    def _consume_outcome(self) -> T:
        """等待结束后读取本轮结果：成功返回实例，失败抛同源错误。"""
        if self._state is ResourceState.READY:
            return self._instance  # type: ignore[return-value]
        error = self._last_error
        raise InitializationError(self._name) from error
