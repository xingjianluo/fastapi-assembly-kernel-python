"""分层配置测试：优先级冲突、来源错误、类型错误、敏感值屏蔽、确定性。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from assembly_kernel import (
    ConfigField,
    DictSource,
    EnvSource,
    FileSource,
    load_layered_config,
)
from assembly_kernel.errors import (
    ConfigError,
    ConfigFormatError,
    ConfigSourceMissingError,
    ConfigTypeError,
    ConfigValidationError,
)

log = logging.getLogger("tests.config")

ALL_FIELDS = [
    ConfigField("app.name", str, default="demo", required=False),
    ConfigField("app.workers", int, default=1, required=False),
    ConfigField("app.ratio", float, default=0.5, required=False),
    ConfigField("app.flag", bool, default=False, required=False),
    ConfigField("db.host", str),
    ConfigField("db.password", str, secret=True),
    ConfigField("db.tags", list[str], default=[], required=False),
    ConfigField("db.meta", dict[str, int], default={}, required=False),
]


def _write_json(tmp_path: Path, payload: object, name: str = "config.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 优先级与合并
# ---------------------------------------------------------------------------


class TestLayeringPriority:
    def test_defaults_lowest_file_middle_env_higher_override_highest(self, tmp_path, case_log):
        path = _write_json(
            tmp_path,
            {
                "app": {"name": "from-file", "workers": 2},
                "db": {"host": "file-host", "password": "filepw"},
            },
        )
        case_log.input(
            file={"app.name": "from-file", "app.workers": 2, "db.host": "file-host"},
            env={"APP_DB_HOST": "env-host", "APP_APP_WORKERS": "3"},
            override={"app.workers": 9},
            rule="override > env > file > defaults",
        )
        case_log.expect(
            "db.host 被 env 覆盖；app.workers 被 override 覆盖；app.name 取 file"
        )

        cfg = load_layered_config(
            ALL_FIELDS,
            [
                DictSource("override", 30, {"app.workers": 9}),
                EnvSource(
                    "env",
                    20,
                    ALL_FIELDS,
                    environ={"APP_DB_HOST": "env-host", "APP_APP_WORKERS": "3"},
                ),
                FileSource("file", 10, path),
            ],
        )
        assert cfg.get("db.host") == "env-host"
        assert cfg.get("app.workers") == 9
        assert cfg.get("app.name") == "from-file"
        assert cfg.get("app.ratio") == 0.5
        assert cfg.provenance_of("app.workers") == "override"
        assert cfg.provenance_of("db.host") == "env"
        assert cfg.provenance_of("app.name") == "file"
        assert cfg.provenance_of("app.ratio") == "defaults"
        case_log.actual(f"explain={cfg.explain()}")

    def test_same_priority_last_source_wins(self, case_log):
        case_log.input(
            priority=10,
            sources_in_order=[{"a": 1}, {"a": 2}],
            rule="同优先级按传入顺序后者胜出（稳定）",
        )
        fields = [ConfigField("a", int)]
        cfg = load_layered_config(
            fields,
            [
                DictSource("s1", 10, {"a": 1}),
                DictSource("s2", 10, {"a": 2}),
            ],
        )
        assert cfg.get("a") == 2
        assert cfg.provenance_of("a") == "s2"
        case_log.actual("a=2, source=s2")

    def test_result_independent_of_source_argument_order(self, tmp_path, case_log):
        """来源以任意次序传入，合并结果只由 priority 决定。"""
        path = _write_json(tmp_path, {"db": {"host": "file-host", "password": "pw"}})
        sources = [
            DictSource("override", 30, {"db.host": "ov-host"}),
            FileSource("file", 10, path),
            EnvSource("env", 20, ALL_FIELDS, environ={"APP_DB_HOST": "env-host"}),
        ]
        permutations = [sources, list(reversed(sources)), [sources[2], sources[0], sources[1]]]
        values = []
        for i, ordered in enumerate(permutations):
            cfg = load_layered_config(ALL_FIELDS, ordered)
            values.append((cfg.get("db.host"), cfg.provenance_of("db.host")))
            log.info("排列%d: db.host=%s from=%s", i, cfg.get("db.host"), cfg.provenance_of("db.host"))
        assert values == [("ov-host", "override")] * 3
        case_log.actual("3 种来源排列结果一致")

    def test_explain_and_nested_view(self, tmp_path):
        path = _write_json(
            tmp_path, {"app": {"name": "x"}, "db": {"host": "h", "password": "secret-pw"}}
        )
        cfg = load_layered_config(ALL_FIELDS, [FileSource("file", 10, path)])
        view = cfg.as_dict()
        assert view["app"]["name"] == "x"
        assert view["db"]["password"] == "***"
        assert cfg.as_dict(redact=False)["db"]["password"] == "secret-pw"
        assert ("db.password", "file", False) in cfg.explain()


class TestConfigErrors:
    def test_required_source_missing_is_rejected(self, tmp_path, case_log):
        missing = tmp_path / "absent.json"
        case_log.input(path=str(missing), required=True)
        case_log.expect("ConfigSourceMissingError，而非静默使用默认值")
        with pytest.raises(ConfigSourceMissingError) as exc:
            load_layered_config(ALL_FIELDS, [FileSource("file", 10, missing)])
        assert exc.value.source == "file"
        case_log.actual(repr(exc.value))

    def test_optional_missing_source_is_skipped_but_required_field_still_fails(self, tmp_path, case_log):
        """可选文件缺失不报错；但必填字段没有值仍必须明确失败。"""
        with pytest.raises(ConfigValidationError, match="必填字段缺失"):
            load_layered_config(
                ALL_FIELDS, [FileSource("file", 10, tmp_path / "n.json", required=False)]
            )
        case_log.actual("可选来源跳过，必填 db.host 缺失 -> 启动错误")

    def test_malformed_json_is_format_error(self, tmp_path, case_log):
        path = tmp_path / "bad.json"
        path.write_text("{ not json", encoding="utf-8")
        case_log.input(file_bytes="{ not json")
        with pytest.raises(ConfigFormatError) as exc:
            load_layered_config(ALL_FIELDS, [FileSource("file", 10, path)])
        assert exc.value.source == "file"
        case_log.actual(repr(exc.value))

    def test_toml_supported_and_malformed_toml_rejected(self, tmp_path):
        good = tmp_path / "good.toml"
        good.write_text('[db]\nhost = "h"\npassword = "p"\n', encoding="utf-8")
        cfg = load_layered_config(ALL_FIELDS, [FileSource("file", 10, good)])
        assert cfg.get("db.host") == "h"

        bad = tmp_path / "bad.toml"
        bad.write_text("db.host = \n", encoding="utf-8")
        with pytest.raises(ConfigFormatError):
            load_layered_config(ALL_FIELDS, [FileSource("file", 10, bad)])

    def test_unsupported_file_extension(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("db: host", encoding="utf-8")
        with pytest.raises(ConfigFormatError, match="不支持的配置文件类型"):
            load_layered_config(ALL_FIELDS, [FileSource("file", 10, path)])

    def test_top_level_non_mapping_rejected(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ConfigFormatError, match="顶层必须是"):
            load_layered_config(ALL_FIELDS, [FileSource("file", 10, path)])

    @pytest.mark.parametrize(
        ("raw", "field_type"),
        [("abc", int), ("1.2.3", float), ("maybe", bool), ("[1", list[str])],
    )
    def test_env_value_parse_failures_are_format_errors(self, raw, field_type, case_log):
        field = ConfigField("count", field_type)
        case_log.input(env_raw="<redacted>", type=str(field_type))
        # 错误信息不得包含变量值
        with pytest.raises(ConfigFormatError) as exc:
            load_layered_config(
                [field],
                [EnvSource("env", 20, [field], environ={"APP_COUNT": raw})],
            )
        assert raw not in str(exc.value)
        case_log.actual(repr(exc.value))

    def test_env_json_with_wrong_element_type_is_type_error(self):
        """JSON 语法合法但元素类型不符，统一归类为类型错误（区别于格式错误）。"""
        field = ConfigField("tags", list[str])
        with pytest.raises(ConfigTypeError) as exc:
            load_layered_config(
                [field],
                [EnvSource("env", 20, [field], environ={"APP_TAGS": '[1, 2]'})],
            )
        assert exc.value.key == "tags[0]"

    @pytest.mark.parametrize(
        ("value", "field_type"),
        [("8080", int), (True, int), (1, bool), ("text", int), ({"k": 1}, list[int])],
    )
    def test_type_mismatch_on_merged_value(self, value, field_type, case_log):
        case_log.input(value_type=type(value).__name__, declared=str(field_type))
        with pytest.raises(ConfigTypeError) as exc:
            load_layered_config(
                [ConfigField("count", field_type)],
                [DictSource("s", 10, {"count": value})],
            )
        # 类型错误只报告键与类型，绝不打印值
        assert str(value) not in str(exc.value)
        assert exc.value.key == "count"
        case_log.actual(repr(exc.value))

    def test_nested_container_element_type_checked(self):
        with pytest.raises(ConfigTypeError) as exc:
            load_layered_config(
                [ConfigField("db.tags", list[str])],
                [DictSource("s", 10, {"db.tags": ["ok", 3]})],
            )
        assert exc.value.key == "db.tags[1]"

    def test_required_field_missing_does_not_fall_back_silently(self, case_log):
        case_log.input(sources=[], required=["db.host", "db.password"])
        case_log.expect("首个缺失必填字段抛 ConfigValidationError")
        with pytest.raises(ConfigValidationError, match="db.host"):
            load_layered_config(ALL_FIELDS, [])

    def test_unknown_key_from_source_is_rejected(self, case_log):
        case_log.input(unknown_key="db.hsot")
        with pytest.raises(ConfigValidationError, match="未声明"):
            load_layered_config(
                [ConfigField("db.host", str)],
                [DictSource("s", 10, {"db.host": "h", "db.hsot": "typo"})],
            )

    def test_duplicate_field_declaration_rejected(self):
        with pytest.raises(ConfigError, match="重复声明"):
            load_layered_config(
                [ConfigField("a", str), ConfigField("a", str)], []
            )

    def test_duplicate_env_binding_rejected(self):
        with pytest.raises(ConfigError, match="APP_X"):
            load_layered_config(
                [
                    ConfigField("x", str),
                    ConfigField("y", str, env_var="APP_X"),
                ],
                [],
            )

    def test_unknown_field_access_rejected(self):
        cfg = load_layered_config([ConfigField("a", str, required=False)], [])
        with pytest.raises(ConfigValidationError):
            cfg.get("b")


class TestSecretRedaction:
    def test_secret_never_in_logs_errors_repr(self, tmp_path, caplog, case_log):
        secret = "super-secret-token-9f3a"
        path = _write_json(tmp_path, {"db": {"host": "h", "password": secret}})
        with caplog.at_level(logging.DEBUG, logger="assembly_kernel"):
            cfg = load_layered_config(ALL_FIELDS, [FileSource("file", 10, path)])
            _ = cfg.as_dict()
            _ = repr(cfg)
            _ = cfg.explain()
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in joined
        case_log.actual("内核 DEBUG 日志、repr、explain 中均不含敏感值")

    def test_secret_type_error_does_not_leak_value(self):
        with pytest.raises(ConfigTypeError) as exc:
            load_layered_config(
                [ConfigField("token", str, secret=True)],
                [DictSource("s", 10, {"token": 123456})],
            )
        assert "123456" not in str(exc.value)

    def test_field_mask_helper(self):
        secret_field = ConfigField("token", str, secret=True)
        plain_field = ConfigField("host", str)
        assert secret_field.mask("abc") == "***"
        assert plain_field.mask("abc") == "'abc'"
