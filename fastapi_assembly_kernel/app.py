"""FastAPI 集成：在 lifespan 中装配内核，同时保持最小接口兼容。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .kernel import AssemblyKernel, set_kernel


def create_app(kernel: AssemblyKernel) -> FastAPI:
    """创建 FastAPI 应用并通过 lifespan 装配内核。

    * 应用启动时执行一次内核装配（异步入口，与同步 start 共享同一次）；
    * 装配失败（配置错误/依赖错误/工厂失败）会让应用启动失败，
      错误以原始可区分类型向上传播，绝不静默降级；
    * 内核实例同时挂在 ``app.state.kernel`` 与进程默认槽位
      （供 ``fastapi_assembly_kernel.get_kernel`` 依赖使用）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await kernel.astart()
        app.state.kernel = kernel
        set_kernel(kernel)
        try:
            yield
        finally:
            set_kernel(None)
            # 应用关闭时优雅停机：逆装配顺序释放组件，仅释放一次；
            # 释放失败以 ShutdownError 向上传播（含逐组件明细）
            await kernel.astop()

    app = FastAPI(lifespan=lifespan)
    return app
