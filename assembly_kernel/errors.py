"""装配内核的可区分异常类型。

所有内核错误均继承 :class:`KernelError`，调用方可按具体子类区分失败原因：

配置层
    :class:`ConfigSourceError` 来源缺失/无法读取（子类 :class:`ConfigSourceMissingError`）
    :class:`ConfigFormatError`  内容格式非法
    :class:`ConfigTypeError`    字段类型不匹配
    :class:`ConfigValidationError` 未通过字段校验

插件层
    :class:`PluginRegistrationError` 注册相关（子类重复注册/未知依赖）
    :class:`DuplicatePluginError` 同名插件重复注册
    :class:`MissingDependencyError` 依赖未注册
    :class:`CircularDependencyError` 依赖图存在环

生命周期
    :class:`InitializationError` 初始化失败的统一包装
"""

from __future__ import annotations


class KernelError(Exception):
    """装配内核所有错误的基类。"""


# ---------------------------------------------------------------------------
# 配置层错误
# ---------------------------------------------------------------------------


class ConfigError(KernelError):
    """配置层错误基类。"""


class ConfigSourceError(ConfigError):
    """配置来源无法被读取。

    :param source: 来源标识（名称），用于区分具体来源。
    """

    def __init__(self, source: str, reason: str = "") -> None:
        self.source = source
        self.reason = reason
        message = f"配置来源不可用: {source}"
        if reason:
            message = f"{message} ({reason})"
        super().__init__(message)


class ConfigSourceMissingError(ConfigSourceError):
    """必填配置来源缺失（如指定的文件不存在）。"""


class ConfigFormatError(ConfigError):
    """配置来源内容格式非法（如 JSON/TOML 解析失败）。"""

    def __init__(self, source: str, reason: str = "") -> None:
        self.source = source
        self.reason = reason
        message = f"配置来源格式非法: {source}"
        if reason:
            message = f"{message} ({reason})"
        super().__init__(message)


class ConfigTypeError(ConfigError):
    """配置字段的运行时类型与声明不匹配。"""

    def __init__(self, key: str, expected: str, actual_type: str) -> None:
        self.key = key
        self.expected = expected
        self.actual_type = actual_type
        # 注意：错误信息只包含字段名与类型，绝不包含字段值，避免泄露敏感值。
        super().__init__(
            f"配置字段类型不匹配: '{key}'，期望 {expected}，实际为 {actual_type}"
        )


class ConfigValidationError(ConfigError):
    """配置字段存在但未通过校验（如必填值为空）。"""

    def __init__(self, key: str, rule: str) -> None:
        self.key = key
        self.rule = rule
        super().__init__(f"配置字段未通过校验: '{key}'（规则: {rule}）")


# ---------------------------------------------------------------------------
# 插件层错误
# ---------------------------------------------------------------------------


class PluginError(KernelError):
    """插件层错误基类。"""


class DuplicatePluginError(PluginError):
    """同名插件被重复注册。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"插件重复注册: '{name}'")


class MissingDependencyError(PluginError):
    """插件声明的依赖在注册表中不存在。"""

    def __init__(self, plugin: str, dependency: str) -> None:
        self.plugin = plugin
        self.dependency = dependency
        super().__init__(f"插件 '{plugin}' 依赖的 '{dependency}' 未注册")


class CircularDependencyError(PluginError):
    """插件依赖图中存在循环。"""

    def __init__(self, cycle: list[str]) -> None:
        # 统一以有序列表表达环上的节点，保证错误信息确定、可解释。
        self.cycle = list(cycle)
        super().__init__(f"检测到循环依赖: {' -> '.join(self.cycle)}")


# ---------------------------------------------------------------------------
# 生命周期错误
# ---------------------------------------------------------------------------


class InitializationError(KernelError):
    """惰性资源初始化失败的统一包装。

    原始异常通过 ``__cause__`` 保留（使用 ``raise ... from exc``）。
    """

    def __init__(self, resource: str, message: str = "初始化失败") -> None:
        self.resource = resource
        super().__init__(f"资源 '{resource}' {message}")
