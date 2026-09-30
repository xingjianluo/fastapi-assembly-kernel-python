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


class KernelNotRunningError(LifecycleError):
    """在内核未处于运行状态时执行了仅运行期可用的操作（如配置热重载）。"""


class ShutdownError(LifecycleError):
    """优雅停机期间一个或多个组件释放失败。

    释放按逆装配顺序全部尝试（失败不跳过其他组件），
    本异常在停机结束时汇总抛出；``failures`` 记录
    ``{插件名: 原始异常}``，便于逐个区分处理。

    同一轮停机的发起方与所有并入方拿到的是同一个异常对象。
    汇总消息只包含插件名与异常类型名，不含异常文本——teardown
    抛出的原始异常消息可能携带密钥等敏感内容；原始异常仍完整
    保留在 ``failures`` 中供进程内代码判定。
    """

    def __init__(self, failures: dict[str, BaseException]) -> None:
        self.failures: dict[str, BaseException] = dict(failures)
        detail = ", ".join(
            f"{name}({type(exc).__name__})" for name, exc in sorted(self.failures.items())
        )
        super().__init__(f"停机期间 {len(self.failures)} 个组件释放失败: {detail}")
