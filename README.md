# FastAPI 应用装配内核

在最小 FastAPI 模板之上提供一套供业务模块复用的应用装配内核：

- **分层配置**：多来源加载与确定性合并，非法配置以可区分错误暴露，敏感值不泄漏；
- **依赖注入式插件装配**：声明依赖、自动拓扑排序，重复/缺失/循环依赖被明确拒绝；
- **并发安全的惰性一次性初始化**：多入口并发触发只初始化一次，失败可安全重试；
- **运行期配置热重载**：不停机按既有优先级重载，全部来源合法才整体原子切换；
- **优雅停机与重启**：逆装配顺序释放、并发触发只释放一轮、失败汇总可区分，
  停机后可用最新配置安全重启。

```
fastapi_assembly_kernel/
├── errors.py      # 可区分异常体系（含 ShutdownError / KernelNotRunningError）
├── config.py      # 分层配置：来源、优先级合并、类型校验、脱敏
├── registry.py    # 插件注册、确定性依赖解析、teardown 钩子声明
├── lifecycle.py   # 惰性一次性初始化原语（线程 + asyncio）
├── kernel.py      # AssemblyKernel：配置 + 插件 + 可重启生命周期状态机
└── app.py         # FastAPI lifespan 集成（启动装配 / 关闭优雅停机）
main.py            # 最小应用入口（GET / 行为保持不变）
tests/             # 单元测试（日志中打印输入与判定依据）
```

## 1. 配置分层与优先级规则

配置由 `ConfigSchema`（字段白名单 + 类型 + 默认值 + 是否敏感）和若干
`ConfigSource` 组成。内置来源：

| 来源 | 说明 |
| --- | --- |
| `MappingSource` | 内存字典，常用于默认值层与测试 |
| `FileSource` | `.json` / `.toml` 文件（按扩展名解析） |
| `EnvSource` | 环境变量，字段 `db_url` 对应 `<PREFIX>DB_URL` |

**优先级规则（确定且可解释）**：

1. 每个来源声明一个整数 `priority`，**越大越优先**；
2. 同一字段被多个来源提供时，高优先级来源的值覆盖低优先级；
3. 未被任何来源提供的字段回落到 `FieldSpec.default`；
4. 两个来源 `priority` 相同会在构造期直接报 `ValueError`——
   不允许出现无法解释的平局。

应用顺序即：`默认值 -> priority 升序的各来源（后者覆盖前者）`。

**错误暴露（绝不静默退回默认值）**：

| 场景 | 异常 |
| --- | --- |
| `required=True` 的来源缺失 | `ConfigSourceMissingError` |
| 文件内容不是合法 JSON/TOML、顶层不是对象 | `ConfigSourceFormatError` |
| 未知字段、类型不匹配、必需字段无来源 | `ConfigValidationError` |

三者都是 `ConfigError` 的互不包含的子类，可分别捕获。
非 env 来源不做隐式类型转换（`"8000"` 不等于 `8000`）；
env 来源的值恒为字符串，按字段声明类型严格解析
（`int` / `float` / `bool`：`true/1/yes/on` 等），解析失败即类型不匹配。

**敏感安全**：`FieldSpec(secret=True)` 的字段在 `ResolvedConfig` 的
`repr` 与 `redacted()` 中显示为 `***`；所有异常信息与日志只包含
字段名、来源名与类型名，绝不包含字段值。

```python
schema = ConfigSchema((
    FieldSpec("port", int, default=8000),
    FieldSpec("api_key", str, required=True, secret=True),
))
loader = LayeredConfigLoader(schema, [
    MappingSource({"port": 8000}, name="defaults", priority=0),
    FileSource("config/app.toml", priority=10),          # 可选
    EnvSource(prefix="APP_", priority=20),               # 最高优先级
])
config = loader.load()   # 非法配置在此以可区分错误抛出
```

## 2. 插件装配与依赖解析规则

```python
registry = PluginRegistry()
registry.register(PluginSpec("db", db_factory))
registry.register(PluginSpec("svc", svc_factory, depends_on=frozenset({"db"})))
```

- **重复注册**：同名插件第二次注册立即抛 `DuplicatePluginError`；
- **缺失依赖**：`resolve_order()` 时抛 `MissingDependencyError`，
  错误信息按名称排序列出全部缺失项；
- **循环依赖**：抛 `CircularDependencyError`，信息包含确定的环路径
  （自依赖 `a -> a` 在声明期即按循环依赖拒绝）。

**装配顺序的确定性**：拓扑排序采用 Kahn 算法，就绪集合用最小堆按
插件名字典序选取，因此同一张依赖图无论注册顺序、字典迭代顺序或
导入顺序如何，输出顺序完全一致；依赖总是先于使用者装配。

**依赖注入**：内核按解析顺序调用工厂，工厂收到 `FactoryContext`：

- `ctx.get(name)`：取得已装配的依赖实例（只能访问 `depends_on` 中
  声明的依赖，访问未声明/未装配的名字抛 `KeyError`）；
- `ctx.config`：已解析的分层配置。

## 3. 初始化失败与重试语义

`AssemblyKernel` 的启动由内部的可重启生命周期状态机驱动
（`STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`）：

- **只初始化一次**：多个线程、多个 asyncio 任务（`aget`/`astart`）
  或两者混合并发触发时，初始化只执行一次，其余调用方等待同一结果；
- **无半初始化状态**：装配使用局部变量构建，全部成功后才一次性发布；
  任何调用方要么拿到完整结果，要么收到失败，绝不观察到半成品；
- **失败广播**：本轮初始化失败时，所有在等待的调用方收到同一个异常；
- **可区分错误**：`ConfigError`/`PluginError` 等内核错误原样传播
  （不被包装）；工厂抛出的其他异常统一包装为
  `InitializationFailedError`（`__cause__` 保留原异常）；
- **安全重试**：失败后状态收敛为 `FAILED`、半成品引用被丢弃，
  下一次 `start()`/`get()` 从头完整重试，不会残留部分状态。

## 4. 运行期配置热重载

应用启动后，`kernel.reload_config()`（异步入口 `areload_config()`）
按第 1 节同一套来源优先级重新解析全部来源：

- **整体切换**：只有所有来源都解析、校验通过，才把新配置一次性
  发布；任一来源缺失（`ConfigSourceMissingError`）、格式非法
  （`ConfigSourceFormatError`）或字段类型不匹配
  （`ConfigValidationError`），当前在用的配置**原封不动**，
  错误以可区分类型原样抛出，且错误信息与日志绝不包含敏感值；
- **快照一致性**：`ResolvedConfig` 不可变，切换是单次引用替换——
  并发读取要么看到切换前完整的一份，要么看到切换后完整的一份，
  不会读到半新半旧，也不会出现短暂的空配置或悄悄退回默认值；
  已经拿到的旧快照对象不受切换影响；
- **串行化**：同一时刻只进行一轮重载（内部互斥）；
- **仅运行期可用**：内核未在运行（未启动/已停机）时调用抛
  `KernelNotRunningError`，不会隐式触发装配。

注意：热重载只切换配置对象，不重建已装配的插件实例；
需要插件级生效时请走停机 + 重启（见下两节）。

## 5. 优雅停机与资源释放

`PluginSpec` 新增可选 `teardown` 钩子（签名：`(实例) -> None`）：

```python
registry.register(PluginSpec("db", db_factory, teardown=lambda db: db.close()))
```

`kernel.stop()`（异步入口 `astop()`）的规则：

- **逆序释放**：按装配顺序的严格逆序调用各插件的 `teardown`
  （依赖别人的组件先释放，被别人依赖的后释放）；顺序来自确定性的
  拓扑排序，与注册/遍历顺序无关；未声明 `teardown` 的组件跳过；
- **以"轮次"为单位只释放一轮**：多个线程/协程并发触发停机时，
  释放只执行一轮。停机**进行中**再到达的 `stop()` 会绑定并**并入
  当前这一轮**，与发起方拿到**同一个轮次结果**（成功，或同一个
  `ShutdownError`）；即使该轮结束后内核已经被其他入口重新拉起，
  并入方也只返回本轮结果，**绝不会对新一代发起第二轮释放**；
- **失败不扩散**：某个组件释放失败不会拖垮或跳过其他组件的释放，
  全部尝试完成后把失败汇总为 `ShutdownError` 抛出，
  `ShutdownError.failures` 是 `{插件名: 原始异常}`，可逐个区分处理；
  即使多个入口并入同一轮，每个组件仍然只释放一次；
- **幂等**：成功停机后再次 `stop()` 是空操作；未启动就 `stop()`
  也是空操作；停机失败后（未重启前）的 `stop()` 会收到同一个
  `ShutdownError`；
- **失败信息的敏感边界**：汇总异常的消息只包含组件名与异常类型
  （如 `svc(RuntimeError)`），停机失败日志同样只记录组件名与
  异常类型；teardown 异常消息若携带密钥/连接串等敏感内容不会进入
  日志或汇总消息（原始异常对象仍保留在 `failures` 中供程序化处理）；
- 停机结束时内核丢弃上一轮的实例与配置引用，不残留任何状态。

### 5.1 停机进行中再次触发停机：后来者如何并入

一次停机对应一个内部"轮次"对象（token + 结果盒子）。调用方到达时：

1. 内核处于 `RUNNING`：本调用成为这一轮的发起方，创建轮次并进入
   `STOPPING`，在锁外按逆序执行全部 teardown；
2. 内核已处于 `STOPPING`：后来者**绑定当前轮次对象**并等待其结束，
   结束后直接读走这一轮的结果——成功则静默返回，失败则与发起方
   抛出**同一个** `ShutdownError`（同一份按组件名可区分的失败汇总）；
3. 关键边界：绑定动作发生在停机进行期间。后来者等待期间如果有其他
   入口把内核重新拉起（`STARTING -> RUNNING`），它也只返回所绑定
   轮次的结果，不会"醒来后把新一代再停一遍"。因此一次逻辑停机最多
   产生一轮释放。

### 5.2 停机与重新拉起并发：调用方不会看到中间态

停机**进行中**到达的 `start()` / `get()` / `config` 会等待本轮停机
彻底结束，然后再参与新一轮：

- 新一轮只有一个装配方（其余调用方并入等待），按**当前最新配置**
  完整装配；
- 实例/配置的"发布"与调用方"取走"在状态锁的**同一个临界区**内完成：
  等待方要么拿到完整可用的新实例/新配置，要么收到明确失败
  （装配错误），不会拿到刚被上一轮停机释放的实例，也不会读到实例
  引用已清空的中间态；
- 重启装配失败时，等待方收到同一个可区分的内核错误，内核收敛为
  `STOPPED` 且不残留半成品，修复后下一次调用从头完整重试；
- 不会出现"重新拉起成功了、内核却停在关闭状态"：新一轮成功发布后
  内核稳定处于 `RUNNING`，旧轮次的并入方返回的是旧轮次结果，
  不影响新一代。

FastAPI 集成中，`create_app` 的 lifespan 在应用关闭时自动执行
上述优雅停机。

## 6. 停机后安全重启

同一个进程里 `stop()` 之后可以再次 `start()`（或惰性 `get()`）：

- **最新配置**：重启重新走一遍分层加载，使用当前最新的配置；
- **无残留**：插件全部重建为新实例，上一轮运行的实例、半成品引用
  都不会泄漏到新的一轮；
- **等待方安全**：重启（或停机）进行中的调用方要么拿到重启后完整
  可用的状态，要么收到失败，不会观察到中间态；重启失败的一轮
  所有等待方收到同一个可区分错误，修复后下一次调用从头完整重试。
  停机窗口内并发的 `stop()` 与重启如何交叉，见 5.1 / 5.2 节。

## 7. FastAPI 集成

```python
from fastapi_assembly_kernel import AssemblyKernel, ...
from fastapi_assembly_kernel.app import create_app

kernel = AssemblyKernel(config_loader=loader, registry=registry)
app = create_app(kernel)   # lifespan 中执行一次装配
```

- 装配失败会让应用启动失败，错误以原始可区分类型向上传播；
- 应用关闭时在 lifespan 中自动执行优雅停机（见第 5 节）；
- 内核挂在 `app.state.kernel`，也可通过
  `fastapi_assembly_kernel.get_kernel` 作为 FastAPI 依赖注入；
- `main.py` 保留原有最小接口：`GET /` 仍返回 `{"Hello": "World"}`。

## 8. 本地验证

```bash
uv sync                 # 安装依赖（含 dev 组 pytest/httpx）
uv run pytest -q        # 运行全部测试
uv run pytest -q -o log_cli=true -o log_cli_level=INFO
                        # 查看每个用例打印的输入与判定依据
uv run uvicorn main:app --reload   # 本地启动
```

测试覆盖：配置优先级冲突、非法配置（缺失/格式/类型/未知字段/必需缺失）、
敏感值脱敏、重复注册、缺失依赖、循环依赖、大规模依赖图确定性、
多线程与 asyncio 并发一次性初始化、失败广播与安全重试、
工厂失败无半装配状态、热重载整体切换与失败保持原配置、
并发读写下的配置快照一致性、逆序释放、并发停机只释放一轮、
释放失败汇总与日志/汇总不泄漏密钥、
**停机进行中再次停机并入同一轮（成功轮/失败轮，轮次间穿插重启，
重复 15 个调度排列）**、**停机窗口内 `get()` 等待并拿到完整新一代
（含重启失败给明确失败后可恢复）**、
停机后安全重启、FastAPI 最小接口兼容与 lifespan 优雅停机。

并发用例的时序编排使用 `threading.Event` / `Barrier` / Future 与
"等待方计数到齐再放行"的确定性同步（见
`tests/test_shutdown_concurrency.py`），不依赖 sleep 赌调度；
可用下面的命令单独反复验证并发场景：

```bash
uv run pytest tests/test_shutdown_concurrency.py -q          # 并发停机/重启专项
for i in $(seq 1 20); do uv run pytest -q || break; done     # 全量连跑 20 轮
```
