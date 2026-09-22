"""分层配置：优先级冲突、非法配置、敏感值脱敏。"""

from __future__ import annotations

import json

import pytest

from fastapi_assembly_kernel import (
    ConfigError,
    ConfigSchema,
    ConfigSourceFormatError,
    ConfigSourceMissingError,
    ConfigValidationError,
    EnvSource,
    FieldSpec,
    FileSource,
    LayeredConfigLoader,
    MappingSource,
)


def make_schema() -> ConfigSchema:
    return ConfigSchema(
        (
            FieldSpec("app_name", str, default="default-app"),
            FieldSpec("port", int, default=8000),
            FieldSpec("ratio", float, default=0.5),
            FieldSpec("debug", bool, default=False),
            FieldSpec("api_key", str, required=True, secret=True),
        )
    )


def test_priority_layers_highest_wins(tmp_path, case_log):
    """同一字段在多层来源冲突时，priority 最高者胜出。"""
    f = tmp_path / "conf.json"
    f.write_text(json.dumps({"app_name": "from-file", "port": 9000, "api_key": "k-file"}))
    inputs = {
        "defaults": {"port": 8000},
        "file(priority=10)": {"app_name": "from-file", "port": 9000},
        "env(priority=20)": {"APP_PORT": "7000", "APP_API_KEY": "k-env"},
    }
    case_log("优先级冲突", inputs, "priority 越大越优先：env(20) > file(10) > 默认值")

    cfg = LayeredConfigLoader(
        make_schema(),
        [
            MappingSource({"port": 8000}, name="defaults", priority=0),
            FileSource(f, priority=10),
            EnvSource(prefix="APP_", priority=20, environ=inputs["env(priority=20)"]),
        ],
    ).load()

    assert cfg.get("port") == 7000          # env 覆盖 file 覆盖默认
    assert cfg.get("app_name") == "from-file"  # 仅 file 提供
    assert cfg.get("api_key") == "k-env"
    assert cfg.get("ratio") == 0.5          # 仅默认值


def test_same_priority_rejected(case_log):
    case_log("同优先级拒绝", "两个 priority=1 的来源",
             "优先级必须唯一，否则合并结果不可解释 -> 构造期 ValueError")
    with pytest.raises(ValueError):
        LayeredConfigLoader(
            make_schema(),
            [
                MappingSource({}, name="a", priority=1),
                MappingSource({}, name="b", priority=1),
            ],
        )


def test_missing_required_source(case_log):
    case_log("必需来源缺失", "FileSource('/no/such.json', required=True)",
             "必需来源缺失必须报 ConfigSourceMissingError，不得静默跳过")
    with pytest.raises(ConfigSourceMissingError):
        LayeredConfigLoader(
            make_schema(),
            [FileSource("/no/such/definitely-missing.json", priority=1, required=True)],
        ).load()


def test_missing_optional_source_skipped(case_log):
    case_log("可选来源缺失", "FileSource(不存在, required=False)",
             "可选来源缺失被跳过，其余来源照常生效")
    cfg = LayeredConfigLoader(
        make_schema(),
        [
            FileSource("/no/such/optional.json", priority=1),
            MappingSource({"api_key": "k"}, name="m", priority=2),
        ],
    ).load()
    assert cfg.get("api_key") == "k"
    assert cfg.get("port") == 8000


def test_invalid_json_format(tmp_path, case_log):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    case_log("非法 JSON", bad.read_text(),
             "内容无法解析 -> ConfigSourceFormatError，不得退回默认值")
    with pytest.raises(ConfigSourceFormatError):
        LayeredConfigLoader(
            make_schema(), [FileSource(bad, priority=1, required=True)]
        ).load()


def test_invalid_toml_format(tmp_path, case_log):
    bad = tmp_path / "bad.toml"
    bad.write_text("key = = value")
    case_log("非法 TOML", bad.read_text(),
             "内容无法解析 -> ConfigSourceFormatError")
    with pytest.raises(ConfigSourceFormatError):
        LayeredConfigLoader(
            make_schema(), [FileSource(bad, priority=1, required=True)]
        ).load()


def test_type_mismatch_mapping(case_log):
    case_log("类型不匹配", {"port": "not-an-int"},
             "非 env 来源不做隐式转换 -> ConfigValidationError")
    with pytest.raises(ConfigValidationError):
        LayeredConfigLoader(
            make_schema(),
            [MappingSource({"port": "not-an-int", "api_key": "k"}, name="m", priority=1)],
        ).load()


def test_type_mismatch_env_unparseable(case_log):
    case_log("env 解析失败", {"APP_PORT": "abc", "APP_API_KEY": "k"},
             "env 值按声明类型严格解析，失败 -> ConfigValidationError")
    with pytest.raises(ConfigValidationError):
        LayeredConfigLoader(
            make_schema(),
            [EnvSource(prefix="APP_", priority=1,
                       environ={"APP_PORT": "abc", "APP_API_KEY": "k"})],
        ).load()


def test_unknown_key_rejected(case_log):
    case_log("未知字段", {"nope": 1},
             "未在 schema 声明的键一律拒绝 -> ConfigValidationError")
    with pytest.raises(ConfigValidationError):
        LayeredConfigLoader(
            make_schema(),
            [MappingSource({"nope": 1, "api_key": "k"}, name="m", priority=1)],
        ).load()


def test_required_field_missing(case_log):
    case_log("必需字段缺失", "所有来源都不提供 api_key",
             "required 字段无来源 -> ConfigValidationError，不得静默置空")
    with pytest.raises(ConfigValidationError):
        LayeredConfigLoader(
            make_schema(), [MappingSource({}, name="m", priority=1)]
        ).load()


def test_env_scalar_parsing(case_log):
    env = {
        "APP_PORT": "1234",
        "APP_RATIO": "0.25",
        "APP_DEBUG": "yes",
        "APP_API_KEY": "k",
        "APP_APP_NAME": "env-app",
    }
    case_log("env 标量解析", env, "int/float/bool/str 按声明类型解析")
    cfg = LayeredConfigLoader(
        make_schema(), [EnvSource(prefix="APP_", priority=1, environ=env)]
    ).load()
    assert cfg.get("port") == 1234
    assert cfg.get("ratio") == 0.25
    assert cfg.get("debug") is True
    assert cfg.get("app_name") == "env-app"


def test_secret_never_leaks(case_log):
    secret = "super-secret-token-xyz"
    case_log("敏感值脱敏", {"APP_API_KEY": secret},
             "repr/redacted/错误信息中均不得出现敏感值")
    cfg = LayeredConfigLoader(
        make_schema(),
        [EnvSource(prefix="APP_", priority=1, environ={"APP_API_KEY": secret})],
    ).load()
    assert cfg.get("api_key") == secret
    assert cfg.redacted()["api_key"] == "***"
    assert secret not in repr(cfg)
    assert secret not in str(cfg.redacted())

    # 类型错误信息也不得包含原始值
    secret_schema = ConfigSchema(
        (FieldSpec("api_key", int, required=True, secret=True),)
    )
    try:
        LayeredConfigLoader(
            secret_schema,
            [EnvSource(prefix="APP_", priority=1, environ={"APP_API_KEY": secret})],
        ).load()
        raise AssertionError("应抛出 ConfigValidationError")
    except ConfigValidationError as exc:
        assert secret not in str(exc)


def test_error_types_distinguishable(case_log):
    case_log("错误可区分", "三类配置错误",
             "ConfigSourceMissingError / ConfigSourceFormatError / "
             "ConfigValidationError 互不包含，且同属 ConfigError")
    assert issubclass(ConfigSourceMissingError, ConfigError)
    assert issubclass(ConfigSourceFormatError, ConfigError)
    assert issubclass(ConfigValidationError, ConfigError)
    for a, b in [
        (ConfigSourceMissingError, ConfigSourceFormatError),
        (ConfigSourceFormatError, ConfigValidationError),
        (ConfigSourceMissingError, ConfigValidationError),
    ]:
        assert not issubclass(a, b) and not issubclass(b, a)
