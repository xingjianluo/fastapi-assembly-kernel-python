# FastAPI 应用装配内核

在最小 FastAPI 模板之上提供一套供业务模块复用的应用装配内核：

- **分层配置**：多来源加载与确定性合并，非法配置以可区分错误暴露，敏感值不泄漏；
- **依赖注入式插件装配**：声明依赖、自动拓扑排序，重复/缺失/循环依赖被明确拒绝；
- **并发安全的惰性一次性初始化**：多入口并发触发只初始化一次，失败可安全重试。

```
fastapi_assembly_kernel/
├── errors.py      # 可区分异常体系
├── config.py      # 分层配置：来源、优先级合并、类型校验、脱敏
├── registry.py    # 插件注册与确定性依赖解析
├── lifecycle.py   # 惰性一次性初始化原语（线程 + asyncio）
├── kernel.py      # AssemblyKernel：配置 + 插件 + 惰性启动
└── app.py         # FastAPI lifespan 集成
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

`AssemblyKernel` 的启动由 `LazyResource` 驱动：

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

## 4. FastAPI 集成

```python
from fastapi_assembly_kernel import AssemblyKernel, ...
from fastapi_assembly_kernel.app import create_app

kernel = AssemblyKernel(config_loader=loader, registry=registry)
app = create_app(kernel)   # lifespan 中执行一次装配
```

- 装配失败会让应用启动失败，错误以原始可区分类型向上传播；
- 内核挂在 `app.state.kernel`，也可通过
  `fastapi_assembly_kernel.get_kernel` 作为 FastAPI 依赖注入；
- `main.py` 保留原有最小接口：`GET /` 仍返回 `{"Hello": "World"}`。

## 5. 本地验证

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
工厂失败无半装配状态、FastAPI 最小接口兼容。
