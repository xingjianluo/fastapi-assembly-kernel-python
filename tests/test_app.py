"""FastAPI 集成：最小接口兼容与内核挂载。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    ConfigValidationError,
    FieldSpec,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
    PluginSpec,
)
from fastapi_assembly_kernel.app import create_app


def test_main_root_endpoint_compatible(case_log):
    case_log("最小接口兼容", "GET /",
             "返回 200 且 body 仍为 {'Hello': 'World'}")
    from main import app

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json() == {"Hello": "World"}


def test_kernel_started_via_lifespan(case_log):
    case_log("lifespan 装配", "create_app(kernel) + TestClient 上下文",
             "应用启动后内核处于已启动状态，插件可经 app.state.kernel 获取")
    registry = PluginRegistry()
    registry.register(PluginSpec("marker", lambda ctx: "marker-instance"))
    kernel = AssemblyKernel(
        config_loader=LayeredConfigLoader(ConfigSchema(()), []),
        registry=registry,
    )
    app = create_app(kernel)
    assert not kernel.is_started()
    with TestClient(app) as client:
        assert kernel.is_started()
        assert client.app.state.kernel is kernel
        assert kernel.get("marker") == "marker-instance"


def test_startup_failure_propagates(case_log):
    case_log("启动失败暴露", "配置含非法字段值",
             "应用启动直接失败并抛出可区分的 ConfigValidationError，不静默降级")
    loader = LayeredConfigLoader(
        ConfigSchema((FieldSpec("port", int, default=1),)),
        [MappingSource({"port": "bad"}, name="broken", priority=0)],
    )
    kernel = AssemblyKernel(config_loader=loader, registry=PluginRegistry())
    app = create_app(kernel)
    with pytest.raises(ConfigValidationError):
        with TestClient(app):
            pass


def test_lifespan_shutdown_releases_components(case_log):
    case_log("lifespan 优雅停机", "TestClient 退出上下文",
             "应用关闭时按逆装配顺序释放组件且仅释放一次，内核回到未启动状态")
    released: list[str] = []
    registry = PluginRegistry()
    registry.register(
        PluginSpec("db", lambda ctx: "db-instance",
                   teardown=lambda i: released.append("db"))
    )
    registry.register(
        PluginSpec("svc", lambda ctx: "svc-instance", frozenset({"db"}),
                   teardown=lambda i: released.append("svc"))
    )
    kernel = AssemblyKernel(
        config_loader=LayeredConfigLoader(ConfigSchema(()), []),
        registry=registry,
    )
    app = create_app(kernel)
    with TestClient(app):
        assert kernel.is_started()
    assert released == ["svc", "db"]
    assert not kernel.is_started()
