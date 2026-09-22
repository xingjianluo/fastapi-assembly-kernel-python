"""分层配置：来源抽象、优先级合并与类型校验。

优先级规则（从低到高应用，后者覆盖前者）::

    默认值 (FieldSpec.default)
        -> 按 priority 升序排列的各个 ConfigSource
           （priority 越大越优先；priority 相同在构造期即被拒绝）

错误暴露（绝不静默退回默认值）：

* 必需来源缺失            -> ConfigSourceMissingError
* 文件内容无法解析        -> ConfigSourceFormatError
* 未知字段 / 类型不匹配 /
  必需字段缺失            -> ConfigValidationError

敏感安全：所有错误信息与日志只出现字段名、来源名，
绝不包含字段值；secret 字段在 :meth:`ResolvedConfig.redacted` 中脱敏。
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    ConfigSourceFormatError,
    ConfigSourceMissingError,
    ConfigValidationError,
)

__all__ = [
    "FieldSpec",
    "ConfigSchema",
    "ConfigSource",
    "FileSource",
    "EnvSource",
    "MappingSource",
    "ResolvedConfig",
    "LayeredConfigLoader",
]

logger = logging.getLogger("fastapi_assembly_kernel.config")

_MISSING = object()

# bool 是 int 的子类型，需要严格区分
_PARSE_TRUE = frozenset({"true", "1", "yes", "on"})
_PARSE_FALSE = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class FieldSpec:
    """单个配置字段的声明。

    required=True 时必须由某个来源提供；
    required=False 时必须提供 default（default 可以为 None）。
    secret=True 的字段值不会进入日志或异常信息。
    """

    name: str
    type: type
    default: Any = _MISSING
    required: bool = False
    secret: bool = False

    def __post_init__(self) -> None:
        if self.type not in (str, int, float, bool):
            raise ValueError(
                f"field {self.name!r}: 仅支持标量类型 str/int/float/bool，"
                f"得到 {self.type!r}"
            )
        if not self.required and self.default is _MISSING:
            raise ValueError(f"field {self.name!r}: 可选字段必须提供 default")
        if self.required and self.default is not _MISSING:
            raise ValueError(f"field {self.name!r}: required 字段不应设置 default")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("field name 必须是非空字符串")


@dataclass(frozen=True)
class ConfigSchema:
    """配置字段集合；字段名集合即白名单，未声明的键一律拒绝。"""

    fields: tuple[FieldSpec, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for f in self.fields:
            if f.name in seen:
                raise ValueError(f"schema 中字段重复: {f.name!r}")
            seen.add(f.name)

    def get(self, name: str) -> FieldSpec | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None


class ResolvedConfig:
    """合并并校验后的只读配置结果。

    repr/str/日志均输出脱敏内容，防止敏感值意外泄漏。
    """

    __slots__ = ("_schema", "_values")

    def __init__(self, schema: ConfigSchema, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_values", dict(values))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResolvedConfig):
            return NotImplemented
        return self._values == other._values and self._schema == other._schema

    def __repr__(self) -> str:
        return f"ResolvedConfig({self.redacted()!r})"

    def get(self, name: str) -> Any:
        spec = self._schema.get(name)
        if spec is None:
            raise KeyError(name)
        return self._values[name]

    def as_dict(self) -> dict[str, Any]:
        """返回值的副本（含真实值，仅供进程内业务代码使用）。"""
        return dict(self._values)

    def redacted(self) -> dict[str, Any]:
        """字段名 -> 值；secret 字段固定输出 ``***``。"""
        out: dict[str, Any] = {}
        for f in self._schema.fields:
            value = self._values[f.name]
            out[f.name] = "***" if f.secret else value
        return out


class ConfigSource:
    """配置来源基类。

    priority 越大优先级越高；required 来源缺失时抛
    :class:`ConfigSourceMissingError`。
    子类通过 :attr:`produces_strings` 声明原始值是否恒为字符串
    （环境变量场景），加载器会据此做严格解析而非类型比较。
    """

    produces_strings = False

    def __init__(self, *, name: str, priority: int, required: bool = False) -> None:
        if not name:
            raise ValueError("source name 必须是非空字符串")
        self.name = name
        self.priority = priority
        self.required = required

    def load(self) -> dict[str, Any] | None:
        """读取并解析为 ``{字段名: 原始值}``。

        返回 None 表示该来源不存在（仅允许 required=False）；
        内容无法解析时抛 :class:`ConfigSourceFormatError`。
        """
        raise NotImplementedError


class MappingSource(ConfigSource):
    """内存字典来源，便于注入默认层与单元测试。"""

    def __init__(
        self,
        mapping: dict[str, Any],
        *,
        name: str,
        priority: int,
        required: bool = False,
    ) -> None:
        super().__init__(name=name, priority=priority, required=required)
        self._mapping = dict(mapping)

    def load(self) -> dict[str, Any] | None:
        if self.required and not self._mapping:
            raise ConfigSourceMissingError(
                f"必需配置来源 {self.name!r} 未提供任何配置项"
            )
        return dict(self._mapping)


class EnvSource(ConfigSource):
    """环境变量来源。

    映射规则：字段名 ``db_url`` 对应环境变量 ``<PREFIX>DB_URL``，
    即前缀 + 字段名大写。环境变量值恒为字符串，加载器按字段声明类型
    做严格解析，解析失败按类型不匹配处理。
    """

    produces_strings = True

    def __init__(
        self,
        *,
        prefix: str,
        priority: int,
        required: bool = False,
        name: str | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            name=name or f"env[{prefix}*]", priority=priority, required=required
        )
        if not prefix:
            raise ValueError("EnvSource prefix 必须非空")
        self._prefix = prefix
        self._environ = environ if environ is not None else dict(os.environ)

    def load(self) -> dict[str, Any] | None:
        out = {
            key[len(self._prefix) :].lower(): value
            for key, value in self._environ.items()
            if key.startswith(self._prefix)
        }
        if self.required and not out:
            raise ConfigSourceMissingError(
                f"必需配置来源 {self.name!r} 未匹配到任何环境变量"
            )
        return out


class FileSource(ConfigSource):
    """JSON / TOML 文件来源，格式由扩展名确定（.json/.toml）。"""

    def __init__(
        self,
        path: str | Path,
        *,
        priority: int,
        required: bool = False,
        name: str | None = None,
    ) -> None:
        path = Path(path)
        super().__init__(
            name=name or f"file:{path}", priority=priority, required=required
        )
        if path.suffix.lower() == ".json":
            self._format = "json"
        elif path.suffix.lower() == ".toml":
            self._format = "toml"
        else:
            raise ValueError(f"不支持的配置文件格式: {path.suffix!r}（仅 .json/.toml）")
        self._path = path

    def load(self) -> dict[str, Any] | None:
        if not self._path.is_file():
            if self.required:
                raise ConfigSourceMissingError(
                    f"必需配置文件不存在: {self._path}"
                )
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
            if self._format == "json":
                data = json.loads(raw)
            else:
                data = tomllib.loads(raw)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigSourceFormatError(
                f"配置来源 {self.name!r} 内容不是合法的 "
                f"{self._format.upper()}: {exc.__class__.__name__}"
            ) from exc
        except OSError as exc:
            raise ConfigSourceMissingError(
                f"配置文件 {self._path} 无法读取: {exc.__class__.__name__}"
            ) from exc
        if not isinstance(data, dict):
            raise ConfigSourceFormatError(
                f"配置来源 {self.name!r} 顶层结构必须是对象/表，"
                f"实际为 {type(data).__name__}"
            )
        return data


def _parse_scalar(spec: FieldSpec, raw: Any, source: ConfigSource) -> Any:
    """把来源原始值转换/校验为字段声明类型，失败一律抛 ConfigValidationError。"""
    expected = spec.type
    if source.produces_strings:
        if not isinstance(raw, str):
            raise ConfigValidationError(
                f"配置字段 {spec.name!r}（来源 {source.name!r}）："
                f"环境变量值必须是字符串"
            )
        text = raw.strip()
        try:
            if expected is bool:
                low = text.lower()
                if low in _PARSE_TRUE:
                    return True
                if low in _PARSE_FALSE:
                    return False
                raise ValueError
            if expected is int:
                return int(text, 10)
            if expected is float:
                return float(text)
            return raw
        except ValueError:
            raise ConfigValidationError(
                f"配置字段 {spec.name!r}（来源 {source.name!r}）："
                f"无法解析为 {expected.__name__}"
            ) from None

    if isinstance(raw, dict) or isinstance(raw, list):
        raise ConfigValidationError(
            f"配置字段 {spec.name!r}（来源 {source.name!r}）："
            f"仅支持标量值，得到 {type(raw).__name__}"
        )
    if expected is bool:
        if isinstance(raw, bool):
            return raw
    elif expected is int:
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    elif expected is float:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    elif expected is str:
        if isinstance(raw, str):
            return raw
    else:  # pragma: no cover - schema 构造期已保证
        raise ConfigValidationError(f"配置字段 {spec.name!r}: 不支持的类型")
    raise ConfigValidationError(
        f"配置字段 {spec.name!r}（来源 {source.name!r}）："
        f"类型不匹配，期望 {expected.__name__}，实际 {type(raw).__name__}"
    )


class LayeredConfigLoader:
    """按确定性优先级合并多个来源并做字段校验。"""

    def __init__(self, schema: ConfigSchema, sources: list[ConfigSource]) -> None:
        priorities = [s.priority for s in sources]
        if len(set(priorities)) != len(priorities):
            raise ValueError("配置来源 priority 必须互不相同，以保证优先级确定")
        self._schema = schema
        # 升序：从低优先级到高优先级依次应用
        self._sources = sorted(sources, key=lambda s: s.priority)
        logger.debug(
            "config 来源优先级（低->高）: %s",
            " -> ".join(f"{s.name}@{s.priority}" for s in self._sources),
        )

    def load(self) -> ResolvedConfig:
        # 字段名 -> (最高优先级来源, 已转换值)
        chosen: dict[str, tuple[ConfigSource, Any]] = {}
        for source in self._sources:
            data = source.load()
            if data is None:
                logger.debug("配置来源 %s 不存在，跳过", source.name)
                continue
            logger.debug(
                "配置来源 %s@%d 提供字段: %s",
                source.name,
                source.priority,
                sorted(data.keys()),
            )
            for key, raw in data.items():
                spec = self._schema.get(key)
                if spec is None:
                    raise ConfigValidationError(
                        f"未知配置字段 {key!r}（来源 {source.name!r}）："
                        f"未在 schema 中声明"
                    )
                value = _parse_scalar(spec, raw, source)
                prev = chosen.get(key)
                chosen[key] = (source, value)
                logger.debug(
                    "字段 %s 取值来源 %s@%d（%s 先前来源 %s）",
                    key,
                    source.name,
                    source.priority,
                    "覆盖" if prev else "设定",
                    prev[0].name if prev else "-",
                )

        values: dict[str, Any] = {}
        for spec in self._schema.fields:
            if spec.name in chosen:
                values[spec.name] = chosen[spec.name][1]
            elif spec.required:
                raise ConfigValidationError(
                    f"必需配置字段 {spec.name!r} 未被任何来源提供"
                )
            else:
                values[spec.name] = spec.default
        resolved = ResolvedConfig(self._schema, values)
        logger.debug("配置解析完成: %r", resolved)
        return resolved
