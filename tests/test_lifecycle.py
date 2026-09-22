"""惰性生命周期测试：一次性初始化、并发安全、失败广播与安全重试。"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from assembly_kernel import InitializationError, LazyResource, ResourceState

log = logging.getLogger("tests.lifecycle")


class TestLazyAndOnce:
    def test_not_initialized_until_first_get(self):
        calls = []
        res = LazyResource("r", lambda: calls.append(1) or "v")
        assert res.state is ResourceState.PENDING
        assert calls == []
        assert res.get() == "v"
        assert calls == [1]

    def test_repeated_gets_return_same_instance_and_init_once(self):
        counter = {"n": 0}

        def init():
            counter["n"] += 1
            return object()

        res = LazyResource("r", init)
        first = res.get()
        assert res.state is ResourceState.READY
        assert all(res.get() is first for _ in range(10))
        assert counter["n"] == 1

    def test_concurrent_triggers_run_init_exactly_once(self, case_log):
        counter = {"n": 0}
        release = threading.Event()

        def init():
            counter["n"] += 1
            release.wait(2)  # 阻塞到所有线程都进入 get()，最大化竞态
            return object()

        res = LazyResource("r", init)
        results: list[object] = []
        errors: list[BaseException] = []
        start = threading.Event()

        def worker():
            start.wait(2)
            try:
                results.append(res.get())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        case_log.input(threads=16, init="阻塞 0.2s 后返回")
        start.set()
        time.sleep(0.2)
        release.set()
        for t in threads:
            t.join()

        assert errors == []
        assert counter["n"] == 1
        assert len({id(v) for v in results}) == 1
        assert res.is_ready()
        case_log.actual(f"init 调用 {counter['n']} 次，16 线程拿到同一实例")


class TestFailureAndRetry:
    def test_failure_wrapped_with_cause(self):
        def boom():
            raise RuntimeError("boom")

        res = LazyResource("r", boom)
        with pytest.raises(InitializationError) as exc:
            res.get()
        assert isinstance(exc.value.__cause__, RuntimeError)
        assert res.state is ResourceState.FAILED

    def test_concurrent_failure_broadcast_to_all_waiters_same_cause(self, case_log):
        counter = {"n": 0}

        def boom():
            counter["n"] += 1
            time.sleep(0.2)
            raise ValueError("kaboom")

        res = LazyResource("r", boom)
        errors: list[InitializationError] = []
        values: list[object] = []
        states_during: list[ResourceState] = []
        start = threading.Event()

        def worker():
            start.wait(2)
            try:
                values.append(res.get())
            except InitializationError as exc:
                errors.append(exc)

        def observer():
            start.wait(2)
            time.sleep(0.05)
            states_during.append(res.state)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        obs = [threading.Thread(target=observer) for _ in range(4)]
        for t in threads + obs:
            t.start()
        case_log.input(workers=12, observers=4, init="0.2s 后抛 ValueError")
        start.set()
        for t in threads + obs:
            t.join()

        assert counter["n"] == 1, "初始化只能执行一次"
        assert len(errors) == 12 and values == []
        # 所有等待方拿到的是同一原始异常，失败原因一致、可区分
        assert len({id(e.__cause__) for e in errors}) == 1
        assert all(isinstance(e.__cause__, ValueError) for e in errors)
        assert all(s is ResourceState.INITIALIZING for s in states_during)
        assert res.state is ResourceState.FAILED
        case_log.actual(
            "12 个调用方均失败且同源；观察窗口内状态恒为 INITIALIZING，无半初始化可见"
        )

    def test_no_partial_instance_observable(self):
        """失败后旧实例引用必须为空，任何调用方都拿不到半成品。"""
        leaked = []

        def partial():
            leaked.append("partial-state")
            raise RuntimeError("nope")

        res = LazyResource("r", partial)
        for _ in range(3):
            with pytest.raises(InitializationError):
                res.get()
        assert res.state is ResourceState.FAILED
        # 内部实例槽位从未发布过半成品
        assert res._instance is None  # noqa: SLF001

    def test_retry_after_failure_succeeds(self, case_log):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("first fails")
            return "ok"

        res = LazyResource("r", flaky)
        with pytest.raises(InitializationError):
            res.get()
        assert res.state is ResourceState.FAILED
        case_log.input(attempts=attempts["n"])
        assert res.get() == "ok"
        assert res.state is ResourceState.READY
        assert res.get() == "ok"
        assert attempts["n"] == 2
        case_log.actual("失败后第 2 次初始化成功，之后复用成功实例")

    def test_waiter_spans_failed_epoch_then_gets_retry_result(self, case_log):
        """第一轮失败时到达的等待方收到失败；其紧接着的 get 参与/等待重试并成功。"""
        gate = threading.Event()

        def first_init():
            time.sleep(0.15)
            raise RuntimeError("epoch-1 fail")

        res = LazyResource("r", first_init)
        epoch1_errors: list[InitializationError] = []

        def failing_getter():
            try:
                res.get()
            except InitializationError as exc:
                epoch1_errors.append(exc)

        t1 = threading.Thread(target=failing_getter)
        t1.start()
        time.sleep(0.05)  # t1 进入等待
        t1.join()
        assert len(epoch1_errors) == 1

        # 现在安装成功的初始化器，并让多线程同时重试
        def second_init():
            gate.wait(2)
            return "recovered"

        res._init = second_init  # noqa: SLF001
        values: list[str] = []
        start = threading.Event()

        def retry_worker():
            start.wait(2)
            values.append(res.get())

        threads = [threading.Thread(target=retry_worker) for _ in range(8)]
        for t in threads:
            t.start()
        start.set()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join()
        assert values == ["recovered"] * 8
        assert res.state is ResourceState.READY
        case_log.actual("失败纪元的等待方与新一轮重试线程最终观察状态一致：READY")


class TestShutdownAndReset:
    def test_shutdown_calls_callback_and_allows_reinit(self):
        closed: list[str] = []
        counter = {"n": 0}

        def init():
            counter["n"] += 1
            return f"v{counter['n']}"

        res = LazyResource("r", init, shutdown=lambda v: closed.append(v))
        assert res.get() == "v1"
        res.shutdown()
        assert closed == ["v1"]
        assert res.state is ResourceState.PENDING
        assert res.get() == "v2"

    def test_shutdown_is_idempotent_when_pending(self):
        closed = []
        res = LazyResource("r", lambda: 1, shutdown=lambda v: closed.append(v))
        res.shutdown()
        res.shutdown()
        assert closed == []

    def test_shutdown_waits_for_inflight_init(self, case_log):
        gate = threading.Event()

        def slow_init():
            gate.wait(2)
            return "v"

        closed: list[str] = []
        res = LazyResource("r", slow_init, shutdown=lambda v: closed.append(v))
        getter = threading.Thread(target=res.get)
        getter.start()
        time.sleep(0.1)
        closer = threading.Thread(target=res.shutdown)
        closer.start()
        time.sleep(0.1)
        assert res.state is ResourceState.INITIALIZING
        gate.set()
        getter.join()
        closer.join()
        assert closed == ["v"]
        assert res.state is ResourceState.PENDING
        case_log.actual("在途初始化期间调用 shutdown：等待完成后关闭，状态回到 PENDING")

    def test_shutdown_after_failed_init_stays_retriable(self):
        def boom():
            raise RuntimeError("x")

        res = LazyResource("r", boom)
        with pytest.raises(InitializationError):
            res.get()
        res.shutdown()  # 不应调用关闭函数，也不应抛错
        assert res.state is ResourceState.PENDING
        res._init = lambda: "ok"  # noqa: SLF001
        assert res.get() == "ok"

    def test_shutdown_callback_error_wrapped_but_state_reset(self):
        res = LazyResource(
            "r", lambda: "v", shutdown=lambda v: (_ for _ in ()).throw(OSError("close fail"))
        )
        res.get()
        with pytest.raises(InitializationError) as exc:
            res.shutdown()
        assert isinstance(exc.value.__cause__, OSError)
        assert res.state is ResourceState.PENDING

    def test_reset_discards_without_callback(self):
        closed = []
        res = LazyResource("r", lambda: "v", shutdown=lambda v: closed.append(v))
        res.get()
        res.reset()
        assert closed == []
        assert res.state is ResourceState.PENDING
        res.reset()  # 幂等

    def test_reset_is_idempotent_and_init_runs_again(self):
        counter = {"n": 0}
        res = LazyResource("r", lambda: counter.__setitem__("n", counter["n"] + 1) or counter["n"])
        res.get()
        res.reset()
        assert res.get() == 2
