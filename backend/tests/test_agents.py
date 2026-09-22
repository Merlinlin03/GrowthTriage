import asyncio
import json
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import AgentArtifact, AuditEvent, DailyLiveQuota, db_session
from app.main import app
from app.services import agent_runtime as runtime, agent_graph as graph, orchestrator
from app.services.analysis import local_topic
from test_golden_flow import wait_for_status

REAL_REQUEST = runtime.request


def output_for(role, data):
    base = {"summary": "证据提示可能存在异常，仍需通过实验验证。", "evidence_refs": list(data), "uncertainties": ["缺少随机实验"]}
    if role == "feedback":
        base["labels"] = {r["id"]: local_topic(r["text"]) for r in data["feedback_rows"]}
    if role == "correlation":
        base.update(hypotheses=["素材疲劳"], alternative_explanations=["受众变化"], causality="unverified")
    if role == "planner":
        base["experiments"] = [{"title": "验证素材疲劳", "primary_metric": "CTR", "guardrails": ["CVR", "CPI"],
                                "duration_days": 7, "validation_goal": "固定人群更换素材", "stop_condition": "安装成本恶化停止"}]
    if role == "auditor":
        base.update(verdict="passed", target="none", issues=[])
    if role == "report_builder":
        base["limitations"] = ["本结果不构成因果证明。"]
    return base


class Provider:
    def __init__(self, revise=None):
        self.roles = []
        self.revise = list(revise or [])

    async def __call__(self, messages, tools, *, require_tool):
        role = next(k for k, v in runtime.AGENTS.items() if v[0] in messages[0]["content"])
        keys = [k for k in tools[0]["function"]["parameters"]["properties"]["key"]["enum"] if k != "all"]
        if require_tool:
            self.roles.append(role)
            return {"tool_calls": [{"id": f"call-{i}", "function": {"name": "read_evidence", "arguments": json.dumps({"key": k})}} for i, k in enumerate(keys)]}, "fake-model"
        observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        data = dict(zip(keys, observations))
        out = output_for(role, data)
        if role == "auditor" and self.revise:
            target = self.revise.pop(0)
            if target:
                out.update(verdict="revise", target=target, issues=["请补充替代解释"])
        return {"content": json.dumps(out, ensure_ascii=False)}, "fake-model"


@pytest.mark.parametrize("role", list(runtime.AGENTS))
def test_each_agent_observes_tool_before_output(monkeypatch, role):
    provider = Provider()
    monkeypatch.setattr(runtime, "request", provider)
    data = {"feedback_rows": [{"id": "a", "text": "too often"}]} if role == "feedback" else {"facts": {"metric": 2}}
    requests, calls = [], []
    result = asyncio.run(runtime.run_agent(role, data, reserve=lambda: requests.append(1),
                         record_tool=lambda *args: calls.append(args)))
    assert result["model_requests"] == 2 and len(requests) == 2
    assert result["tools_read"] == list(data)
    assert calls == [(role, next(iter(data)), True)]


@pytest.mark.parametrize("function,args", [("delete_data", {"key": "facts"}), ("read_evidence", {"key": "other_workspace"}), ("read_evidence", {"key": "facts", "run_id": "stranger"})])
def test_harness_rejects_untrusted_tool_calls(monkeypatch, function, args):
    async def bad(*a, **kw):
        return {"tool_calls": [{"id": "x", "function": {"name": function, "arguments": json.dumps(args)}}]}, "fake"
    monkeypatch.setattr(runtime, "request", bad)
    calls = []
    with pytest.raises(runtime.AgentError, match="AGENT_TOOL_DENIED"):
        asyncio.run(runtime.run_agent("performance", {"facts": {}}, reserve=lambda: None, record_tool=lambda *a: calls.append(a)))
    assert calls == [("performance", "denied", False)]


def test_invalid_evidence_and_numbers_are_rejected():
    out = output_for("performance", {"facts": {"cpi": 2}})
    out["evidence_refs"] = ["invented"]
    with pytest.raises(runtime.AgentError, match="EVIDENCE"):
        runtime.validate_output("performance", out, {"facts": {}}, {"facts"})
    out["evidence_refs"] = ["facts"]
    out["summary"] = "CPI 上升至 999"
    with pytest.raises(runtime.AgentError, match="NUMERIC"):
        runtime.validate_output("performance", out, {"facts": {"cpi": 2}}, {"facts"})


def test_tool_loop_has_hard_limit(monkeypatch):
    async def endless(*a, **kw):
        return {"tool_calls": [{"id": "x", "function": {"name": "read_evidence", "arguments": '{"key":"facts"}'}}]}, "fake"
    monkeypatch.setattr(runtime, "request", endless)
    requests = []
    with pytest.raises(runtime.AgentError, match="AGENT_TURN_LIMIT"):
        asyncio.run(runtime.run_agent("performance", {"facts": {}}, reserve=lambda: requests.append(1), record_tool=lambda *a: None))
    assert len(requests) == 4


def start_uploaded(client):
    from test_upload_flow import _guest, _upload
    csrf = _guest(client)
    dataset = _upload(client, csrf).json()["data"]["dataset_id"]
    run_id = client.post(f"/api/v1/datasets/{dataset}/runs", headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())}).json()["data"]["run_id"]
    return csrf, run_id


def setup_live(monkeypatch, provider):
    monkeypatch.setattr(orchestrator, "settings", replace(settings, deepseek_api_key="fake-only", live_daily_limit=10000))
    monkeypatch.setattr(runtime, "request", provider)
    with db_session() as session:
        for quota in session.scalars(select(DailyLiveQuota)):
            quota.limit_snapshot = 10000
        session.commit()


def test_six_agents_graph_rework_approval_final_review(monkeypatch):
    provider = Provider(["correlation", None, "report_builder", None])
    setup_live(monkeypatch, provider)
    with TestClient(app) as client:
        csrf, run_id = start_uploaded(client)
        waiting = wait_for_status(client, run_id, {"waiting_approval", "failed"})
        assert waiting["status"] == "waiting_approval", waiting
        assert provider.roles.count("correlation") == 2 and provider.roles.count("feedback") == 1
        assert client.get(f"/api/v1/runs/{run_id}/report").status_code != 200
        decision = {"decision": "approved", "decision_key": str(uuid.uuid4()), "comment": "test"}
        route = f"/api/v1/approvals/{waiting['pending_approval_id']}/decision"
        assert client.post(route, headers={"X-CSRF-Token": csrf}, json=decision).status_code == 200
        assert wait_for_status(client, run_id, {"succeeded", "failed"})["status"] == "succeeded"
        assert client.post(route, headers={"X-CSRF-Token": csrf}, json=decision).status_code == 200
        report = client.get(f"/api/v1/runs/{run_id}/report").json()["data"]
        assert report["agent_review"]["verdict"] == "passed"
        assert report["versions"]["dag"] == "langgraph-six-agents-v1"
        assert report["external_writes"] == 0 and report["ticket"]
        assert report["experiments"][0]["title"] == "验证素材疲劳"
        assert provider.roles.count("report_builder") == 2
        trace = client.get(f"/api/v1/runs/{run_id}/trace").json()["data"]
        assert len([s for s in trace["steps"] if s["execution_mode"] == "live"]) == 6
        with db_session() as session:
            versions = session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run_id, AgentArtifact.step_key == "correlation")).all()
            assert len(versions) == 2 and sum(a.is_current for a in versions) == 1


def test_repeated_audit_failure_never_creates_approval_or_report(monkeypatch):
    setup_live(monkeypatch, Provider(["planner"] * 3))
    with TestClient(app) as client:
        _, run_id = start_uploaded(client)
        failed = wait_for_status(client, run_id, {"failed"})
        assert failed["error_code"] == "AGENT_REVIEW_LIMIT"
        assert client.get(f"/api/v1/runs/{run_id}/report").status_code != 200
        assert client.get(f"/api/v1/runs/{run_id}/results").json()["data"]["approval"] is None


def test_final_audit_failure_blocks_report(monkeypatch):
    setup_live(monkeypatch, Provider([None, "report_builder", "report_builder"]))
    with TestClient(app) as client:
        csrf, run_id = start_uploaded(client)
        waiting = wait_for_status(client, run_id, {"waiting_approval"})
        client.post(f"/api/v1/approvals/{waiting['pending_approval_id']}/decision", headers={"X-CSRF-Token": csrf},
                    json={"decision": "rejected", "decision_key": str(uuid.uuid4()), "comment": "test"})
        assert wait_for_status(client, run_id, {"failed"})["error_code"] == "AGENT_FINAL_REVIEW_LIMIT"
        assert client.get(f"/api/v1/runs/{run_id}/report").status_code != 200


@pytest.mark.parametrize("code", ["LLM_TIMEOUT", "LLM_HTTP_429", "LLM_SCHEMA_INVALID"])
def test_later_agent_failure_blocks_approval(monkeypatch, code):
    provider = Provider()

    async def failing(messages, tools, **kwargs):
        if runtime.AGENTS["performance"][0] in messages[0]["content"]:
            raise runtime.AgentError(code)
        return await provider(messages, tools, **kwargs)

    setup_live(monkeypatch, failing)
    with TestClient(app) as client:
        _, run_id = start_uploaded(client)
        assert wait_for_status(client, run_id, {"failed"})["error_code"] == code
        trace = client.get(f"/api/v1/runs/{run_id}/trace").json()["data"]
        assert next(s for s in trace["steps"] if s["step_key"] == "performance")["status"] == "failed"
        assert client.get(f"/api/v1/runs/{run_id}/report").status_code != 200


def test_run_budget_blocks_before_provider_request():
    from test_recovery import make_run
    from app.db import AnalysisRun
    run_id, _ = make_run()
    with db_session() as session:
        run = session.get(AnalysisRun, run_id)
        run.status = "running"
        for _ in range(80):
            orchestrator._audit(session, run, "agent.request_reserved", {})
        session.commit()
    with pytest.raises(runtime.AgentError, match="AGENT_RUN_BUDGET"):
        graph.reserve(run_id, "feedback")


def test_interrupted_report_agent_is_not_replayed(monkeypatch):
    from app.db import AnalysisRun
    from app.services.recovery import recover_runs
    setup_live(monkeypatch, Provider())
    with TestClient(app) as client:
        _, run_id = start_uploaded(client)
        waiting = wait_for_status(client, run_id, {"waiting_approval"})
        with db_session() as session:
            run = session.get(AnalysisRun, run_id)
            workspace_id = run.workspace_id
        orchestrator.decide_approval(waiting["pending_approval_id"], workspace_id, "rejected", str(uuid.uuid4()), "test")
        with db_session() as session:
            run = session.get(AnalysisRun, run_id)
            run.status = "running"
            session.commit()
        assert all(item[0] != run_id for item in recover_runs())
        with db_session() as session:
            assert session.get(AnalysisRun, run_id).error_code == "PROCESS_INTERRUPTED"


def test_deepseek_request_uses_supported_tool_and_json_protocol(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"model": "test-model", "choices": [{"message": {"content": "{}"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    monkeypatch.setattr(runtime.httpx, "AsyncClient", lambda **kwargs: Client())
    result = asyncio.run(REAL_REQUEST([], [], require_tool=True))
    assert result[1] == "test-model"
    assert captured["tool_choice"] == {"type": "function", "function": {"name": "read_evidence"}}
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["thinking"] == {"type": "disabled"}


def test_all_tool_is_scoped_and_malformed_json_repair_is_bounded(monkeypatch):
    calls, requests = [], []

    async def provider(messages, tools, *, require_tool):
        if require_tool:
            return {"tool_calls": [{"id": "x", "function": {"name": "read_evidence", "arguments": '{"key":"all"}'}}]}, "fake"
        observation = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
        assert observation == {"facts": {"metric": 2}}
        return {"content": "not json"}, "fake"

    monkeypatch.setattr(runtime, "request", provider)
    with pytest.raises(runtime.AgentError, match="LLM_SCHEMA_INVALID"):
        asyncio.run(runtime.run_agent("performance", {"facts": {"metric": 2}}, reserve=lambda: requests.append(1), record_tool=lambda *a: calls.append(a)))
    assert calls == [("performance", "all", True)] and len(requests) == 3
