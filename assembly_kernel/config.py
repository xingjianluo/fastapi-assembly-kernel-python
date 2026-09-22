"""分层配置：多来源加载、确定性合并、严格校验。

优先级（数值越大优先级越高，后者覆盖前者）::

    0. 字段声明内默认值 (defaults)
    1. 配置文件 (file)
    2. 环境变量 (env)
    3. 显式覆盖 (override)

高层来源的*已定义*键覆盖低层键；同优先级按来源传入顺序后者胜出。
来源缺失 / 格式非法 / 字段类型不匹配均抛出 :mod:`assembly_kernel.errors`
中的可区分错误，绝不静默回退到默认值。敏感字段（``secret=True``）的值
不会出现在日志、异常信息、``repr`` 或脱敏后的配置视图中。
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable, get_args, get_origin

from .errors import (
    ConfigError,
    ConfigFormatError,
    ConfigSourceError,
    ConfigSourceMissingError,
    ConfigTypeError,
    ConfigValidationError,
)

__all__ = [
    "ConfigField",
    "ConfigSource",
    "DictSource",
    "EnvSource",
    "FileSource",
    "LayeredConfig",
    "load_layered_config",
]

_logger = logging.getLogger("assembly_kernel.config")

# 哨兵：表示字段没有声明默认值（区别于显式默认值 ``None``）。
_MISSING: Any = object()

# bool 是 int 的子类，标量判定时必须先判 bool。
_SCALAR_TYPES = (str, int, float, bool)
_BOOL_LITERALS = {
    "true": True,
    "false": False,
    "1": True,
    "0": False,
    "yes": True,
    "no": False,
    "on": True,
    "off": False,
}


# ---------------------------------------------------------------------------
# 字段声明
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigField:
    """配置字段的声明式定义。

    :param key: 规范键名，点分路径表示嵌套层级，如 ``db.pool.size``。
    :param type_: 值类型，支持 ``str/int/float/bool``，以及
        ``list[X]`` / ``dict[str, X]``（可嵌套）。
    :param default: 默认值；未提供（``MISSING``）时配合 ``required`` 判定。
    :param required: 无任何来源提供该键时，``True`` 则启动失败。
    :param secret: 为 ``True`` 时值在日志、异常与脱敏视图中以 ``***`` 屏蔽。
    :param env_var: 显式绑定的环境变量名；默认 ``APP_`` + 键名大写，
        其中非字母数字字符统一替换为下划线。
    """

    key: str
    type_: Any = str
    default: Any = _MISSING
    required: bool = True
    secret: bool = False
    env_var: str | None = None

    def default_env_var(self) -> str:
        """返回该字段默认绑定的环境变量名。"""
        return "APP_" + re.sub(r"[^A-Za-z0-9]+", "_", self.key).upper().strip("_")

    def resolved_env_var(self) -> str:
        """实际生效的环境变量名（显式绑定优先）。"""
        return self.env_var or self.default_env_var()

    def mask(self, value: Any) -> str:
        """值的可安全打印形式：敏感字段一律 ``***``；其余使用 ``repr``。"""
        if self.secret:
            return "***"
        text = repr(value)
        return text if len(text) <= 80 else text[:77] + "..."


# ---------------------------------------------------------------------------
# 来源协议与内置来源
# ---------------------------------------------------------------------------


@runtime_checkable
class ConfigSource(Protocol):
    """配置来源协议。"""

    name: str
    priority: int

    def load(self) -> dict[str, Any]:
        """读取来源，返回*扁平点分键* -> 原始值的映射。

        来源缺失应抛 :class:`ConfigSourceMissingError`，
        格式非法应抛 :class:`ConfigFormatError`。
        """
        ...


@dataclass
class DictSource:
    """进程内字典来源，适合代码内默认值与显式覆盖。

    ``data`` 必须使用扁平点分键（如 ``{"db.host": "x"}``）。
    """

    name: str
    priority: int
    data: Mapping[str, Any] = field(default_factory=dict)

    def load(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.data.items():
            if not isinstance(key, str):
                # 不打印任何值，仅报告键的类型。
                raise ConfigFormatError(
                    self.name, f"字典来源的键必须为字符串，实际为 {type(key).__name__}"
                )
            result[key] = value
        _logger.debug(
            "来源加载完成 name=%s priority=%s keys=%s",
            self.name,
            self.priority,
            sorted(result),
        )
        return result


@dataclass
class EnvSource:
    """环境变量来源。

    仅采集字段声明绑定的环境变量名，未知环境变量一律忽略。字符串原始值
    依据字段声明类型解析：标量直接转换，容器类型按 JSON 解析；解析失败
    抛 :class:`ConfigFormatError`（错误信息只含变量名，绝不含变量值）。
    """

    name: str
    priority: int
    fields: Sequence[ConfigField]
    environ: Mapping[str, str] | None = None

    def _env(self) -> Mapping[str, str]:
        import os

        return self.environ if self.environ is not None else os.environ

    def load(self) -> dict[str, Any]:
        env = self._env()
        result: dict[str, Any] = {}
        for f in self.fields:
            var = f.resolved_env_var()
            raw = env.get(var)
            if raw is None:
                continue
            result[f.key] = _parse_env_value(raw, f, self.name, var)
        _logger.debug(
            "来源加载完成 name=%s priority=%s keys=%s",
            self.name,
            self.priority,
            sorted(result),
        )
        return result


@dataclass
class FileSource:
    """配置文件来源（按后缀解析 ``.json`` / ``.toml``）。

    :param required: 为 ``True`` 时文件不存在抛
        :class:`ConfigSourceMissingError`；为 ``False`` 时跳过该来源。
    """

    name: str
    priority: int
    path: Path
    required: bool = True

    def load(self) -> dict[str, Any]:
        path = Path(self.path)
        if not path.exists():
            if self.required:
                raise ConfigSourceMissingError(
                    self.name, f"文件不存在: {path}"
                )
            _logger.debug("可选来源缺失，跳过 name=%s path=%s", self.name, path)
            return {}
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            # 只保留操作系统给出的错误类别（如 Permission denied），不含文件内容。
            raise ConfigSourceError(self.name, exc.strerror or str(exc)) from exc

        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                parsed = json.loads(raw_bytes.decode("utf-8"))
            elif suffix == ".toml":
                parsed = tomllib.loads(raw_bytes.decode("utf-8"))
            else:
                raise ConfigFormatError(
                    self.name, f"不支持的配置文件类型: {suffix or '<无后缀>'}"
                )
        except ConfigFormatError:
            raise
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            # 定位信息可帮助排查，且不包含任何配置值。
            line = getattr(exc, "lineno", "?")
            col = getattr(exc, "colno", "?")
            raise ConfigFormatError(
                self.name, f"解析失败（约第 {line} 行第 {col} 列）: {exc.msg}"
            ) from exc

        if not isinstance(parsed, dict):
            raise ConfigFormatError(
                self.name, f"顶层必须是表/对象，实际为 {type(parsed).__name__}"
            )
        result = _flatten(parsed, prefix="")
        _logger.debug(
            "来源加载完成 name=%s priority=%s path=%s keys=%s",
            self.name,
            self.priority,
            path,
            sorted(result),
        )
        return result


# ---------------------------------------------------------------------------
# 合并视图
# ---------------------------------------------------------------------------


class LayeredConfig:
    """分层配置视图：持有合并后的配置，提供类型安全的读取访问。"""

    def __init__(
        self,
        fields: Sequence[ConfigField],
        values: Mapping[str, Any],
        provenance: Mapping[str, str],
    ) -> None:
        self._fields: dict[str, ConfigField] = {f.key: f for f in fields}
        self._values: dict[str, Any] = dict(values)
        self._provenance: dict[str, str] = dict(provenance)

    def get(self, key: str) -> Any:
        """读取已校验的配置值。

        未声明的键抛 :class:`ConfigValidationError`；可选字段缺省时返回 ``None``。
        """
        if key not in self._fields:
            raise ConfigValidationError(key, "未在字段声明中注册的配置键")
        return self._values.get(key)

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """返回嵌套字典形式；``redact=True`` 时敏感值以 ``***`` 呈现。"""
        root: dict[str, Any] = {}
        for key in sorted(self._values):
            field = self._fields[key]
            value = "***" if redact and field.secret else self._values[key]
            parts = key.split(".")
            cur = root
            for part in parts[:-1]:
                nxt = cur.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[part] = nxt
                cur = nxt
            cur[parts[-1]] = value
        return root

    def explain(self) -> list[tuple[str, str, bool]]:
        """返回 ``(键, 生效来源名, 是否为字段默认值)`` 的确定性解释列表。

        顺序与字段声明顺序一致；可选字段缺省时来源名为 ``"<none>"``。
        """
        rows: list[tuple[str, str, bool]] = []
        for field in self._fields.values():
            source = self._provenance.get(field.key, "<none>")
            rows.append((field.key, source, source == "defaults"))
        return rows

    def provenance_of(self, key: str) -> str:
        """返回某键最终生效值的来源名（不含值本身）。"""
        if key not in self._fields:
            raise ConfigValidationError(key, "未在字段声明中注册的配置键")
        return self._provenance.get(key, "<none>")

    def __repr__(self) -> str:
        # repr 中绝不出现任何配置值。
        return f"LayeredConfig(keys={sorted(self._fields)!r})"


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------


def load_layered_config(
    fields: Sequence[ConfigField],
    sources: Sequence[ConfigSource],
) -> LayeredConfig:
    """加载并合并所有配置来源，随后按字段声明做严格校验。

    异常可区分：

    * 字段声明本身非法（重名、重复环境变量、不支持的类型）→ :class:`ConfigError`
    * 必填来源缺失 / 不可读 → :class:`ConfigSourceMissingError` /
      :class:`ConfigSourceError`
    * 来源内容非法 → :class:`ConfigFormatError`
    * 字段类型不匹配 → :class:`ConfigTypeError`（不含字段值）
    * 必填字段缺失或出现未声明键 → :class:`ConfigValidationError`
    """
    declared = list(fields)

    # 1) 字段声明的确定性静态检查（与输入顺序无关地给出明确错误）。
    seen_keys: set[str] = set()
    seen_env: dict[str, str] = {}
    for f in declared:
        if f.key in seen_keys:
            raise ConfigError(f"配置字段重复声明: '{f.key}'")
        seen_keys.add(f.key)
        _check_supported_type(f.type_, f.key)
        var = f.resolved_env_var()
        if var in seen_env:
            raise ConfigError(
                f"环境变量 {var} 被多个字段绑定: '{seen_env[var]}' 与 '{f.key}'"
            )
        seen_env[var] = f.key

    # 2) 默认值层（priority 最低）。
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for f in declared:
        if f.default is not _MISSING:
            merged[f.key] = f.default
            provenance[f.key] = "defaults"

    # 3) 按优先级稳定施加来源：sorted 稳定，同优先级保持传入顺序。
    ordered = sorted(sources, key=lambda s: s.priority)
    for source in ordered:
        data = source.load()  # 来源错误直接向上传播，不静默回退
        for key in sorted(data):  # 遍历顺序确定，便于日志判定
            if key in merged:
                _logger.debug("配置覆盖 key=%s source=%s", key, source.name)
            merged[key] = data[key]
            provenance[key] = source.name

    # 4) 严格校验：未声明键直接拒绝（捕捉拼写错误，避免静默忽略）。
    for key in sorted(merged):
        if key not in seen_keys:
            raise ConfigValidationError(key, "来源提供了未声明的配置键")

    # 5) 逐字段做存在性与类型校验。
    values: dict[str, Any] = {}
    for f in declared:
        if f.key not in merged:
            if f.required:
                raise ConfigValidationError(f.key, "必填字段缺失且无默认值")
            # 可选字段缺省：视图中记为 None，provenance 留空。
            values[f.key] = None
            continue
        value = merged[f.key]
        _validate_type(value, f.type_, f.key)
        # float 字段接受 int 输入，归一化为 float，保证读取类型稳定。
        if f.type_ is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        values[f.key] = value
        _logger.debug(
            "配置判定 key=%s source=%s secret=%s",
            f.key,
            provenance.get(f.key),
            f.secret,
        )

    return LayeredConfig(declared, values, provenance)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _type_name(type_: Any) -> str:
    """生成类型的可读名称，用于错误信息（如 ``list[int]``）。"""
    origin = get_origin(type_)
    if origin is None:
        return getattr(type_, "__name__", repr(type_))
    args = get_args(type_)
    if origin is list:
        inner = _type_name(args[0]) if args else "Any"
        return f"list[{inner}]"
    if origin is dict:
        if len(args) == 2:
            return f"dict[{_type_name(args[0])}, {_type_name(args[1])}]"
        return "dict"
    return getattr(origin, "__name__", repr(origin))


def _check_supported_type(type_: Any, key: str) -> None:
    """启动期检查字段声明的类型是否被支持（不支持即确定性编程错误）。"""
    origin = get_origin(type_)
    if origin is None:
        if type_ not in _SCALAR_TYPES:
            raise ConfigError(
                f"字段 '{key}' 声明了不支持的标量类型: {_type_name(type_)}"
            )
        return
    if origin is list:
        args = get_args(type_)
        if not args:
            raise ConfigError(f"字段 '{key}' 的 list 类型必须声明元素类型")
        _check_supported_type(args[0], key + "[]")
        return
    if origin is dict:
        args = get_args(type_)
        if len(args) != 2 or args[0] is not str:
            raise ConfigError(f"字段 '{key}' 的字典类型必须为 dict[str, ...]")
        _check_supported_type(args[1], key + ".*")
        return
    raise ConfigError(f"字段 '{key}' 声明了不支持的类型: {_type_name(type_)}")


def _validate_type(value: Any, type_: Any, key: str) -> None:
    """对最终合并值做严格运行时类型校验，失败抛 :class:`ConfigTypeError`。"""
    expected = _type_name(type_)
    origin = get_origin(type_)

    if origin is None:
        if type_ is bool:
            ok = isinstance(value, bool)
        elif type_ is int:
            # 显式排除 bool（True 不是合法 int 配置值）。
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif type_ is float:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type_)
        if not ok:
            raise ConfigTypeError(key, expected, type(value).__name__)
        return

    if origin is list:
        if not isinstance(value, list):
            raise ConfigTypeError(key, expected, type(value).__name__)
        item_type = get_args(type_)[0]
        for index, item in enumerate(value):
            _validate_type(item, item_type, f"{key}[{index}]")
        return

    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigTypeError(key, expected, type(value).__name__)
        value_type = get_args(type_)[1]
        for sub_key, sub_value in value.items():
            if not isinstance(sub_key, str):
                raise ConfigTypeError(
                    f"{key}.<键>", "str", type(sub_key).__name__
                )
            _validate_type(sub_value, value_type, f"{key}.{sub_key}")
        return


def _parse_env_value(raw: str, field: ConfigField, source: str, var: str) -> Any:
    """把环境变量的字符串原始值解析为 Python 值（不含值进入错误信息）。"""
    origin = get_origin(field.type_)
    try:
        if origin is None:
            if field.type_ is bool:
                lowered = raw.strip().lower()
                if lowered not in _BOOL_LITERALS:
                    raise ConfigFormatError(
                        source,
                        f"环境变量 {var} 期望布尔值，允许 true/false/1/0/yes/no/on/off",
                    )
                return _BOOL_LITERALS[lowered]
            if field.type_ is int:
                return int(raw.strip())
            if field.type_ is float:
                return float(raw.strip())
            return raw
        # list / dict 统一走 JSON，保证嵌套结构可表达、语法确定。
        return json.loads(raw)
    except ConfigFormatError:
        raise
    except ValueError as exc:
        raise ConfigFormatError(
            source, f"环境变量 {var} 无法解析为 {_type_name(field.type_)}"
        ) from exc


def _flatten(value: Mapping[str, Any], *, prefix: str) -> dict[str, Any]:
    """把嵌套表扁平化为点分键；列表整体作为叶子值，不进入键路径。"""
    flat: dict[str, Any] = {}
    for key, sub in value.items():
        if not isinstance(key, str):
            raise ConfigFormatError("<file>", f"配置表键必须为字符串: {type(key).__name__}")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(sub, Mapping):
            flat.update(_flatten(sub, prefix=path))
        else:
            flat[path] = sub
    return flat
