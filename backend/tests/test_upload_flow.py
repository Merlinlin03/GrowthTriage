from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services import orchestrator
from app.db import DailyLiveQuota, db_session
from test_golden_flow import wait_for_status


FEEDBACK = """feedback_id,created_at,source_type,audience_stage,country,text,link_method,language,rating,platform,campaign_id,creative_id
f1,2026-08-18T12:00:00Z,ad_comment,prospect,CA,Good app,direct_ad_object,en,,Meta,CMP-CA,CR-CA-9
f2,2026-08-19T12:00:00Z,ad_comment,prospect,CA,Works well,direct_ad_object,en,,Meta,CMP-CA,CR-CA-9
f3,2026-08-25T12:00:00Z,ad_comment,prospect,CA,I see this ad too often,direct_ad_object,en,,Meta,CMP-CA,CR-CA-9
f4,2026-08-26T12:00:00Z,ad_comment,prospect,CA,The ad promise differs from the app,direct_ad_object,en,,Meta,CMP-CA,CR-CA-9
f5,2026-08-27T12:00:00Z,ad_comment,prospect,CA,I see it too often,direct_ad_object,en,,Meta,CMP-CA,CR-CA-9
"""
METRICS = """metric_row_id,window,period_start,period_end,reporting_timezone,platform,country,campaign_id,creative_id,currency,conversion_source,attribution_window,spend,impressions,reach,destination_clicks,installs,revenue_d7
m1,baseline,2026-08-18,2026-08-24,UTC,Meta,CA,CMP-CA,CR-CA-9,USD,mmp,7d_click,100,10000,5000,300,60,140
m2,current,2026-08-25,2026-08-31,UTC,Meta,CA,CMP-CA,CR-CA-9,USD,mmp,7d_click,120,12000,3000,180,18,50
"""


@pytest.fixture(autouse=True)
def adapt_classifier_regressions_to_six_agents(monkeypatch):
    # Keep the original batching/failure assertions while migrating the provider boundary.
    from app.services import agent_runtime
    from test_agents import output_for

    async def fake_agent(role, evidence, *, reserve, record_tool, revision=None):
        if role == "feedback":
            def reserve_attempt():
                reserve()
                return True
            mode, model, latency, error, labels = await orchestrator.classify_feedback(evidence["feedback_rows"], reserve_attempt)
            if mode != "live":
                raise agent_runtime.AgentError(error)
            output = output_for(role, evidence)
            output["labels"] = labels
        else:
            output, model, latency = output_for(role, evidence), "mock-deepseek", 1
        return {"output": output, "model": model, "latency_ms": latency, "model_requests": 1, "tools_read": list(evidence)}

    monkeypatch.setattr(agent_runtime, "run_agent", fake_agent)


def _guest(client):
    return client.post("/api/v1/sessions/guest").json()["data"]["csrf_token"]


def _upload(client, csrf, feedback=FEEDBACK, metrics=METRICS):
    return client.post("/api/v1/datasets", headers={"X-CSRF-Token": csrf}, data={"consent": "true"}, files={"feedback": ("feedback.csv", feedback.encode()), "ad_metrics": ("ad_metrics.csv", metrics.encode())})


def test_uploaded_data_drives_findings_and_report():
    with TestClient(app) as client:
        csrf = _guest(client)
        uploaded = _upload(client, csrf)
        assert uploaded.status_code == 200, uploaded.text
        dataset_id = uploaded.json()["data"]["dataset_id"]
        created = client.post(f"/api/v1/datasets/{dataset_id}/runs", headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
        assert created.status_code == 200, created.text
        run_id = created.json()["data"]["run_id"]
        waiting = wait_for_status(client, run_id, {"waiting_approval", "failed"})
        assert waiting["status"] == "waiting_approval"
        results = client.get(f"/api/v1/runs/{run_id}/results").json()["data"]
        assert results["source"] == "upload"
        assert results["scope"]["creative_id"] == "CR-CA-9"
        assert results["feedback"]["related"] == {"baseline": 0, "current": 3}
        assert results["metrics"]["ctr"]["current"] == 0.015
        assert "CR-BR-17" not in results["finding"]["claim"]
        decision = client.post(f"/api/v1/approvals/{waiting['pending_approval_id']}/decision", headers={"X-CSRF-Token": csrf}, json={"decision": "rejected", "decision_key": str(uuid.uuid4()), "comment": "test"})
        assert decision.status_code == 200, decision.text
        assert wait_for_status(client, run_id, {"succeeded"})["status"] == "succeeded"
        report = client.get(f"/api/v1/runs/{run_id}/report").json()["data"]
        assert report["scope"]["creative_id"] == "CR-CA-9"
        assert report["ticket"] is None
        assert all(not action["executed"] for action in report["actions"] if action["risk_level"] == "R3")


def test_blocking_csv_does_not_create_dataset():
    with TestClient(app) as client:
        csrf = _guest(client)
        invalid = _upload(client, csrf, feedback=FEEDBACK.replace("Good app", "Contact me at jane@example.com"))
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "DATASET_BLOCKED"
        assert any(issue["code"] == "FEEDBACK_PRIVACY" for issue in invalid.json()["error"]["field_errors"])


def test_feedback_only_does_not_request_approval():
    stable_metrics = METRICS.replace("120,12000,3000,180,18,50", "105,10500,5250,315,63,147")
    with TestClient(app) as client:
        csrf = _guest(client)
        uploaded = _upload(client, csrf, metrics=stable_metrics)
        assert uploaded.status_code == 200, uploaded.text
        dataset_id = uploaded.json()["data"]["dataset_id"]
        created = client.post(f"/api/v1/datasets/{dataset_id}/runs", headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
        run_id = created.json()["data"]["run_id"]
        assert wait_for_status(client, run_id, {"succeeded", "failed"})["status"] == "succeeded"
        results = client.get(f"/api/v1/runs/{run_id}/results").json()["data"]
        assert results["correlation"]["kind"] == "feedback_only"
        assert results["approval"] is None
        assert not any(action["risk_level"] == "R2" for action in results["actions"])


def test_live_labels_change_decision_branch(monkeypatch):
    async def fake_classifier(rows, reserve_attempt=None):
        assert reserve_attempt is not None and reserve_attempt()
        return "live", "mock-deepseek", 1, None, {row["id"]: "other" for row in rows}

    monkeypatch.setattr(orchestrator, "settings", replace(settings, deepseek_api_key="test-only-key"))
    monkeypatch.setattr(orchestrator, "classify_feedback", fake_classifier)
    with TestClient(app) as client:
        csrf = _guest(client)
        uploaded = _upload(client, csrf)
        dataset_id = uploaded.json()["data"]["dataset_id"]
        created = client.post(f"/api/v1/datasets/{dataset_id}/runs", headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
        run_id = created.json()["data"]["run_id"]
        assert wait_for_status(client, run_id, {"succeeded", "failed"})["status"] == "succeeded"
        result = client.get(f"/api/v1/runs/{run_id}/results").json()["data"]
        assert result["execution_mode"] == "live"
        assert result["feedback"]["related"] == {"baseline": 0, "current": 0}
        assert result["correlation"]["kind"] == "metric_only"
        assert result["approval"] is None


def _many_feedback_rows() -> str:
    extra = [f"f{index},2026-08-27T12:00:00Z,ad_comment,prospect,CA,I see it too often,direct_ad_object,en,,Meta,CMP-CA,CR-CA-9" for index in range(6, 57)]
    return FEEDBACK + "\n".join(extra) + "\n"


def test_production_batches_every_feedback_row(monkeypatch):
    batch_sizes = []

    async def fake_classifier(rows, reserve_attempt=None):
        assert reserve_attempt is not None and reserve_attempt()
        batch_sizes.append(len(rows))
        return "live", "mock-deepseek", 2, None, {row["id"]: "repeat_exposure" for row in rows}

    monkeypatch.setattr(orchestrator, "settings", replace(settings, app_mode="production", deepseek_api_key="test-only-key"))
    monkeypatch.setattr(orchestrator, "classify_feedback", fake_classifier)
    with TestClient(app) as client:
        csrf = _guest(client)
        uploaded = _upload(client, csrf, feedback=_many_feedback_rows())
        assert uploaded.status_code == 200, uploaded.text
        dataset_id = uploaded.json()["data"]["dataset_id"]
        created = client.post(f"/api/v1/datasets/{dataset_id}/runs", headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
        run_id = created.json()["data"]["run_id"]
        result = wait_for_status(client, run_id, {"waiting_approval", "succeeded", "failed"})
        assert result["status"] != "failed", result
        assert result["execution_mode"] == "live"
        assert batch_sizes == [50, 6]
        with db_session() as session:
            assert sum(row.reserved_count for row in session.query(DailyLiveQuota).all()) >= 2


@pytest.mark.parametrize("error_code", ["LLM_HTTP_401", "LLM_HTTP_429", "LLM_TIMEOUT", "LLM_SCHEMA_INVALID"])
def test_production_later_batch_failure_has_no_report(monkeypatch, error_code):
    calls = 0

    async def fake_classifier(rows, reserve_attempt=None):
        nonlocal calls
        assert reserve_attempt is not None and reserve_attempt()
        calls += 1
        if calls == 2:
            return "fallback", None, 2, error_code, {}
        return "live", "mock-deepseek", 2, None, {row["id"]: "repeat_exposure" for row in rows}

    monkeypatch.setattr(orchestrator, "settings", replace(settings, app_mode="production", deepseek_api_key="test-only-key"))
    monkeypatch.setattr(orchestrator, "classify_feedback", fake_classifier)
    with TestClient(app) as client:
        csrf = _guest(client)
        uploaded = _upload(client, csrf, feedback=_many_feedback_rows())
        dataset_id = uploaded.json()["data"]["dataset_id"]
        created = client.post(f"/api/v1/datasets/{dataset_id}/runs", headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
        run_id = created.json()["data"]["run_id"]
        result = wait_for_status(client, run_id, {"failed"})
        assert result["error_code"] == error_code
        assert result["execution_mode"] == "failed"
        trace = client.get(f"/api/v1/runs/{run_id}/trace").json()["data"]
        assert any(step["step_key"] == "feedback" and step["status"] == "failed" for step in trace["steps"])
        assert client.get(f"/api/v1/runs/{run_id}/report").status_code != 200
        assert client.get(f"/api/v1/runs/{run_id}/results").json()["data"]["approval"] is None
