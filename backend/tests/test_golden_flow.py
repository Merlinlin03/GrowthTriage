from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.orchestrator import decide_approval


def wait_for_status(client: TestClient, run_id: str, wanted: set[str], timeout: float = 12.0):
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}/status")
        assert response.status_code == 200, response.text
        latest = response.json()["data"]
        if latest["status"] in wanted:
            return latest
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {wanted}, latest={latest}")


def test_golden_approve_and_report():
    with TestClient(app) as client:
        guest = client.post("/api/v1/sessions/guest")
        assert guest.status_code == 200
        csrf = guest.json()["data"]["csrf_token"]
        created = client.post(
            "/api/v1/runs/golden",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["data"]["run_id"]
        waiting = wait_for_status(client, run_id, {"waiting_approval"})
        assert waiting["completed_steps"] == 8
        approval_id = waiting["pending_approval_id"]

        results = client.get(f"/api/v1/runs/{run_id}/results").json()["data"]
        assert results["finding"]["causality_status"] == "unverified"
        assert results["fixture"]["feedback_rows"] == 86
        for metric_name, expected in results["fixture"]["target"]["metrics"].items():
            for field_name, value in expected.items():
                assert results["metrics"][metric_name][field_name] == pytest.approx(value)
        assert results["feedback"]["related"] == {"baseline": 4, "current": 18}
        assert results["feedback"]["repeat_exposure"] == {"baseline": 2, "current": 11}
        assert results["feedback"]["promise_mismatch"] == {"baseline": 2, "current": 7}
        assert any(a["risk_level"] == "R3" and a["executed"] is False for a in results["actions"])
        evidence = client.get(f"/api/v1/findings/{results['finding']['id']}/evidence")
        assert evidence.status_code == 200
        assert len(evidence.json()["data"]["items"]) >= 6
        trace = client.get(f"/api/v1/runs/{run_id}/trace").json()["data"]
        assert trace["artifacts"]
        assert client.get(f"/api/v1/artifacts/{trace['artifacts'][0]['id']}").status_code == 200
        assert any(item["approval_id"] == approval_id for item in client.get("/api/v1/approvals").json()["data"])

        decision_key = str(uuid.uuid4())
        decision_body = {"decision": "approved", "decision_key": decision_key, "comment": "test approval"}
        decision = client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            headers={"X-CSRF-Token": csrf},
            json=decision_body,
        )
        assert decision.status_code == 200, decision.text
        replay = client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            headers={"X-CSRF-Token": csrf},
            json=decision_body,
        )
        assert replay.status_code == 200

        complete = wait_for_status(client, run_id, {"succeeded"})
        assert complete["completed_steps"] == 9
        final = client.get(f"/api/v1/runs/{run_id}/report").json()["data"]
        assert final["external_writes"] == 0
        assert final["ticket"] is not None
        assert client.get(f"/api/v1/runs/{run_id}/ticket").json()["data"]["ticket_no"] == final["ticket"]["ticket_no"]
        assert all(not (a["risk_level"] == "R3" and a["executed"]) for a in final["actions"])


def test_workspace_isolation():
    with TestClient(app) as owner:
        guest = owner.post("/api/v1/sessions/guest").json()["data"]
        created = owner.post(
            "/api/v1/runs/golden",
            headers={"X-CSRF-Token": guest["csrf_token"], "Idempotency-Key": str(uuid.uuid4())},
        ).json()["data"]
        with TestClient(app) as stranger:
            stranger.post("/api/v1/sessions/guest")
            response = stranger.get(f"/api/v1/runs/{created['run_id']}/status")
            assert response.status_code == 404


def test_queued_after_approval_recovers_on_restart():
    with TestClient(app) as client:
        guest = client.post("/api/v1/sessions/guest").json()["data"]
        run_id = client.post("/api/v1/runs/golden", headers={"X-CSRF-Token": guest["csrf_token"], "Idempotency-Key": str(uuid.uuid4())}).json()["data"]["run_id"]
        approval_id = wait_for_status(client, run_id, {"waiting_approval"})["pending_approval_id"]
        decide_approval(approval_id, guest["workspace_id"], "rejected", str(uuid.uuid4()), "restart test")
        assert client.get(f"/api/v1/runs/{run_id}/status").json()["data"]["status"] == "queued"
        cookies = dict(client.cookies)
    with TestClient(app) as recovered:
        recovered.cookies.update(cookies)
        assert wait_for_status(recovered, run_id, {"succeeded"})["status"] == "succeeded"
        assert recovered.get(f"/api/v1/runs/{run_id}/report").json()["data"]["ticket"] is None
