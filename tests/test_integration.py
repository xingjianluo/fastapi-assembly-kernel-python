"""集成测试：配置 -> 插件 -> 惰性资源的一次完整装配。"""

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
    LazyResource,
    Plugin,
    PluginRegistry,
    ResourceState,
    assemble,
    load_layered_config,
)
from assembly_kernel.errors import InitializationError

log = logging.getLogger("tests.integration")


def test_full_assembly_pipeline(tmp_path):
    # 1) 分层配置：文件给主机，环境变量给端口（覆盖默认），显式覆盖敏感值
    cfg_file = tmp_path / "app.json"
    cfg_file.write_text(
        json.dumps({"db": {"host": "file-host", "port": 6000, "pool": 8}}),
        encoding="utf-8",
    )
    fields = [
        ConfigField("db.host", str),
        ConfigField("db.port", int, default=5432, required=False),
        ConfigField("db.pool", int, default=4, required=False),
        ConfigField("db.token", str, secret=True),
    ]
    cfg = load_layered_config(
        fields,
        [
            FileSource("file", 10, cfg_file),
            EnvSource("env", 20, fields, environ={"APP_DB_PORT": "7000"}),
            DictSource("override", 30, {"db.token": "tok-override"}),
        ],
    )
    assert cfg.get("db.host") == "file-host"
    assert cfg.get("db.port") == 7000
    assert cfg.get("db.pool") == 8

    # 2) 基于配置声明插件图并装配（token 只存在内存中）
    reg = PluginRegistry()
    connection = LazyResource(
        "db-connection",
        lambda: {"host": cfg.get("db.host"), "port": cfg.get("db.port")},
    )
    reg.register(Plugin("db-connection", factory=lambda d: connection.get()))
    reg.register(
        Plugin("repo", frozenset({"db-connection"}), factory=lambda d: ("repo", d["db-connection"]))
    )
    reg.register(Plugin("api", frozenset({"repo"}), factory=lambda d: ("api", d["repo"])))

    assert connection.state is ResourceState.PENDING  # 装配前未初始化
    ctx = assemble(reg)
    assert ctx.order == ["db-connection", "repo", "api"]
    assert connection.is_ready()  # 工厂首次调用时惰性初始化
    assert ctx.get("api")[1][1] == {"host": "file-host", "port": 7000}
    log.info("完整装配成功: %s", ctx.order)
    log.info("脱敏配置视图: %s", cfg.as_dict())


def test_pipeline_fails_fast_on_bad_config_before_plugins_touch_it():
    fields = [ConfigField("db.port", int)]
    from assembly_kernel.errors import ConfigTypeError

    with pytest.raises(ConfigTypeError):
        load_layered_config(
            fields, [DictSource("bad", 10, {"db.port": "not-int"})]
        )
