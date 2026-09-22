from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.services.admin_auth import create_hash, verify_password


def test_admin_requires_config_and_login(monkeypatch):
    password = "test-admin-password-123"
    hashed = create_hash(password)
    assert verify_password(password, hashed)
    assert not verify_password("wrong", hashed)
    monkeypatch.setattr(main, "settings", replace(settings, admin_password_hash=hashed))
    async def fake_probe(reserve_attempt=None):
        assert reserve_attempt is not None
        return {"configured": True, "reachable": True, "actual_model": "mock-deepseek", "latency_ms": 3, "error_code": None}
    monkeypatch.setattr(main, "probe_model", fake_probe)
    with TestClient(main.app) as client:
        assert client.get("/api/v1/admin/health").status_code == 401
        assert client.post("/api/v1/admin/model/probe").status_code == 401
        assert client.post("/api/v1/admin/login", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/v1/admin/login", json={"password": password}).status_code == 200
        assert client.get("/api/v1/admin/health").status_code == 200
        csrf = client.cookies.get("gt_csrf")
        assert client.post("/api/v1/admin/model/probe").status_code == 403
        probe = client.post("/api/v1/admin/model/probe", headers={"X-CSRF-Token": csrf})
        assert probe.status_code == 200
        assert probe.json()["data"]["actual_model"] == "mock-deepseek"
        assert client.post("/api/v1/admin/cleanup", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.post("/api/v1/admin/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.get("/api/v1/admin/health").status_code == 401
