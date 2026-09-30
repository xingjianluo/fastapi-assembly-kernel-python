"""停机进行中再触发停机、停机与重新拉起并发的规则测试。

时序编排全部使用 ``threading.Event`` / ``Barrier`` / Future 等确定性
同步手段（统计进入 ``Condition.wait`` 的等待方数量作为放行点），
不使用 sleep 赌调度。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigValidationError,
    FieldSpec,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
    ShutdownError,
)
from fastapi_assembly_kernel.config import FileSource

SECRET = "s3cr3t-token-do-not-leak"


# ----------------------------------------------------------------------
# 测试道具
# ----------------------------------------------------------------------


class _WaitCounter:
    """替换内核 Condition.wait，统计已排队的等待方线程数。

    达到期望数量时发信号——此时可以确定所有目标调用方都已经进入
    内核状态机的等待（停机已在进行、其余调用方全部并入/等待重启），
    再放行停机的 teardown，避免依赖调度时序。
    """

    def __init__(self, kernel, expected: int) -> None:
        self._kernel = kernel
        self._expected = expected
        self._seen = 0
        self._lock = threading.Lock()
        self.all_waiting = threading.Event()
        self._orig = kernel._cond.wait

    def __enter__(self) -> "_WaitCounter":
        orig = self._orig

        def counting_wait(timeout=None):
            with self._lock:
                self._seen += 1
                if self._seen >= self._expected:
                    self.all_waiting.set()
            return orig(timeout)

        self._kernel._cond.wait = counting_wait
        return self

    def __exit__(self, *exc) -> None:
        self._kernel._cond.wait = self._orig


def _chain_kernel(names=("a", "b", "c"), *, block_on="c"):
    """构造 a<-b<-c 三代依赖链内核。

    返回 (kernel, counts, teardown_entered, release_teardown, generation)。
    逆序第一个释放的组件 block_on 在 teardown 中阻塞，撑开停机窗口；
    release_teardown.set() 后停机才继续。
    """
    counts = {n: 0 for n in names}
    state_lock = threading.Lock()
    generation = {"n": 0}
    teardown_entered = threading.Event()
    release_teardown = threading.Event()

    def factory(name):
        def f(ctx):
            with state_lock:
                generation["n"] += 1 if name == names[0] else 0
            return f"gen{generation['n']}-{name}"

        return f

    def teardown(name):
        def h(instance):
            counts[name] += 1
            if name == block_on:
                teardown_entered.set()
                release_teardown.wait(5)

        return h

    registry = PluginRegistry()
    prev: tuple[str, ...] = ()
    for i, name in enumerate(names):
        deps = prev
        registry.register(
            PluginSpec(name, factory(name), frozenset(deps), teardown=teardown(name))
        )
        prev = (name,)
    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    return kernel, counts, teardown_entered, release_teardown, generation


def _file_kernel(tmp_path, *, factory, teardown=None, extra_sources=()):
    cfg = tmp_path / "app.json"
    cfg.write_text(json.dumps({"greeting": "v1"}), encoding="utf-8")
    loader = LayeredConfigLoader(
        ConfigSchema((FieldSpec("greeting", str, default="hello"),)),
        [MappingSource({}, name="defaults", priority=0),
         FileSource(cfg, priority=10), *extra_sources],
    )
    registry = PluginRegistry()
    registry.register(PluginSpec("only", factory, teardown=teardown))
    return AssemblyKernel(config_loader=loader, registry=registry), cfg


# ----------------------------------------------------------------------
# 场景一：停机进行中再次触发停机 + 后台重新拉起
# ----------------------------------------------------------------------


@pytest.mark.parametrize("iteration", range(15))
def test_stopping_stop_joiners_merge_into_same_round_despite_restart(
    iteration, case_log
):
    case_log(
        "停机并入(成功轮)+重启并发",
        "1 个 stop 正在释放 c/b/a；释放窗口内 2 个 stop 并入、"
        "1 个 get 在后台重新拉起",
        "teardown 全程只跑一轮；并入方与发起方同样成功返回；"
        "重启得到完整可用的新一代；内核最终 RUNNING",
    )
    kernel, counts, entered, release, _gen = _chain_kernel()
    kernel.start()
    first_a = kernel.get("a")

    leader = threading.Thread(target=kernel.stop)
    joiners = 2
    results: list[object] = []

    def joiner():
        try:
            kernel.stop()
            results.append("ok")
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    restarted: list[object] = []

    def restarter():
        restarted.append(kernel.get("a"))

    with _WaitCounter(kernel, expected=joiners + 1) as wc:
        leader.start()
        assert entered.wait(5), "停机未进入 teardown"
        with ThreadPoolExecutor(joiners + 1) as pool:
            f_join = [pool.submit(joiner) for _ in range(joiners)]
            f_restart = pool.submit(restarter)
            assert wc.all_waiting.wait(5), "并入方与重启方未全部进入等待"
            release.set()
            for f in f_join:
                f.result(timeout=5)
            f_restart.result(timeout=5)
            new_a = restarted[0]

    leader.join(5)

    # 每个组件自始至终只释放一次（没有第二轮释放）
    assert counts == {"a": 1, "b": 1, "c": 1}
    # 并入方与发起方拿到同一成功结果
    assert results == ["ok"] * joiners
    # 等待方拿到的是重启后完整可用的新一代实例，而非被释放的旧实例
    assert new_a != first_a
    assert restarted[0] is new_a
    # 重新拉起成功后内核必须 RUNNING，不能停在关闭状态
    assert kernel.is_started()
    # 新一代实例保持可用（不会在断言期间被二次释放）
    assert kernel.get("a") is new_a
    # 新代处于 RUNNING 时再停机：只释放新一代一次，幂等正常
    kernel.stop()
    assert counts == {"a": 2, "b": 2, "c": 2}
    assert not kernel.is_started()


def test_stopping_failed_round_joiners_share_summary_then_restart_ok(case_log):
    case_log(
        "停机并入(失败轮)+重启并发",
        "中间组件 b 释放失败（消息带密钥）；窗口内 2 个 stop 并入、"
        "1 个 get 后台重新拉起",
        "每个组件仍只释放一次且失败不跳过其他组件；3 个停机调用方拿到"
        "同一个按组件名可区分的 ShutdownError；重启成功且不背旧失败；"
        "日志/汇总消息不含密钥",
    )
    names = ("a", "b", "c")
    counts = {n: 0 for n in names}
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    b_fail_armed = {"on": True}  # b 只在第一轮停机失败，模拟下一轮恢复

    def factory(name):
        return lambda ctx: f"instance-{name}"

    def teardown(name):
        def h(instance):
            with lock:
                counts[name] += 1
            if name == "c":
                entered.set()
                release.wait(5)
            if name == "b" and b_fail_armed["on"]:
                b_fail_armed["on"] = False
                raise RuntimeError(f"close failed token={SECRET}")

        return h

    registry = PluginRegistry()
    registry.register(PluginSpec("a", factory("a"), teardown=teardown("a")))
    registry.register(
        PluginSpec(
            "b", factory("b"), frozenset({"a"}), teardown=teardown("b")
        )
    )
    registry.register(
        PluginSpec(
            "c", factory("c"), frozenset({"b"}), teardown=teardown("c")
        )
    )
    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()

    errors: list[ShutdownError] = []

    def leader_stop():
        try:
            kernel.stop()
        except ShutdownError as exc:
            errors.append(exc)

    def joiner_stop():
        try:
            kernel.stop()
        except ShutdownError as exc:
            errors.append(exc)

    new_instances: list[object] = []

    def restarter():
        new_instances.append(kernel.get("c"))

    leader = threading.Thread(target=leader_stop)
    with _WaitCounter(kernel, expected=3) as wc:
        leader.start()
        assert entered.wait(5)
        with ThreadPoolExecutor(3) as pool:
            fj = [pool.submit(joiner_stop) for _ in range(2)]
            fr = pool.submit(restarter)
            assert wc.all_waiting.wait(5)
            release.set()
            for f in fj:
                f.result(timeout=5)
            fr.result(timeout=5)
            new_c = new_instances[0]
    leader.join(5)

    # 失败不跳过其他组件，且每个组件恰好一次
    assert counts == {"a": 1, "b": 1, "c": 1}
    # 发起方 + 2 个并入方拿到同一个汇总异常
    assert len(errors) == 3
    assert all(e is errors[0] for e in errors)
    assert set(errors[0].failures) == {"b"}
    assert isinstance(errors[0].failures["b"], RuntimeError)
    # 汇总消息只含组件名与异常类型，不泄漏密钥
    assert SECRET not in str(errors[0])
    # 重启成功：新实例完整可用，且不背上一轮失败
    assert new_c == "instance-c"
    assert kernel.is_started()
    assert kernel.get("c") is new_c
    # 新一轮全部释放成功 -> stop 正常返回，不抛旧的 ShutdownError；
    # 新一代每个组件也只释放一次（两代合计各 2 次）
    kernel.stop()
    assert not kernel.is_started()
    assert counts == {"a": 2, "b": 2, "c": 2}


# ----------------------------------------------------------------------
# 场景二：停机与重新拉起并发，调用方只见完整结果
# ----------------------------------------------------------------------


def test_get_during_shutdown_waits_and_gets_complete_new_generation(case_log, tmp_path):
    case_log(
        "停机期间 get 等待新一代",
        "旧代正在 teardown（阻塞中）时 3 个线程并发 get('only')",
        "等待方在停机结束后拿到按当前配置装配的同一个完整新实例；"
        "装配只发生一次；拿到的实例之后不会再被本轮停机释放",
    )
    builds = {"n": 0}
    build_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def factory(ctx):
        with build_lock:
            builds["n"] += 1
        return object()

    def teardown(instance):
        entered.set()
        release.wait(5)

    kernel, _cfg = _file_kernel(tmp_path, factory=factory, teardown=teardown)
    kernel.start()
    first = kernel.get("only")

    leader = threading.Thread(target=kernel.stop)
    getters = 3
    results: list[object] = []

    def getter():
        results.append(kernel.get("only"))

    with _WaitCounter(kernel, expected=getters) as wc:
        leader.start()
        assert entered.wait(5)
        with ThreadPoolExecutor(getters) as pool:
            futs = [pool.submit(getter) for _ in range(getters)]
            assert wc.all_waiting.wait(5)
            release.set()
            for f in futs:
                f.result(timeout=5)
    leader.join(5)

    assert builds["n"] == 2  # 旧代一次 + 新代一次
    assert len(results) == getters
    new_instance = results[0]
    assert all(r is new_instance for r in results)
    assert new_instance is not first
    assert kernel.is_started()
    # 新实例在等待方拿到后仍稳定可用：旧停机轮次不会回头释放它
    assert kernel.get("only") is new_instance


def test_get_during_shutdown_gets_clear_failure_if_restart_fails(case_log, tmp_path):
    case_log(
        "停机期间重启失败给明确失败",
        "旧代停机窗口内 2 个 get 等待；停机结束后配置非法导致重启失败",
        "等待方收到同一个可区分的 ConfigValidationError；内核收敛为"
        "STOPPED、不残留半成品；修复配置后可从头完整重启",
    )

    class FailSecondLoad(MappingSource):
        """第 2 次 load 抛配置校验错误（模拟停机后最新配置非法）。"""

        def __init__(self):
            super().__init__({}, name="flaky", priority=5)
            self.calls = 0
            self._lock = threading.Lock()

        def load(self):
            with self._lock:
                self.calls += 1
                n = self.calls
            if n == 2:
                raise ConfigValidationError("字段 'greeting' 类型不匹配")
            return {}

    flaky = FailSecondLoad()
    entered = threading.Event()
    release = threading.Event()

    def factory(ctx):
        return object()

    def teardown(instance):
        entered.set()
        release.wait(5)

    kernel, cfg = _file_kernel(
        tmp_path, factory=factory, teardown=teardown, extra_sources=(flaky,)
    )
    kernel.start()

    errors: list[BaseException] = []

    def getter():
        try:
            kernel.get("only")
        except ConfigValidationError as exc:
            errors.append(exc)

    leader = threading.Thread(target=kernel.stop)
    with _WaitCounter(kernel, expected=2) as wc:
        leader.start()
        assert entered.wait(5)
        with ThreadPoolExecutor(2) as pool:
            futs = [pool.submit(getter) for _ in range(2)]
            assert wc.all_waiting.wait(5)
            release.set()
            for f in futs:
                f.result(timeout=5)
    leader.join(5)

    # 等待方拿到明确且同一个失败
    assert len(errors) == 2
    assert errors[0] is errors[1]
    assert not kernel.is_started()
    # 修复配置后从头完整重启成功
    cfg.write_text(json.dumps({"greeting": "recovered"}), encoding="utf-8")
    assert kernel.get("only") is not None
    assert kernel.is_started()
    kernel.stop()


# ----------------------------------------------------------------------
# 场景三：释放失败边界（密钥不泄漏 + 每个组件只释放一次）
# ----------------------------------------------------------------------


def test_teardown_failure_logs_and_summary_never_leak_secret(case_log, caplog):
    case_log(
        "释放失败不泄漏密钥",
        "db/web 正常，svc 抛出消息含密钥的异常；4 路并发 stop",
        "每个组件恰好释放一次、失败不跳过其他组件；所有调用方拿到同一个"
        "按组件名可区分的汇总；日志与汇总消息均不含密钥",
    )
    released: list[str] = []
    rel_lock = threading.Lock()

    def ok(name):
        def h(instance):
            with rel_lock:
                released.append(name)

        return h

    def bad(instance):
        with rel_lock:
            released.append("svc")
        raise RuntimeError(f"disconnect failed password={SECRET}")

    registry = PluginRegistry()
    registry.register(
        PluginSpec("db", lambda ctx: "db-instance", teardown=ok("db"))
    )
    registry.register(
        PluginSpec(
            "svc",
            lambda ctx: "svc-instance",
            frozenset({"db"}),
            teardown=bad,
        )
    )
    registry.register(
        PluginSpec(
            "web",
            lambda ctx: "web-instance",
            frozenset({"svc"}),
            teardown=ok("web"),
        )
    )
    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()

    errors: list[ShutdownError] = []

    def worker():
        try:
            kernel.stop()
        except ShutdownError as exc:
            errors.append(exc)

    with caplog.at_level("DEBUG"):
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: worker(), range(4)))

    # 逆序全部尝试，每个组件恰好一次
    assert released == ["web", "svc", "db"]
    assert len(errors) == 4
    summary = errors[0]
    assert all(e is summary for e in errors)
    assert set(summary.failures) == {"svc"}
    # 汇总消息含组件名与异常类型，但不含密钥
    assert "svc" in str(summary) and "RuntimeError" in str(summary)
    assert SECRET not in str(summary)
    # 日志同样不含密钥（原始异常消息不写入日志）
    assert SECRET not in caplog.text
    # 原始异常保留在 failures 中供程序化区分处理
    assert isinstance(summary.failures["svc"], RuntimeError)
    assert not kernel.is_started()
