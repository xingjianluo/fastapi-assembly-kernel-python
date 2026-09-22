"""FastAPI 应用装配内核：分层配置、插件装配、惰性生命周期。"""

from __future__ import annotations

from .config import (
    ConfigField,
    ConfigSource,
    DictSource,
    EnvSource,
    FileSource,
    LayeredConfig,
    load_layered_config,
)
from .errors import (
    CircularDependencyError,
    ConfigError,
    ConfigFormatError,
    ConfigSourceError,
    ConfigSourceMissingError,
    ConfigTypeError,
    ConfigValidationError,
    DuplicatePluginError,
    InitializationError,
    KernelError,
    MissingDependencyError,
    PluginError,
)
from .lifecycle import LazyResource, ResourceState
from .plugins import (
    AssembledContext,
    Plugin,
    PluginRegistry,
    assemble,
    topological_order,
)

__all__ = [
    # config
    "ConfigField",
    "ConfigSource",
    "DictSource",
    "EnvSource",
    "FileSource",
    "LayeredConfig",
    "load_layered_config",
    # errors
    "KernelError",
    "ConfigError",
    "ConfigSourceError",
    "ConfigSourceMissingError",
    "ConfigFormatError",
    "ConfigTypeError",
    "ConfigValidationError",
    "PluginError",
    "DuplicatePluginError",
    "MissingDependencyError",
    "CircularDependencyError",
    "InitializationError",
    # lifecycle
    "ResourceState",
    "LazyResource",
    # plugins
    "Plugin",
    "PluginRegistry",
    "AssembledContext",
    "assemble",
    "topological_order",
]
