"""FastAPI 应用入口。

根接口 ``GET /`` 保持模板原有行为（``{"Hello": "World"}``）；
同时通过 FastAPI lifespan 演示 assembly_kernel 的标准装配方式：
分层配置 → 插件装配 → 惰性资源在首次使用时初始化。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from assembly_kernel import (
    ConfigField,
    DictSource,
    EnvSource,
    LazyResource,
    Plugin,
    PluginRegistry,
    assemble,
    load_layered_config,
)

logger = logging.getLogger("app")

# 演示用的最小字段集合；字段非法会在此处抛出可区分的启动错误。
_FIELDS = [
    ConfigField("app.name", str, default="fastapi-assembly-kernel", required=False),
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 1) 分层配置（默认值 < 环境变量 < 显式覆盖）；缺失/非法配置在此快速失败。
    cfg = load_layered_config(
        _FIELDS,
        [
            EnvSource("env", 20, _FIELDS),
            DictSource("override", 30, {}),
        ],
    )

    # 2) 声明插件依赖图；注册错误（重名/缺失/循环）在 assemble 前即被拒绝。
    registry = PluginRegistry()
    app_info = LazyResource("app-info", lambda: {"name": cfg.get("app.name")})
    registry.register(Plugin("app-info", factory=lambda _deps: app_info.get()))

    # 3) 拓扑装配；惰性资源此刻才首次初始化。
    context = assemble(registry)
    app.state.plugins = context
    logger.info("应用装配完成 order=%s", context.order)
    try:
        yield
    finally:
        app_info.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    # 行为与初始模板完全一致，保证现有最小接口兼容。
    return {"Hello": "World"}
