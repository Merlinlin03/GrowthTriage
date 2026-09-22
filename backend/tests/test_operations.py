import asyncio
import importlib.util
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Base
from app.main import app
from app.services.request_limits import RequestLimits


spec = importlib.util.spec_from_file_location("db_backup", Path(__file__).resolve().parents[2] / "scripts/db_backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_wal_backup_restore_and_existing_target(tmp_path):
    from sqlalchemy import create_engine
    source, snapshot, restored = (tmp_path / name for name in ["source.db", "snapshot.db", "restored.db"])
    engine = create_engine(f"sqlite:///{source.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO daily_live_quota VALUES ('2026-09-17', 7, 30, 99)")
        conn.commit()
        backup.copy_database(source, snapshot)
    backup.copy_database(snapshot, restored)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT reserved_count FROM daily_live_quota").fetchone() == (7,)
    with pytest.raises(FileExistsError):
        backup.copy_database(source, restored)


def test_invalid_backup_does_not_leave_destination(tmp_path):
    source, target = tmp_path / "bad.db", tmp_path / "new.db"
    source.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        backup.copy_database(source, target)
    assert not target.exists()
    assert source.read_bytes() == b"not a database"


def test_request_size_and_security_headers():
    with TestClient(app) as client:
        response = client.post("/api/v1/sessions/guest", headers={"Content-Length": "3100001"})
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"


def test_cross_origin_and_missing_csrf_cannot_write():
    with TestClient(app) as client:
        response = client.post("/api/v1/sessions/guest", headers={"Origin": "https://untrusted.example"})
        assert response.status_code == 403
        client.post("/api/v1/sessions/guest")
        assert client.post("/api/v1/runs/golden").status_code == 403


def test_backup_restores_completed_business_result(tmp_path):
    import json
    from app.db import db_path
    from test_recovery import make_run
    from app.services.orchestrator import execute_run, decide_approval, resume_after_approval
    from app.db import ApprovalRequest, db_session
    from sqlalchemy import select
    import uuid

    run_id, workspace_id = make_run()
    asyncio.run(execute_run(run_id))
    with db_session() as session:
        approval_id = session.scalar(select(ApprovalRequest.id).where(ApprovalRequest.run_id == run_id))
    decide_approval(approval_id, workspace_id, "approved", str(uuid.uuid4()), "backup test")
    asyncio.run(resume_after_approval(run_id))
    snapshot, restored = tmp_path / "snapshot.db", tmp_path / "restored.db"
    backup.copy_database(Path(db_path), snapshot)
    backup.copy_database(snapshot, restored)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT status FROM analysis_run WHERE id=?", (run_id,)).fetchone() == ("succeeded",)
        assert conn.execute("SELECT count(*) FROM sandbox_ticket WHERE run_id=?", (run_id,)).fetchone() == (1,)
        payload = json.loads(conn.execute("SELECT payload FROM report_bundle WHERE run_id=?", (run_id,)).fetchone()[0])
        assert payload["external_writes"] == 0
        assert payload["ticket"] is not None


def test_chunked_limit_and_rate_window(monkeypatch):
    import app.services.request_limits as limits
    clock = [100.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock[0])
    called = []

    async def downstream(scope, receive, send):
        called.append((await receive())["body"])

    middleware = RequestLimits(downstream, max_bytes=5, writes_per_minute=2)

    async def request(chunks):
        messages = iter([{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                         for index, chunk in enumerate(chunks)])
        output = []

        async def receive():
            return next(messages)

        async def send(message):
            output.append(message)

        await middleware({"type": "http", "method": "POST", "path": "/api/test", "headers": []}, receive, send)
        return output

    assert asyncio.run(request([b"123", b"456"]))[0]["status"] == 413
    assert not called
    asyncio.run(request([b"12", b"3"]))
    assert called == [b"123"]
    assert asyncio.run(request([b"a"]))[0]["status"] == 429
    clock[0] += 61
    asyncio.run(request([b"a"]))
    assert called == [b"123", b"a"]
