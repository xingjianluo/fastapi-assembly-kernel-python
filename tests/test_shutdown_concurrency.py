"""停机并发规则的确定性测试：

* 停机进行中再次触发停机 -> 后来者并入同一轮，拿同一结果，不发起第二轮释放；
* 停机与重新拉起并发 -> 等待方拿到完整可用、且不会再被旧停机轮释放的新实例；
* 释放失败边界 -> 每个组件只释放一次、失败不跳过其他组件、并入方共享同一份
  可按组件名区分的 ShutdownError，且日志/报错不泄漏密钥。

时序全部用 threading.Event / Barrier 以及一个“按指定顺序放行等待线程”的
条件变量替身 OrderedCond 编排，不依赖 sleep 赌调度。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
    ShutdownError,
)

SECRET = "s3cr3t-token-9f3a"


# ---------------------------------------------------------------------
# 测试道具
# ---------------------------------------------------------------------


class OrderedCond:
    """条件变量替身：notify 只标记“可唤醒”，由测试按指定顺序真正放行。

    wait() 的线程在独立闸门上阻塞；测试通过 release(tag) 决定它们
    重新获取锁的先后，从而构造普通调度下无法稳定复现的交错时序。
    与 threading.Condition 一样，所有方法都在持有底层锁时调用。
    """

    def __init__(self, on_wait=None):
        self._lock = threading.Lock()
        self._waiting: dict[str, threading.Event] = {}
        self._on_wait = on_wait

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()

    def _gate(self) -> threading.Event:
        tag = threading.current_thread().name
        gate = threading.Event()
        self._waiting[tag] = gate
        if self._on_wait is not None:
            self._on_wait(tag)
        return gate

    def wait(self, timeout=None):
        gate = self._gate()
        self._lock.release()
        gate.wait(timeout=timeout)
        self._lock.acquire()

    def wait_for(self, predicate, timeout=None):
        while not predicate():
            self.wait(timeout=timeout)
        return True

    def notify(self, n=1):
        # 唤醒资格由测试通过 release 显式控制，这里不做任何放行
        return

    def notify_all(self):
        return

    def release(self, tag: str) -> None:
        """放⾏指定线程（以线程名标记）重新参与临界区。"""
        self._waiting[tag].set()


def make_kernel(specs, *, observer=None, cond=None):
    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    for name, (deps, teardown) in specs.items():
        registry.register(
            PluginSpec(
                name,
                lambda ctx, n=name: f"instance-{n}",
                frozenset(deps),
                teardown=teardown,
            )
        )
    kernel = AssemblyKernel(config_loader=loader, registry=registry, observer=observer)
    if cond is not None:
        kernel._cond = cond
    return kernel


def run_in_thread(target, name):
    t = threading.Thread(target=target, name=name)
    t.start()
    return t


# ---------------------------------------------------------------------
# 1. 停机进行中：后来者并入同一轮，共享成功结果，只释放一遍
# ---------------------------------------------------------------------


def test_stop_arriving_during_stopping_joins_same_round(case_log):
    joiners = 4
    case_log(
        "停机并入-成功",
        f"1 个停机发起方 + {joiners} 个在 STOPPING 阶段并入的调用方",
        "teardown 只执行一轮；并入方全部等待本轮并拿到成功结果",
    )
    joined = threading.Event()
    state = {"n": 0}
    state_lock = threading.Lock()
    release_td = threading.Event()

    def observer(event):
        if event == "stop_round_joined":
            with state_lock:
                state["n"] += 1
                if state["n"] == joiners:
                    joined.set()

    def teardown(instance):
        release_td.wait(5)

    kernel = make_kernel({"a": ((), teardown)}, observer=observer)
    kernel.start()
    results: list[object] = []

    def stop_and_record():
        try:
            kernel.stop()
            results.append("ok")
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    leader = run_in_thread(stop_and_record, "leader")
    # observer 在并入方进入等待的临界区内触发，达到 joiners 人意味着
    # 所有后来者都已确认“并入当前轮”并提交等待，此时再放行释放
    workers = [
        run_in_thread(stop_and_record, f"joiner-{i}") for i in range(joiners)
    ]
    assert joined.wait(5), "并入方未全部进入同一轮停机"
    release_td.set()
    leader.join(5)
    for w in workers:
        w.join(5)

    assert results == ["ok"] * (joiners + 1)
    assert not kernel.is_started()


def test_double_release_cannot_happen_when_restart_wins_race(case_log):
    """旧缺陷的精确交错：停机结束的瞬间重启先被提交，并入方后醒来。

    用 OrderedCond 强制“重启线程先在 STOPPED 上完成新一轮装配发布，
    旧停机轮的并入方随后才被放行”。旧实现下并入方醒来看到 RUNNING
    会再释放一遍；正确实现下并入方只认自己并入的那一轮，直接拿成功。
    """
    case_log(
        "重启先提交/并入方后醒",
        "A 停机中 B 并入；停机结束后 C 先重启成功，再放行 B",
        "gen1 只释放一次；C 拿到的新实例未被释放；最终内核 RUNNING；"
        "B 与 A 拿到同一轮的成功结果",
    )
    b_registered = threading.Event()
    cond = OrderedCond(on_wait=lambda tag: b_registered.set() if tag == "B" else None)

    td_enter = threading.Event()
    release_td = threading.Event()
    build2_started = threading.Event()
    allow_build2 = threading.Event()
    torn: list[str] = []

    def teardown(instance):
        torn.append(instance)
        td_enter.set()
        release_td.wait(5)

    def factory(ctx):
        if getattr(factory, "calls", 0) == 1:
            build2_started.set()
            allow_build2.wait(5)
        factory.calls = getattr(factory, "calls", 0) + 1
        return f"instance-gen{factory.calls}"

    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    registry.register(PluginSpec("only", factory, teardown=teardown))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel._cond = cond
    kernel.start()
    gen1 = "instance-gen1"

    results: dict[str, object] = {}

    def stop_to(who):
        def job():
            try:
                kernel.stop()
                results[who] = "ok"
            except BaseException as exc:  # noqa: BLE001
                results[who] = exc
        return job

    def get_to():
        try:
            results["C"] = kernel.get("only")
        except BaseException as exc:  # noqa: BLE001
            results["C"] = exc

    # A 发起停机并停在 teardown
    A = run_in_thread(stop_to("A"), "A")
    assert td_enter.wait(5)
    # B 在 STOPPING 阶段并入，阻塞在 OrderedCond 的闸门上
    B = run_in_thread(stop_to("B"), "B")
    assert b_registered.wait(5)
    # 放行 teardown：A 收敛停机轮（B 仍被闸门挡住）后返回
    release_td.set()
    A.join(5)
    assert results["A"] == "ok"

    # A 已彻底结束、B 尚未醒来：C 把内核重新拉起，新实例完整发布
    C = run_in_thread(get_to, "C")
    assert build2_started.wait(5)
    allow_build2.set()
    C.join(5)
    new_instance = results["C"]
    assert isinstance(new_instance, str) and new_instance == "instance-gen2"

    # 此时再放行并入方 B
    cond.release("B")
    B.join(5)

    assert results["B"] == "ok"
    assert torn == [gen1]                  # gen1 仅释放一次
    assert new_instance not in torn        # C 拿到的实例没有被旧停机轮释放
    assert kernel.is_started()             # 重新拉起成功后内核保持 RUNNING


def test_joined_failure_is_shared_and_components_released_once(case_log, caplog):
    joiners = 3
    case_log(
        "停机并入-失败汇总",
        f"svc 释放时抛出含密钥的异常，{joiners} 个调用方在 STOPPING 并入",
        "每个组件只释放一次且失败不跳过其他组件；全部调用方拿到同一个"
        "按组件名区分的 ShutdownError；日志与报错不泄漏密钥；之后可重启",
    )
    joined = threading.Event()
    state = {"n": 0}
    release_td = threading.Event()

    def observer(event):
        if event == "stop_round_joined":
            state["n"] += 1
            if state["n"] == joiners:
                joined.set()

    released: list[str] = []

    def ok(name):
        def hook(instance):
            released.append(name)
        return hook

    def web_hook(instance):
        # web 是逆序第一个释放的组件：阻塞在这里保持停机轮敞开，
        # 直到全部并入方确认并入同一轮
        released.append("web")
        release_td.wait(5)

    def bad(instance):
        released.append("svc")
        raise RuntimeError(f"cannot close, token={SECRET}")

    specs = {
        "db": ((), ok("db")),
        "svc": (("db",), bad),
        "web": (("svc",), web_hook),
    }
    kernel = make_kernel(specs, observer=observer)
    kernel.start()
    errors: list[ShutdownError] = []

    def stop_and_collect():
        try:
            kernel.stop()
        except ShutdownError as exc:
            errors.append(exc)

    leader = run_in_thread(stop_and_collect, "leader")
    workers = [run_in_thread(stop_and_collect, f"joiner-{i}") for i in range(joiners)]
    assert joined.wait(5)
    with caplog.at_level("ERROR"):
        release_td.set()
        leader.join(5)
        for w in workers:
            w.join(5)

    # 逆序释放：失败的 svc 不跳过 web/db，每个组件恰好一次
    assert released == ["web", "svc", "db"]
    # 发起方与所有并入方拿到同一个汇总异常
    assert len(errors) == joiners + 1
    shared = errors[0]
    assert all(e is shared for e in errors)
    assert set(shared.failures) == {"svc"}
    assert isinstance(shared.failures["svc"], RuntimeError)
    assert "svc" in str(shared)
    # 敏感内容不进入报错文本与日志（只有组件名、异常类型名）
    assert SECRET not in str(shared)
    assert SECRET not in caplog.text

    # 失败轮之后仍可按最新配置重新拉起，新实例完整可用
    kernel.start()
    assert kernel.is_started()
    assert kernel.get("web") == "instance-web"
    # 再停机：新一代组件同样各自只释放一次（本轮 svc 仍失败，
    # ShutdownError 为预期行为）
    with pytest.raises(ShutdownError):
        kernel.stop()
    assert released == ["web", "svc", "db", "web", "svc", "db"]


# ---------------------------------------------------------------------
# 2. 停机与重新拉起并发：等待方只见完整可用的新一代
# ---------------------------------------------------------------------


def test_lazy_get_during_stopping_gets_full_new_generation(case_log):
    case_log(
        "停机期间惰性访问",
        "teardown 进行中另一个线程 get()，teardown 被闸门挡住",
        "get 等待停机彻底结束后拿到全新装配的完整实例；gen1 只释放一次；"
        "最终 RUNNING，交出的实例未被释放",
    )
    td_enter = threading.Event()
    release_td = threading.Event()
    builds = {"n": 0}
    lock = threading.Lock()

    def factory(ctx):
        with lock:
            builds["n"] += 1
            gen = builds["n"]
        return f"gen{gen}-instance"

    def teardown(instance):
        td_enter.set()
        release_td.wait(5)

    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    registry.register(PluginSpec("only", factory, teardown=teardown))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()

    def stop_job():
        kernel.stop()

    A = run_in_thread(stop_job, "stopper")
    assert td_enter.wait(5)

    got: dict[str, object] = {}
    C = run_in_thread(lambda: got.setdefault("v", kernel.get("only")), "getter")
    release_td.set()
    A.join(5)
    C.join(5)

    assert got["v"] == "gen2-instance"
    assert builds["n"] == 2
    assert kernel.is_started()


def test_stop_during_new_start_applies_to_new_generation(case_log):
    """边界语义：停机已彻底结束后，针对“正在进行的新一轮启动”到达的停机，
    作用于新一代（它并非旧停机轮的并入方）。
    """
    case_log(
        "新一轮启动中到达的停机",
        "停机结束 -> 重启装配在工厂内阻塞 -> 此刻 stop() 到达",
        "该 stop 等装配发布后释放新一代（gen2 恰好一次）；"
        "它不与已结束的旧轮合并，也不重复释放 gen1",
    )
    build2_started = threading.Event()
    allow_build2 = threading.Event()
    torn: list[str] = []

    def factory(ctx):
        if getattr(factory, "calls", 0) == 1:
            build2_started.set()
            allow_build2.wait(5)
        factory.calls = getattr(factory, "calls", 0) + 1
        return f"gen{factory.calls}"

    def teardown(instance):
        torn.append(instance)

    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    registry.register(PluginSpec("only", factory, teardown=teardown))
    kernel = AssemblyKernel(config_loader=loader, registry=registry)
    kernel.start()
    kernel.stop()
    assert torn == ["gen1"]

    C = run_in_thread(lambda: kernel.start(), "restarter")
    assert build2_started.wait(5)
    # 新装配尚未发布（STARTING）时停机到达
    D = run_in_thread(lambda: kernel.stop(), "late-stopper")
    allow_build2.set()
    C.join(5)
    D.join(5)

    assert torn == ["gen1", "gen2"]  # gen1、gen2 各只释放一次
    assert not kernel.is_started()


def test_astop_joins_while_aget_restarts(case_log):
    """异步入口（astart/astop/aget 经 to_thread）与同步入口共享同一套轮次。"""
    case_log(
        "异步/同步混合并发",
        "同步发起停机；协程任务 aget 等待重启；另一协程 astop 并入旧轮",
        "与线程场景一致：旧轮只释放一遍，aget 拿到完整新实例，"
        "并入的 astop 拿旧轮成功结果且不释放新实例",
    )
    td_enter = threading.Event()
    release_td = threading.Event()
    joined = threading.Event()
    state = {"n": 0}

    def observer(event):
        if event == "stop_round_joined":
            state["n"] += 1
            if state["n"] == 1:
                joined.set()

    def factory(ctx):
        return object()

    def teardown(instance):
        td_enter.set()
        release_td.wait(5)

    loader = LayeredConfigLoader(
        ConfigSchema(()), [MappingSource({}, name="test", priority=0)]
    )
    registry = PluginRegistry()
    registry.register(PluginSpec("only", factory, teardown=teardown))
    kernel = AssemblyKernel(config_loader=loader, registry=registry, observer=observer)
    kernel.start()

    outcome: dict[str, object] = {}

    def sync_stop():
        kernel.stop()
        outcome["A"] = "ok"

    async def joined_stop():
        await kernel.astop()
        outcome["B"] = "ok"

    async def restart_get():
        outcome["C_done"] = await kernel.aget("only")

    A = run_in_thread(sync_stop, "sync-leader")
    assert td_enter.wait(5)
    B = run_in_thread(lambda: asyncio.run(joined_stop()), "async-joiner")
    assert joined.wait(5)

    C = run_in_thread(lambda: asyncio.run(restart_get()), "async-restart")
    release_td.set()
    A.join(5)
    B.join(5)
    C.join(5)

    assert outcome["A"] == "ok"
    assert outcome["B"] == "ok"
    assert isinstance(outcome["C_done"], object)
    assert kernel.is_started()
    # 新实例仍然存活、可用
    assert kernel.get("only") is outcome["C_done"]
    kernel.stop()
