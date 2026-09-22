"""并发安全的惰性一次性初始化。"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import InitializationFailedError, InitState, LazyResource


def test_concurrent_callers_trigger_single_initialization(case_log):
    threads = 16
    case_log(
        "并发一次性初始化",
        f"{threads} 个线程同时调用 get()",
        "initializer 只执行一次，所有调用方拿到同一实例",
    )
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def initializer():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.2)  # 放大并发窗口
        return {"id": calls}

    resource = LazyResource(initializer, name="svc")
    results: list[object] = []

    def worker():
        barrier.wait()
        results.append(resource.get())

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda _: worker(), range(threads)))

    assert calls == 1
    assert len(results) == threads
    assert all(r is results[0] for r in results)
    assert resource.state == InitState.READY


def test_failure_broadcast_and_safe_retry(case_log):
    waiters = 8
    case_log(
        "失败广播与安全重试",
        f"{waiters} 个线程并发触发，首次 initializer 抛 RuntimeError",
        "本轮全部调用方收到同一个 InitializationFailedError；"
        "状态收敛为 FAILED；之后新调用重试成功",
    )
    attempts = 0
    release = threading.Event()

    def flaky():
        nonlocal attempts
        attempts += 1
        release.wait(timeout=5)  # 确保所有等待方都已进入等待
        if attempts == 1:
            raise RuntimeError("boom")
        return "recovered"

    resource = LazyResource(flaky, name="flaky")
    barrier = threading.Barrier(waiters)
    errors: dict[int, InitializationFailedError] = {}

    def worker(i: int):
        barrier.wait()
        try:
            resource.get()
        except InitializationFailedError as exc:
            errors[i] = exc

    with ThreadPoolExecutor(waiters) as pool:
        futures = [pool.submit(worker, i) for i in range(waiters)]
        time.sleep(0.2)
        release.set()
        for f in futures:
            f.result()

    assert attempts == 1
    assert len(errors) == waiters
    shared = next(iter(errors.values()))
    assert all(e is shared for e in errors.values())
    assert isinstance(shared.__cause__, RuntimeError)
    assert resource.state == InitState.FAILED
    assert not resource.is_ready()

    # 失败之后的新触发 -> 安全重试
    assert resource.get() == "recovered"
    assert attempts == 2
    assert resource.state == InitState.READY


def test_no_partial_state_observable(case_log):
    case_log(
        "无半初始化状态",
        "initializer 内部先把半成品写入外部变量再抛错",
        "失败后 resource 不持有任何实例引用，is_ready 为 False",
    )
    leaked: dict[str, object] = {}

    def bad_initializer():
        partial = {"stage": "half-built"}
        leaked["partial"] = partial  # 模拟工厂内部泄漏的半成品
        raise ValueError("cannot finish")

    resource = LazyResource(bad_initializer, name="bad")
    with pytest.raises(InitializationFailedError):
        resource.get()
    assert resource.state == InitState.FAILED
    assert not resource.is_ready()
    # 资源自身不缓存半成品（外部变量只是测试道具）
    assert getattr(resource, "_value", None) is None


def test_async_concurrent_initialization(case_log):
    tasks = 12
    case_log(
        "异步并发初始化",
        f"{tasks} 个 asyncio 任务同时 aget()",
        "与同步入口共享同一次初始化，只执行一次",
    )
    calls = 0

    def slow():
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return 42

    async def main():
        resource = LazyResource(slow, name="async-svc")
        values = await asyncio.gather(*[resource.aget() for _ in range(tasks)])
        assert values == [42] * tasks
        # 线程入口与协程入口共享同一次初始化
        t = threading.Thread(target=resource.get)
        t.start()
        t.join()
        return resource

    resource = asyncio.run(main())
    assert calls == 1
    assert resource.is_ready()


def test_retry_after_failure_is_clean(case_log):
    case_log(
        "重试不残留状态",
        "initializer 第 1、2 次失败，第 3 次成功",
        "每次失败状态都收敛为 FAILED，最终成功后状态 READY 且只保留成功结果",
    )
    attempts = 0

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError(f"fail #{attempts}")
        return "third-time-lucky"

    resource = LazyResource(flaky, name="retry")
    for _ in range(2):
        with pytest.raises(InitializationFailedError):
            resource.get()
        assert resource.state == InitState.FAILED
    assert resource.get() == "third-time-lucky"
    assert attempts == 3
    assert resource.state == InitState.READY
