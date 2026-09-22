"""内核可区分异常类型（骨架）。"""

from __future__ import annotations


class KernelError(Exception):
    """装配内核所有错误的基类。"""


class ConfigError(KernelError):
    """配置加载与校验错误的基类。"""


class ConfigSourceMissingError(ConfigError):
    """配置来源不可用（必需来源缺失）。"""


class ConfigSourceFormatError(ConfigError):
    """配置来源内容无法解析为合法格式。"""


class ConfigValidationError(ConfigError):
    """配置字段类型不匹配或结构非法。"""


class PluginError(KernelError):
    """插件注册与装配错误的基类。"""


class DuplicatePluginError(PluginError):
    """同名插件重复注册。"""


class MissingDependencyError(PluginError):
    """插件声明的依赖不存在。"""


class CircularDependencyError(PluginError):
    """插件依赖图中存在环。"""


class LifecycleError(KernelError):
    """生命周期错误的基类。"""


class InitializationFailedError(LifecycleError):
    """惰性初始化执行失败；清除状态后允许重试。"""
