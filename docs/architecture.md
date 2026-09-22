# 应用装配内核（assembly_kernel）

供 FastAPI 业务模块复用的装配内核，由三个相互独立、可组合的子系统构成：

| 模块 | 职责 | 关键入口 |
| --- | --- | --- |
| `assembly_kernel.config` | 多来源分层配置、严格校验、敏感值屏蔽 | `load_layered_config()` |
| `assembly_kernel.plugins` | 插件注册、依赖完整性校验、确定性拓扑装配 | `PluginRegistry` / `assemble()` |
| `assembly_kernel.lifecycle` | 并发安全的惰性一次性初始化与重试 | `LazyResource` |
| `assembly_kernel.errors` | 全部可区分异常类型 | `KernelError` 层次 |

---

## 1. 配置分层与优先级规则

### 1.1 来源与优先级（数值越大优先级越高）

| 层级 | priority | 实现 | 典型用途 |
| --- | --- | --- | --- |
| 字段默认值 `defaults` | 0（隐式） | `ConfigField(default=...)` | 代码内安全默认 |
| 配置文件 | 10 | `FileSource`（`.json` / `.toml`） | 环境基线配置 |
| 环境变量 | 20 | `EnvSource` | 容器/CI 注入 |
| 显式覆盖 | 30 | `DictSource` | 命令行、测试覆写 |

合并规则确定且可解释：

1. 先灌入所有字段默认值；
2. 来源按 `priority` **升序**依次施加，后施加的来源覆盖先施加的；
3. **同优先级**按来源在 `sources` 参数中的传入顺序，后者覆盖前者
   （`sorted` 为稳定排序）；
4. 合并结果与来源传入顺序无关，只取决于 `priority`
   （有测试 `test_result_independent_of_source_argument_order` 锁定）。

每个键最终来自哪个来源，可用 `cfg.provenance_of(key)` 与
`cfg.explain()` 查询，返回 `(键, 生效来源, 是否默认值)`。

### 1.2 键模型与环境变量映射

* 文件支持嵌套表，加载时扁平化为**点分键**：`{"db": {"host": "x"}}`
  等价于 `{"db.host": "x"}`；列表作为整体叶子值。
* 环境变量默认映射：键名大写、非字母数字字符替换为 `_`，加前缀
  `APP_`，例如 `db.pool.size` → `APP_DB_POOL_SIZE`；可用
  `ConfigField(env_var=...)` 显式绑定。
* `EnvSource` **只采集字段声明中出现的环境变量**，其余忽略；
  未在任何字段中声明的键出现在来源里会被明确拒绝
  （`ConfigValidationError: 来源提供了未声明的配置键`），防止拼写错误被静默吞掉。

### 1.3 类型系统

* 标量：`str / int / float / bool`（`bool` 不是合法的 `int` 配置值；
  `int` 可用于 `float` 字段并被归一化为 `float`）。
* 容器：`list[X]`、`dict[str, X]`，元素类型递归校验，错误定位到
  具体下标/子键，如 `db.tags[1]`。
* 环境变量：标量按字符串解析（布尔接受
  `true/false/1/0/yes/no/on/off`，大小写不敏感）；容器按 **JSON** 解析。
* 类型校验只在**最终合并结果**上执行一次：JSON/TOML 语法非法是
  `ConfigFormatError`，语法合法但类型不符是 `ConfigTypeError`。

### 1.4 启动失败不静默回退（错误可区分）

| 情形 | 异常 |
| --- | --- |
| 必填来源文件不存在 | `ConfigSourceMissingError` |
| 文件存在但不可读（权限等） | `ConfigSourceError` |
| JSON/TOML 语法非法、顶层不是表、后缀不支持、环境变量无法解析 | `ConfigFormatError` |
| 字段类型与声明不符 | `ConfigTypeError` |
| 必填字段缺失、出现未声明键、读取未注册键 | `ConfigValidationError` |
| 字段重复声明、环境变量被多字段绑定、不支持的声明类型 | `ConfigError` |

以上全部继承 `ConfigError` → `KernelError`，调用方可按具体子类
分别处理。**任何错误都不会导致静默退回默认值。**

### 1.5 敏感值屏蔽

`ConfigField(..., secret=True)` 的值：

* 不出现在任何异常消息中（类型错误只含键名与类型名）；
* 不出现在内核 DEBUG 日志中（日志只记录键、来源、是否 secret）；
* `cfg.as_dict()` 默认以 `"***"` 呈现，需要明文时显式传
  `redact=False`；`repr(cfg)` 只包含键名。

---

## 2. 插件装配与依赖解析规则

### 2.1 注册

```python
Plugin(name="repo", requires=frozenset({"db"}), factory=lambda deps: Repo(deps["db"]))
```

* `name` 必须是非空字符串且全局唯一；重复注册立即抛
  `DuplicatePluginError`。
* `requires` 为依赖名称集合，**声明顺序不影响装配结果**。
* `factory` 接收只读映射 `{依赖名: 已实例化依赖}`；`factory=None`
  时实例记为 `None`（能力位节点）。

### 2.2 确定性拓扑排序

* 先做完整性校验：所有依赖必须已注册。多个缺失同时存在时，按
  `(插件名, 依赖名)` 字典序报告第一个（`MissingDependencyError`）。
* 使用 **Kahn 算法**：初始零入度节点按名称排序入队；每消费一个节点，
  新就绪节点重新排序并入队。因此装配顺序只取决于**名称与依赖图**，
  与注册顺序、`dict` 哈希/迭代顺序、模块导入顺序无关。
  500 节点随机图、多轮乱序注册的测试锁定了这一稳定性。
* 同层无关节点按名称字典序排列。

### 2.3 循环依赖报告

存在环时抛 `CircularDependencyError`，`error.cycle` 是一条**闭合路径**：

1. 从未决节点中反复剥离“在未决子图内入度为 0”的节点，剩余的恰好
   是参与环的节点（挂在环上的下游叶子被排除）；
2. 取字典序最小节点为起点，沿按名排序的邻接边做 DFS，找到首条回到
   起点的路径并闭合，例如 `["a", "b", "c", "a"]`。

### 2.4 装配失败语义

* 任一工厂抛异常 → 包装为 `PluginError`（`__cause__` 保留原异常），
  `assemble()` 不返回任何部分结果。
* 纯注册/依赖类问题在调用任何工厂**之前**就被拒绝。

---

## 3. 惰性初始化：失败与重试语义

`LazyResource(name, init_fn, shutdown=...)` 的状态机：

```
PENDING ──get()──▶ INITIALIZING ──成功──▶ READY
                      │  ▲
                    失败  │ 下一次 get() 重试
                      ▼  │
                    FAILED
```

* **一次初始化**：首次 `get()` 触发初始化；并发触发时只有一个线程
  执行，其余线程在 `threading.Condition` 上等待，绝不重复执行。
* **结果一致**：所有成功调用拿到同一个实例；初始化在锁外执行，
  慢任务不阻塞状态读取，且避免用户回调自锁。
* **失败广播**：初始化抛异常时，本轮初始化者与所有在途等待方都
  收到 `InitializationError`，且 `__cause__` 是**同一个**原始异常
  （测试断言 `id(e.__cause__)` 唯一）。
* **无半初始化**：失败时实例槽位清空、状态为 `FAILED`，任何调用方
  都不可能观察到半成品；初始化期间外部只能观察到 `INITIALIZING`。
* **安全重试**：失败不锁死，下一次 `get()` 重新执行初始化；等待方
  只等待它参与的那一轮，旧失败不会错误地抛给新一轮的调用方
  （按状态纪元区分）。
* **关闭**：`shutdown()` 幂等；在途初始化时先等待结束，成功则调用
  关闭函数并回到 `PENDING`，失败则直接回到可重试状态。关闭函数自身
  抛错会包装为 `InitializationError`，但内核状态仍已复位。
  `reset()` 强制丢弃状态（不调用关闭函数），用于测试/运维。

> 初始化函数内部在抛出异常前产生的外部副作用（已开的连接等）应由
> 其自行清理；内核保证的是“不发布半成品引用”。

---

## 4. 本地验证方法

```bash
# 1. 安装依赖（含 dev 组的 pytest）
uv sync

# 2. 运行全部测试（65 个用例，含并发场景）
uv run pytest

# 3. 单模块运行
uv run pytest tests/test_config.py tests/test_plugins.py tests/test_lifecycle.py -q

# 4. 启动现有最小服务并验证兼容接口
uv run uvicorn main:app
curl http://127.0.0.1:8000/   # => {"Hello":"World"}
```

pytest 已开启 `log_cli`（DEBUG 级），单测运行时可看到：

* 用例输入与期望判定（`tests.case` 日志）；
* 内核的判定依据：来源覆盖记录、字段校验结果、插件注册/装配顺序、
  初始化/重试/关闭事件（`assembly_kernel.*` 日志）。

内核 DEBUG 日志中不含敏感字段值；测试
`test_secret_never_in_logs_errors_repr` 对此做了断言锁定。

### 最小使用示例

```python
from assembly_kernel import (
    ConfigField, DictSource, EnvSource, FileSource, load_layered_config,
    LazyResource, Plugin, PluginRegistry, assemble,
)

fields = [
    ConfigField("db.host", str),
    ConfigField("db.port", int, default=5432, required=False),
    ConfigField("db.token", str, secret=True),
]
cfg = load_layered_config(
    fields,
    [
        FileSource("file", 10, Path("/etc/app/config.toml"), required=False),
        EnvSource("env", 20, fields),
        DictSource("override", 30, {"db.token": token_from_cli}),
    ],
)

conn = LazyResource("db-connection", lambda: connect(cfg.get("db.host")))
reg = PluginRegistry()
reg.register(Plugin("db-connection", factory=lambda _: conn.get()))
reg.register(Plugin("repo", frozenset({"db-connection"}),
                    factory=lambda d: Repo(d["db-connection"])))
ctx = assemble(reg)          # 此时 conn 才真正初始化
repo = ctx.get("repo")
```
