# fastapi-assembly-kernel

在现有 FastAPI 模板之上提供一套可复用的**应用装配内核**
（`assembly_kernel`），覆盖：

1. **分层配置** — 多来源确定性合并、严格类型校验、敏感值屏蔽；
2. **插件式装配** — 声明依赖、确定性拓扑排序、可解释的错误拒绝；
3. **惰性生命周期** — 并发安全的一次性初始化、失败广播与安全重试。

现有最小接口 `GET /` → `{"Hello": "World"}` 行为保持不变
（见 `tests/test_app_compat.py`）。

## 目录结构

```
assembly_kernel/
├── errors.py       # 可区分异常层次（KernelError 基类）
├── config.py       # ConfigField / FileSource / EnvSource / DictSource / load_layered_config
├── plugins.py      # Plugin / PluginRegistry / topological_order / assemble
└── lifecycle.py    # LazyResource / ResourceState
tests/              # 65 个用例：优先级冲突、非法配置、循环/缺失依赖、并发初始化等
docs/architecture.md # 完整规则与语义说明
main.py             # 现有 FastAPI 最小应用（兼容）
```

## 快速开始

```bash
uv sync
uv run pytest                       # 运行全部测试
uv run uvicorn main:app             # 启动最小服务
curl http://127.0.0.1:8000/         # {"Hello":"World"}
```

## 规则一览

| 主题 | 规则要点 |
| --- | --- |
| 配置优先级 | 默认值(0) < 文件(10) < 环境变量(20) < 显式覆盖(30)；同级后者胜 |
| 严格失败 | 来源缺失/格式非法/类型不符/必填缺失 → 各自独立的异常，绝不静默回退 |
| 敏感值 | `secret=True` 的值不进入日志、异常、repr、脱敏视图 |
| 装配顺序 | Kahn 拓扑排序，就绪层按名称排序；与注册/字典/导入顺序无关 |
| 依赖错误 | 重复注册 / 缺失依赖 / 循环依赖分别抛不同异常，环路径确定可解释 |
| 惰性初始化 | 并发只执行一次；失败全员同源报错、不发布半成品；下一次调用可重试 |

完整语义、错误对照表与本地验证方法见
[`docs/architecture.md`](docs/architecture.md)。
