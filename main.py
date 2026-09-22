"""最小应用入口：保留原有最小接口行为，同时演示装配内核接线。"""

from fastapi_assembly_kernel import (
    AssemblyKernel,
    ConfigSchema,
    FieldSpec,
    LayeredConfigLoader,
    MappingSource,
    PluginRegistry,
)
from fastapi_assembly_kernel.app import create_app

# 分层配置：默认值层（最低优先级）+ 环境变量层（可覆盖）
_config_loader = LayeredConfigLoader(
    ConfigSchema(
        (
            FieldSpec("app_name", str, default="fastapi-assembly-kernel"),
            FieldSpec("debug", bool, default=False),
        )
    ),
    [
        MappingSource({}, name="defaults", priority=0),
        # 如需环境变量覆盖，取消下一行注释：
        # EnvSource(prefix="APP_", priority=10),
    ],
)

_registry = PluginRegistry()
kernel = AssemblyKernel(config_loader=_config_loader, registry=_registry)

app = create_app(kernel)


@app.get("/")
def read_root():
    return {"Hello": "World"}
