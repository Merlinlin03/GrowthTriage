"""LangGraph routes; SQLite artifacts/approvals remain the durable commit boundaries."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from ..db import (AgentArtifact, AgentStep, AnalysisRun, ApprovalRequest, AuditEvent, DailyLiveQuota, Dataset, FeedbackRecord,
                  PolicyDecision, ReportBundle, ToolCall, WRITE_LOCK, db_session, now_ms)
from . import agent_runtime as runtime
from . import orchestrator as ops
from .analysis import correlate, feedback_cluster, snapshot
from .deepseek import PII
from .golden import new_id


class State(TypedDict, total=False):
    run_id: str
    feedback: dict
    performance: dict
    correlation: dict
    planner: dict
    auditor: dict
    report_builder: dict
    revision: dict
    reworks: int


def artifact(run_id: str, key: str) -> dict:
    with db_session() as session:
        return ops._artifact(session, run_id, key) or {}


def reserve(run_id: str, role: str) -> None:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        if not run or run.status != "running":
            raise runtime.AgentError("RUN_NOT_RUNNING")
        if run.expires_at_ms and run.expires_at_ms <= now_ms():
            raise runtime.AgentError("RUN_EXPIRED")
        count = session.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.run_id == run_id, AuditEvent.event_type == "agent.request_reserved"))
        if count >= 80:
            raise runtime.AgentError("AGENT_RUN_BUDGET")
        today = datetime.now(timezone.utc).date().isoformat()
        quota = session.get(DailyLiveQuota, today)
        if not quota:
            quota = DailyLiveQuota(day_utc=today, reserved_count=0, limit_snapshot=ops.settings.live_daily_limit,
                                   purge_after_ms=now_ms() + 72 * 3600 * 1000)
            session.add(quota)
        if quota.reserved_count >= quota.limit_snapshot:
            raise runtime.AgentError("LLM_DAILY_LIMIT")
        quota.reserved_count += 1
        if not run.live_reserved_at_ms:
            run.live_reserved_at_ms = now_ms()
        ops._audit(session, run, "agent.request_reserved", {"agent": role, "request_index": count + 1})
        session.commit()


def record_tool(run_id: str, role: str, key: str, allowed: bool) -> None:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        if not run or run.status != "running":
            raise runtime.AgentError("RUN_NOT_RUNNING")
        tool = ToolCall(id=new_id("tool"), workspace_id=run.workspace_id, run_id=run_id,
                        tool_name=f"{role}.read_evidence", risk_level="R1",
                        status="succeeded" if allowed else "denied", executed=allowed,
                        args_sha256=hashlib.sha256(key.encode()).hexdigest())
        session.add(tool)
        session.flush()
        session.add(PolicyDecision(id=new_id("policy"), workspace_id=run.workspace_id, run_id=run_id,
                    tool_call_id=tool.id, decision="allow" if allowed else "deny",
                    matched_rule="AGENT_SCOPED_READ", reason="本任务证据白名单校验"))
        ops._audit(session, run, "agent.tool", {"agent": role, "key": key, "allowed": allowed})
        session.commit()


def start(run_id: str, role: str) -> None:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        step = ops._step(session, run_id, role)
        if step.status == "succeeded":
            run.completed_steps -= 1
        step.status = "pending"
        session.commit()
    ops._begin_step(run_id, role)


def save(run_id: str, role: str, payload: dict, result: dict) -> None:
    with WRITE_LOCK, db_session() as session:
        for old in session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run_id,
                                  AgentArtifact.step_key == role, AgentArtifact.is_current.is_(True))):
            old.is_current = False
        session.commit()
    payload = {**payload, "agent": {"name": runtime.AGENTS[role][0], "framework": "langgraph",
               "analysis": result["output"], "tools_read": result["tools_read"],
               "model_requests": result["model_requests"]}}
    ops._complete_step(run_id, role, payload, execution_mode="live", actual_model=result["model"],
                       latency_ms=result["latency_ms"])


async def invoke(run_id: str, role: str, evidence: dict, revision: dict | None = None) -> dict:
    started = now_ms()
    try:
        return await runtime.run_agent(role, evidence, reserve=lambda: reserve(run_id, role),
                                       record_tool=lambda r, k, a: record_tool(run_id, r, k, a), revision=revision)
    except runtime.AgentError as exc:
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            if run:
                ops._audit(session, run, "agent.failed", {"agent": role, "phase": run.current_phase,
                           "error_code": exc.code, "latency_ms": now_ms() - started})
                session.commit()
        raise


def fail(run_id: str, code: str) -> None:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        if not run or run.status in {"succeeded", "expired"}:
            return
        run.error_code = code
        run.execution_mode = "failed"
        for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.status == "running")):
            step.status = "failed"
            step.execution_mode = "failed"
            step.error_code = code
            step.completed_at_ms = now_ms()
            step.latency_ms = now_ms() - (step.started_at_ms or now_ms())
        ops._touch(run, status="failed")
        ops._audit(session, run, "run.failed", {"error_code": code})
        session.commit()


async def feedback(state: State) -> dict:
    run_id = state["run_id"]
    start(run_id, "feedback")
    with db_session() as session:
        run = session.get(AnalysisRun, run_id)
        rows = session.scalars(select(FeedbackRecord).where(FeedbackRecord.dataset_id == run.dataset_id)).all()
    labels, batches = {}, []
    for offset in range(0, len(rows), 50):
        data = [{"id": row.id, "text": PII.sub("[REDACTED]", row.text)[:500]} for row in rows[offset:offset + 50]]
        result = await invoke(run_id, "feedback", {"feedback_rows": data}, state.get("revision"))
        if batches and result["model"] != batches[0]["model"]:
            raise runtime.AgentError("LLM_MODEL_INCONSISTENT")
        labels.update(result["output"]["labels"])
        batches.append(result)
    if not batches:
        raise runtime.AgentError("LLM_NO_INPUT")
    with db_session() as session:
        payload = feedback_cluster(session, run.dataset_id, artifact(run_id, "snapshot")["target"], labels)
    result = dict(batches[-1])
    result["latency_ms"] = sum(b["latency_ms"] for b in batches)
    result["model_requests"] = sum(b["model_requests"] for b in batches)
    payload["batch_analyses"] = [b["output"] for b in batches]
    save(run_id, "feedback", payload, result)
    return {"feedback": payload}


async def performance(state: State) -> dict:
    run_id = state["run_id"]
    start(run_id, "performance")
    facts = artifact(run_id, "snapshot")
    result = await invoke(run_id, "performance", {"facts": facts}, state.get("revision"))
    target = facts["target"]
    payload = {"anomaly": target["anomaly"], "creative_id": target["creative_id"],
               "metrics": target["metrics"], "stable_controls": facts["stable_controls"]}
    save(run_id, "performance", payload, result)
    return {"performance": payload}


def evidence(run_id: str, keys: tuple[str, ...]) -> dict:
    return {key: artifact(run_id, key) for key in keys}


async def correlation(state: State) -> dict:
    run_id = state["run_id"]
    start(run_id, "correlation")
    data = evidence(run_id, ("snapshot", "feedback", "performance"))
    gate = correlate(data["snapshot"], data["feedback"])
    result = await invoke(run_id, "correlation", {**data, "association_gate": gate}, state.get("revision"))
    payload = {**gate, "hypotheses": result["output"]["hypotheses"],
               "alternative_explanations": result["output"]["alternative_explanations"]}
    save(run_id, "correlation", payload, result)
    return {"correlation": payload}


async def planner(state: State) -> dict:
    run_id = state["run_id"]
    start(run_id, "planner")
    data = evidence(run_id, ("snapshot", "feedback", "performance", "correlation"))
    result = await invoke(run_id, "planner", {**data, "policy": {"R1": "allow", "R2": "approval", "R3": "suggest_only"}}, state.get("revision"))
    payload = {"experiments": result["output"]["experiments"], "hypotheses": data["correlation"]["hypotheses"]}
    save(run_id, "planner", payload, result)
    return {"planner": payload}


async def auditor(state: State) -> dict:
    run_id = state["run_id"]
    start(run_id, "auditor")
    data = evidence(run_id, ("snapshot", "feedback", "performance", "correlation", "planner"))
    result = await invoke(run_id, "auditor", data)
    output = result["output"]
    save(run_id, "auditor", {"passed": output["verdict"] == "passed", "causality": "unverified"}, result)
    return {"auditor": output, "revision": output,
            "reworks": state.get("reworks", 0) + int(output["verdict"] == "revise")}


def route_review(state: State) -> str:
    if state["auditor"]["verdict"] == "passed":
        return END
    if state["reworks"] > 2:
        raise runtime.AgentError("AGENT_REVIEW_LIMIT")
    target = state["auditor"]["target"]
    if target not in {"feedback", "performance", "correlation", "planner"}:
        raise runtime.AgentError("AGENT_ROUTE_INVALID")
    return target


builder = StateGraph(State)
for key, node in (("feedback", feedback), ("performance", performance), ("correlation", correlation),
                  ("planner", planner), ("auditor", auditor)):
    builder.add_node(key, node)
builder.add_edge(START, "feedback")
for left, right in zip(("feedback", "performance", "correlation", "planner"), ("performance", "correlation", "planner", "auditor")):
    builder.add_edge(left, right)
builder.add_conditional_edges("auditor", route_review, [END, "feedback", "performance", "correlation", "planner"])
analysis_graph = builder.compile()


async def execute(run_id: str) -> None:
    try:
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            if not run or run.status not in {"queued", "running"}:
                return
            dataset = session.get(Dataset, run.dataset_id)
            ops._touch(run, status="running", phase="validator")
            ops._audit(session, run, "graph.started", {"framework": "langgraph", "agents": 6})
            session.commit()
        if ops._begin_step(run_id, "validator"):
            ops._complete_step(run_id, "validator", {"feedback_rows": dataset.feedback_rows, "metric_rows": dataset.metric_rows, "blocking_issues": 0, "graph": "six-agents-v1"})
        if ops._begin_step(run_id, "snapshot"):
            with db_session() as session:
                facts = snapshot(session, dataset.id)
            ops._complete_step(run_id, "snapshot", facts)
        async with asyncio.timeout(600):
            await analysis_graph.ainvoke({"run_id": run_id, "reworks": 0}, {"recursion_limit": 32})
        gate = artifact(run_id, "correlation")
        if ops._begin_step(run_id, "policy_gate"):
            ops._complete_step(run_id, "policy_gate", {"R1": "allow", "R2": "require_approval" if gate["kind"] == "cross_domain" and gate["link_strength"] in {"A", "B"} else "not_requested", "R3": "suggest_only"})
        if not ops._create_decision_entities(run_id):
            with WRITE_LOCK, db_session() as session:
                ops._touch(session.get(AnalysisRun, run_id), status="queued", phase="report_builder")
                session.commit()
            await finish(run_id)
    except runtime.AgentError as exc:
        fail(run_id, exc.code)
    except TimeoutError:
        fail(run_id, "AGENT_PHASE_TIMEOUT")
    except Exception:
        fail(run_id, "RUN_CONSISTENCY_ERROR")


async def report_node(state: State) -> dict:
    run_id = state["run_id"]
    start(run_id, "report_builder")
    data = evidence(run_id, ("snapshot", "feedback", "performance", "correlation", "planner", "auditor"))
    with db_session() as session:
        payload = ops._report_payload(session, session.get(AnalysisRun, run_id))
    result = await invoke(run_id, "report_builder", {**data, "execution": payload}, state.get("revision"))
    # Drafts are not downloadable until the final auditor passes.
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        session.add(AgentArtifact(id=new_id("artifact"), workspace_id=run.workspace_id, run_id=run_id,
                    step_key="report_draft", artifact_type="report_draft.v2", is_current=False, payload=result))
        session.commit()
    return {"report_builder": result}


async def final_review(state: State) -> dict:
    run_id = state["run_id"]
    with WRITE_LOCK, db_session() as session:
        ops._touch(session.get(AnalysisRun, run_id), phase="final_review")
        session.commit()
    data = evidence(run_id, ("snapshot", "feedback", "performance", "correlation", "planner"))
    with db_session() as session:
        execution = ops._report_payload(session, session.get(AnalysisRun, run_id))
    result = await invoke(run_id, "auditor", {**data, "execution": execution, "report": state["report_builder"]["output"]})
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        session.add(AgentArtifact(id=new_id("artifact"), workspace_id=run.workspace_id, run_id=run_id,
                    step_key="final_review", artifact_type="final_review.v2", is_current=False, payload=result))
        ops._audit(session, run, "agent.final_review", {"verdict": result["output"]["verdict"]})
        session.commit()
    return {"auditor": result["output"], "revision": result["output"],
            "reworks": state.get("reworks", 0) + int(result["output"]["verdict"] == "revise")}


def route_final(state: State) -> str:
    if state["auditor"]["verdict"] == "passed":
        return END
    if state["reworks"] > 1:
        raise runtime.AgentError("AGENT_FINAL_REVIEW_LIMIT")
    return "report_builder"


report_builder = StateGraph(State)
report_builder.add_node("report_builder", report_node)
report_builder.add_node("final_review", final_review)
report_builder.add_conditional_edges(START, lambda state: "final_review" if state.get("report_builder") else "report_builder", ["final_review", "report_builder"])
report_builder.add_edge("report_builder", "final_review")
report_builder.add_conditional_edges("final_review", route_final, [END, "report_builder"])
report_graph = report_builder.compile()


async def finish(run_id: str) -> None:
    try:
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            if not run or run.status != "queued":
                return
            if run.expires_at_ms and run.expires_at_ms <= now_ms():
                ops._touch(run, status="expired", phase="expired")
                session.commit()
                return
            ops._touch(run, status="running", phase="report_builder")
            session.commit()
            drafts = session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run_id,
                AgentArtifact.step_key == "report_draft").order_by(AgentArtifact.created_at_ms.desc())).all()
            reviews = session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run_id,
                AgentArtifact.step_key == "final_review").order_by(AgentArtifact.created_at_ms.desc())).all()
            initial = {"run_id": run_id, "reworks": sum(r.payload["output"]["verdict"] == "revise" for r in reviews)}
            if drafts and (not reviews or drafts[0].created_at_ms > reviews[0].created_at_ms):
                initial["report_builder"] = drafts[0].payload
        if initial.get("report_builder"):
            start(run_id, "report_builder")
        async with asyncio.timeout(600):
            result = await report_graph.ainvoke(initial, {"recursion_limit": 8})
        agent = result["report_builder"]
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            payload = ops._report_payload(session, run)
            if run.status != "running" or (run.expires_at_ms and run.expires_at_ms <= now_ms()):
                raise runtime.AgentError("RUN_EXPIRED")
            payload["execution_mode"] = "live"
            payload["summary"] = agent["output"]["summary"]
            payload["limitations"] += agent["output"]["limitations"]
            payload["agent_review"] = result["auditor"]
            payload["versions"]["dag"] = "langgraph-six-agents-v1"
            markdown = ops._report_markdown(payload)
            if not ops._redaction_passed(payload, markdown):
                raise runtime.AgentError("REPORT_REDACTION_FAILED")
            session.add(ReportBundle(id=new_id("report"), workspace_id=run.workspace_id, run_id=run_id,
                        status="ready", payload=payload, markdown=markdown, redaction_passed=True))
            session.commit()
        save(run_id, "report_builder", {"report": "ready", "redaction_passed": True}, agent)
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            ops._touch(run, status="succeeded", phase="complete")
            ops._audit(session, run, "run.succeeded", {"external_writes": 0, "agents": 6})
            session.commit()
    except runtime.AgentError as exc:
        fail(run_id, exc.code)
    except TimeoutError:
        fail(run_id, "AGENT_PHASE_TIMEOUT")
    except Exception:
        fail(run_id, "RUN_CONSISTENCY_ERROR")


REPORT_RETRY_ERRORS = {"LLM_PROVIDER_ERROR", "LLM_TRANSPORT_ERROR", "LLM_RESPONSE_INVALID", "LLM_TIMEOUT", "LLM_HTTP_429", "LLM_HTTP_500", "LLM_HTTP_502", "LLM_HTTP_503", "LLM_HTTP_504"}


def queue_report_retry(run_id: str, workspace_id: str) -> bool:
    with WRITE_LOCK, db_session() as session:
        run = session.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id, AnalysisRun.workspace_id == workspace_id))
        if not run:
            raise runtime.AgentError("RESOURCE_NOT_FOUND")
        if run.status in {"queued", "running", "succeeded"}:
            return False
        if run.expires_at_ms and run.expires_at_ms <= now_ms():
            raise runtime.AgentError("RUN_EXPIRED")
        validator = ops._artifact(session, run_id, "validator") or {}
        audit = ops._artifact(session, run_id, "auditor") or {}
        approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id))
        if (run.status != "failed" or run.current_phase not in {"report_builder", "final_review"}
                or run.error_code not in REPORT_RETRY_ERRORS or validator.get("graph") != "six-agents-v1"
                or not audit.get("passed") or (approval and approval.status not in {"approved", "rejected"})):
            raise runtime.AgentError("REPORT_NOT_RETRYABLE")
        if session.scalar(select(AnalysisRun.id).where(AnalysisRun.workspace_id == workspace_id,
                AnalysisRun.id != run_id, AnalysisRun.status.in_(["queued", "running", "waiting_approval"]))):
            raise runtime.AgentError("RUN_ALREADY_ACTIVE")
        ops._audit(session, run, "report.retry_requested", {"previous_error": run.error_code, "previous_phase": run.current_phase})
        run.error_code = None
        run.execution_mode = "live"
        step = ops._step(session, run_id, "report_builder")
        step.error_code = None
        step.status = "pending"
        ops._touch(run, status="queued", phase="report_builder")
        session.commit()
        return True
