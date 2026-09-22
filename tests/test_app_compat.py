"""兼容性测试：现有最小接口行为必须保持不变。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_root_endpoint_contract_unchanged():
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}


def test_app_is_fastapi_instance():
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
