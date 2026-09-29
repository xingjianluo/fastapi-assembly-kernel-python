"""FastAPI 应用装配内核：分层配置 / 依赖注入式插件 / 惰性初始化 / 停机与热重载。"""

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
    LifecycleError,
    MissingDependencyError,
    PluginError,
    ShutdownError,
)
from .kernel import AssemblyKernel, get_kernel, set_kernel
from .lifecycle import InitState, LazyResource, SettledOnce
from .registry import FactoryContext, PluginRegistry, PluginSpec, PluginTeardown
from .shutdown import ShutdownSession

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
    "LayeredConfigLoader",
    "LazyResource",
    "LifecycleError",
    "MappingSource",
    "MissingDependencyError",
    "PluginError",
    "PluginRegistry",
    "PluginSpec",
    "PluginTeardown",
    "ResolvedConfig",
    "SettledOnce",
    "ShutdownError",
    "ShutdownSession",
    "get_kernel",
    "set_kernel",
]
