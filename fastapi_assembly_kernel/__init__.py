"""FastAPI 应用装配内核：分层配置 / 依赖注入式插件 / 惰性一次性初始化。"""

from .config import (
    ConfigSchema,
    ConfigSource,
    EnvSource,
    FieldSpec,
    FileSource,
    LayeredConfigLoader,
    MappingSource,
    ResolvedConfig,
)
from .errors import (
    CircularDependencyError,
    ConfigError,
    ConfigSourceFormatError,
    ConfigSourceMissingError,
    ConfigValidationError,
    DuplicatePluginError,
    InitializationFailedError,
    KernelError,
    KernelNotRunningError,
    LifecycleError,
    MissingDependencyError,
    PluginError,
    ShutdownError,
)
from .kernel import AssemblyKernel, KernelState, get_kernel, set_kernel
from .lifecycle import InitState, LazyResource
from .registry import FactoryContext, PluginRegistry, PluginSpec

__all__ = [
    "AssemblyKernel",
    "ConfigError",
    "ConfigSchema",
    "ConfigSource",
    "ConfigSourceFormatError",
    "ConfigSourceMissingError",
    "ConfigValidationError",
    "CircularDependencyError",
    "DuplicatePluginError",
    "EnvSource",
    "FactoryContext",
    "FieldSpec",
    "FileSource",
    "InitState",
    "InitializationFailedError",
    "KernelError",
    "KernelNotRunningError",
    "KernelState",
    "LayeredConfigLoader",
    "LazyResource",
    "LifecycleError",
    "MappingSource",
    "MissingDependencyError",
    "PluginError",
    "PluginRegistry",
    "PluginSpec",
    "ResolvedConfig",
    "ShutdownError",
    "get_kernel",
    "set_kernel",
]
