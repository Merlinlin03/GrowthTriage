from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import (
    ActionFinding,
    ActionPlan,
    AgentArtifact,
    AgentStep,
    AnalysisRun,
    ApprovalRequest,
    AuditEvent,
    DailyLiveQuota,
    Dataset,
    EvidenceRef,
    ExperimentCard,
    FeedbackRecord,
    Finding,
    PolicyDecision,
    ReportBundle,
    SandboxTicket,
    ToolCall,
    WRITE_LOCK,
    db_session,
    now_ms,
)
from .analysis import correlate, feedback_cluster, snapshot
from .deepseek import ALLOWED_TOPICS, BATCH_SIZE, classify_feedback
from .golden import new_id
from ..config import settings


STEP_DEFS = [
    ("validator", "数据校验", "local"),
    ("snapshot", "事实快照", "local"),
    ("feedback", "用户反馈分析 Agent", "llm"),
    ("performance", "广告指标诊断 Agent", "llm"),
    ("correlation", "联合证据分析 Agent", "llm"),
    ("planner", "优化方案规划 Agent", "llm"),
    ("auditor", "诊断审核 Agent", "llm"),
    ("policy_gate", "Harness Policy Gate", "local"),
    ("report_builder", "诊断报告 Agent", "llm"),
]


def _audit(session, run: AnalysisRun, event_type: str, payload: dict | None = None) -> None:
    session.add(
        AuditEvent(
            id=new_id("audit"),
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type=event_type,
            subject_hash=hashlib.sha256(run.workspace_id.encode()).hexdigest()[:16],
            payload=payload or {},
        )
    )


def _touch(run: AnalysisRun, *, status: str | None = None, phase: str | None = None) -> None:
    if status:
        run.status = status
    if phase:
        run.current_phase = phase
    run.updated_at_ms = now_ms()
    run.revision += 1
    run.checkpoint_version += 1


def create_steps(session, run: AnalysisRun) -> None:
    for step_key, label, mode in STEP_DEFS:
        session.add(
            AgentStep(
                id=new_id("step"),
                workspace_id=run.workspace_id,
                run_id=run.id,
                step_key=step_key,
                label=label,
                status="pending",
                execution_mode=mode,
                input_sha256=hashlib.sha256(f"{run.id}:{step_key}".encode()).hexdigest(),
            )
        )


def _step(session, run_id: str, step_key: str) -> AgentStep:
    return session.scalar(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.step_key == step_key))


def _artifact(session, run_id: str, step_key: str) -> dict | None:
    artifact = session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run_id, AgentArtifact.step_key == step_key, AgentArtifact.is_current.is_(True)))
    return artifact.payload if artifact else None


def _begin_step(run_id: str, step_key: str) -> bool:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        step = _step(session, run_id, step_key)
        if not run or not step or step.status == "succeeded" or run.status not in {"running", "queued"}:
            return False
        if run.expires_at_ms and run.expires_at_ms <= now_ms():
            raise TimeoutError("RUN_EXPIRED")
        step.status = "running"
        step.attempt_count += 1
        step.started_at_ms = now_ms()
        _touch(run, phase=step_key)
        _audit(session, run, "step.started", {"step_key": step_key})
        session.commit()
        return True


def _reserve_live_request(run_id: str | None = None) -> bool:
    if not settings.deepseek_api_key:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id) if run_id else None
        if run_id and (not run or run.status != "running"):
            return False
        quota = session.get(DailyLiveQuota, today)
        if not quota:
            quota = DailyLiveQuota(day_utc=today, reserved_count=0, limit_snapshot=settings.live_daily_limit, purge_after_ms=now_ms() + 72 * 3600 * 1000)
            session.add(quota)
        if quota.reserved_count >= quota.limit_snapshot:
            return False
        quota.reserved_count += 1
        if run and not run.live_reserved_at_ms:
            run.live_reserved_at_ms = now_ms()
        session.commit()
        return True


def reserve_probe_attempt() -> bool:
    return _reserve_live_request()


def _complete_step(
    run_id: str,
    step_key: str,
    payload: dict,
    execution_mode: str = "local",
    actual_model: str | None = None,
    latency_ms: int = 120,
    error_code: str | None = None,
) -> None:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        step = _step(session, run_id, step_key)
        if not run or not step or step.status == "succeeded":
            return
        if run.hard_expires_at_ms and run.hard_expires_at_ms <= now_ms():
            raise TimeoutError("RUN_EXPIRED")
        step.status = "succeeded"
        step.execution_mode = execution_mode
        step.actual_model = actual_model
        step.latency_ms = latency_ms
        step.error_code = error_code
        step.completed_at_ms = now_ms()
        session.add(
            AgentArtifact(
                id=new_id("artifact"),
                workspace_id=run.workspace_id,
                run_id=run.id,
                step_key=step_key,
                artifact_type=f"{step_key}.v1",
                payload=payload,
            )
        )
        run.completed_steps += 1
        modes = session.scalars(select(AgentStep.execution_mode).where(AgentStep.run_id == run_id, AgentStep.status == "succeeded")).all()
        if "live" in modes and "fallback" in modes:
            run.execution_mode = "mixed"
        elif "live" in modes:
            run.execution_mode = "live"
        elif "fallback" in modes:
            run.execution_mode = "fallback"
        _touch(run, phase=step_key)
        _audit(session, run, "step.succeeded", {"step_key": step_key, "mode": execution_mode, "error_code": error_code})
        session.commit()


def _fail_model_step(run_id: str, error_code: str, latency_ms: int) -> None:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        step = _step(session, run_id, "feedback")
        if not run or not step or run.status not in {"running", "queued"}:
            return
        step.status = "failed"
        step.execution_mode = "failed"
        step.latency_ms = latency_ms
        step.error_code = error_code
        step.completed_at_ms = now_ms()
        run.error_code = error_code
        run.execution_mode = "failed"
        _touch(run, status="failed", phase="feedback")
        _audit(session, run, "run.failed", {"step_key": "feedback", "error_code": error_code})
        session.commit()


def _create_decision_entities(run_id: str) -> bool:
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        existing = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id))
        if existing:
            return True
        if session.scalar(select(Finding).where(Finding.run_id == run_id)):
            return False
        facts = _artifact(session, run_id, "snapshot")
        feedback = _artifact(session, run_id, "feedback")
        correlation = _artifact(session, run_id, "correlation")
        target = facts["target"]
        kind = correlation["kind"]
        if kind == "none":
            return False
        scope = f'{target["market"]} / {target["creative_id"]}'
        if kind == "cross_domain":
            claim = f'{scope} 在两个窗口出现广告效率下滑与相关反馈上升，按平台、市场和 Creative 存在证据关联；候选假设待 A/B 实验验证，不构成因果证明。'
        elif kind == "metric_only":
            claim = f'{scope} 的广告指标出现异常，但缺少同期可直接关联的反馈上升；仅作投放单侧分诊，不推断用户体验原因。'
        else:
            claim = f'{scope} 的相关反馈上升，但投放指标未达到异常阈值；仅作反馈单侧分诊，不建议高风险调整。'
        if correlation.get("agent"):
            claim = correlation["agent"]["analysis"]["summary"]
        finding = Finding(
            id=new_id("finding"),
            workspace_id=run.workspace_id,
            run_id=run.id,
            title=f'{target["creative_id"]} ' + ("联合异常" if kind == "cross_domain" else "单侧信号"),
            claim=claim,
            incident_priority="P1" if kind == "cross_domain" else "P2",
            confidence=0.82 if kind == "cross_domain" and correlation["link_strength"] == "A" else 0.62,
            link_strength=correlation["link_strength"],
            causality_status="unverified",
        )
        session.add(finding)
        session.flush()
        evidence = [("feedback", "相关主题反馈", f'{feedback["related"]["baseline"]} → {feedback["related"]["current"]}', feedback["representative_ids"][0] if feedback["representative_ids"] else run.dataset_id)]
        for metric_name, label in (("ctr", "CTR"), ("cvr", "CVR"), ("cpi", "CPI"), ("roas_d7", "ROAS D7"), ("frequency", "Frequency")):
            values = target["metrics"][metric_name]
            if values["baseline"] is not None and values["current"] is not None:
                multiplier = 100 if metric_name in {"ctr", "cvr"} else 1
                suffix = "%" if multiplier == 100 else ""
                evidence.append(("metric", label, f'{values["baseline"] * multiplier:.2f}{suffix} → {values["current"] * multiplier:.2f}{suffix}', target["source_ids"]["current"]))
        if facts["stable_controls"]:
            evidence.append(("control", "稳定控制", ", ".join(facts["stable_controls"][:3]), run.dataset_id))
        for evidence_type, label, value, target_id in evidence:
            session.add(EvidenceRef(id=new_id("ev"), workspace_id=run.workspace_id, run_id=run.id, finding_id=finding.id, evidence_type=evidence_type, target_id=target_id, label=label, value=value))
        actions = [
            ("生成 Creative Brief", "R1", "allowed", True, {"scope": "local", "hypotheses": correlation["hypotheses"]}),
            ("预算、出价、停投与扩量建议", "R3", "suggest_only", False, {"executed": False}),
        ]
        needs_approval = kind == "cross_domain" and correlation["link_strength"] in {"A", "B"}
        if needs_approval:
            actions.insert(1, ("创建素材诊断内部工单", "R2", "waiting_approval", False, {"tool": "sandbox.ticket.create"}))
        for title, risk, status, executed, details in actions:
            action = ActionPlan(id=new_id("action"), workspace_id=run.workspace_id, run_id=run.id, title=title, risk_level=risk, status=status, executed=executed, details=details)
            session.add(action)
            session.flush()
            session.add(ActionFinding(action_id=action.id, finding_id=finding.id))
        experiments = [
            ("实验 A · 新素材，保持原承诺", "CTR", ["CVR", "CPI"], "检验新素材表达能否在保持产品承诺不变时改善 CTR。"),
            ("实验 B · 承诺对齐 Variant", "CVR", ["CTR", "D7 ROAS"], "检验广告承诺与实际产品体验对齐后，CVR 是否改善。"),
        ] if kind == "cross_domain" else []
        planner = _artifact(session, run_id, "planner") or {}
        cards = planner["experiments"] if planner.get("agent") else [
            {"title": title, "primary_metric": primary, "guardrails": guardrails, "duration_days": 7,
             "validation_goal": goal, "stop_condition": "护栏指标出现恶化时停止并转人工复核"}
            for title, primary, guardrails, goal in experiments]
        for card in cards:
            session.add(ExperimentCard(id=new_id("exp"), workspace_id=run.workspace_id, run_id=run.id, **card))
        if not needs_approval:
            session.commit()
            return False
        tool = ToolCall(id=new_id("tool"), workspace_id=run.workspace_id, run_id=run.id, tool_name="sandbox.ticket.create", risk_level="R2", status="waiting_approval", args_sha256=hashlib.sha256(f"{run.id}:ticket".encode()).hexdigest(), executed=False)
        session.add(tool)
        session.flush()
        session.add(PolicyDecision(id=new_id("policy"), workspace_id=run.workspace_id, run_id=run.id, tool_call_id=tool.id, policy_version="policy-1.0", decision="require_approval", matched_rule="R2_SANDBOX_WRITE", reason="当前内部工单写入会改变 Workspace 状态，需要当前用户确认。"))
        approval = ApprovalRequest(id=new_id("approval"), workspace_id=run.workspace_id, run_id=run.id, tool_call_id=tool.id, status="pending", expires_at_ms=min(run.expires_at_ms or now_ms() + 1_800_000, now_ms() + 1_800_000))
        session.add(approval)
        _touch(run, status="waiting_approval", phase="waiting_approval")
        _audit(session, run, "approval.requested", {"approval_id": approval.id, "risk": "R2"})
        session.commit()
        return True


async def execute_run(run_id: str) -> None:
    with db_session() as session:
        run = session.get(AnalysisRun, run_id)
        dataset = session.get(Dataset, run.dataset_id) if run else None
        use_agents = dataset and dataset.source == "upload" and bool(settings.deepseek_api_key)
    if use_agents:
        from .agent_graph import execute
        await execute(run_id)
        return
    try:
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            if not run or run.status not in {"queued", "running"}:
                return
            dataset = session.get(Dataset, run.dataset_id)
            _touch(run, status="running", phase="validator")
            _audit(session, run, "run.started")
            session.commit()
        if _begin_step(run_id, "validator"):
            _complete_step(run_id, "validator", {"feedback_rows": dataset.feedback_rows, "metric_rows": dataset.metric_rows, "blocking_issues": 0})
        if _begin_step(run_id, "snapshot"):
            with db_session() as session:
                facts = snapshot(session, dataset.id)
            _complete_step(run_id, "snapshot", facts)
        with db_session() as session:
            facts = _artifact(session, run_id, "snapshot")
            feedback_rows = session.scalars(select(FeedbackRecord).where(FeedbackRecord.dataset_id == dataset.id)).all()
        feedback_started = _begin_step(run_id, "feedback")
        performance_started = _begin_step(run_id, "performance")
        if performance_started:
            target = facts["target"]
            _complete_step(run_id, "performance", {"anomaly": target["anomaly"], "creative_id": target["creative_id"], "metrics": target["metrics"], "stable_controls": facts["stable_controls"]})
        if feedback_started:
            mode, model, latency, error, labels = "fallback", None, 0, "GOLDEN_FIXED" if dataset.source == "golden" else "LLM_NOT_CONFIGURED", {}
            if dataset.source == "upload" and settings.deepseek_api_key:
                mode, error = "live", None
                candidate_labels: dict[str, str] = {}
                for start in range(0, len(feedback_rows), BATCH_SIZE):
                    batch = [{"id": row.id, "text": row.text} for row in feedback_rows[start:start + BATCH_SIZE]]
                    batch_mode, batch_model, batch_latency, batch_error, batch_labels = await classify_feedback(batch, reserve_attempt=lambda: _reserve_live_request(run_id))
                    latency += batch_latency
                    if batch_mode != "live" or not batch_model or set(batch_labels) != {item["id"] for item in batch} or any(value not in ALLOWED_TOPICS for value in batch_labels.values()):
                        mode, model, error = "fallback", None, batch_error or "LLM_SCHEMA_INVALID"
                        break
                    if model is not None and model != batch_model:
                        mode, model, error = "fallback", None, "LLM_MODEL_INCONSISTENT"
                        break
                    model = batch_model
                    candidate_labels.update(batch_labels)
                if not feedback_rows:
                    mode, error = "fallback", "LLM_NO_INPUT"
                if mode == "live":
                    labels = candidate_labels
            if dataset.source == "upload" and settings.app_mode == "production" and mode != "live":
                _fail_model_step(run_id, error or "LLM_PROVIDER_ERROR", latency)
                return
            with db_session() as session:
                feedback = feedback_cluster(session, dataset.id, facts["target"], labels)
            _complete_step(run_id, "feedback", feedback, execution_mode=mode, actual_model=model, latency_ms=latency, error_code=error)
        with db_session() as session:
            feedback = _artifact(session, run_id, "feedback")
        if _begin_step(run_id, "correlation"):
            _complete_step(run_id, "correlation", correlate(facts, feedback))
        with db_session() as session:
            correlation = _artifact(session, run_id, "correlation")
        if _begin_step(run_id, "planner"):
            _complete_step(run_id, "planner", {"hypotheses": correlation["hypotheses"], "experiments": 2 if correlation["kind"] == "cross_domain" else 0, "action_risks": ["R1", "R2", "R3"] if correlation["kind"] == "cross_domain" and correlation["link_strength"] in {"A", "B"} else ["R1", "R3"] if correlation["kind"] != "none" else []})
        if _begin_step(run_id, "auditor"):
            _complete_step(run_id, "auditor", {"passed": True, "causality": "unverified", "metrics_source": facts["target"]["source_ids"], "numeric_source": "metrics-1.0"})
        if _begin_step(run_id, "policy_gate"):
            _complete_step(run_id, "policy_gate", {"R1": "allow", "R2": "require_approval" if correlation["kind"] == "cross_domain" and correlation["link_strength"] in {"A", "B"} else "not_requested", "R3": "suggest_only"})
        if not _create_decision_entities(run_id):
            with WRITE_LOCK, db_session() as session:
                run = session.get(AnalysisRun, run_id)
                _touch(run, status="queued", phase="report_builder")
                session.commit()
            await resume_after_approval(run_id)
    except TimeoutError:
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            if run:
                _touch(run, status="expired", phase="expired")
                session.commit()
    except Exception:
        with WRITE_LOCK, db_session() as session:
            run = session.get(AnalysisRun, run_id)
            if run and run.status not in {"succeeded", "expired"}:
                run.error_code = "RUN_CONSISTENCY_ERROR"
                _touch(run, status="failed", phase="failed")
                _audit(session, run, "run.failed", {"error_code": run.error_code})
                session.commit()


def _report_payload(session, run: AnalysisRun) -> dict:
    dataset = session.get(Dataset, run.dataset_id)
    facts = _artifact(session, run.id, "snapshot")
    feedback = _artifact(session, run.id, "feedback")
    correlation = _artifact(session, run.id, "correlation")
    finding = session.scalar(select(Finding).where(Finding.run_id == run.id))
    actions = session.scalars(select(ActionPlan).where(ActionPlan.run_id == run.id)).all()
    experiments = session.scalars(select(ExperimentCard).where(ExperimentCard.run_id == run.id)).all()
    ticket = session.scalar(select(SandboxTicket).where(SandboxTicket.run_id == run.id))
    approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id))
    evidence = session.scalars(select(EvidenceRef).where(EvidenceRef.run_id == run.id)).all()
    return {
        "fixture": "golden-br-cr17-v1" if dataset.source == "golden" else None,
        "source": dataset.source,
        "scope": {key: facts["target"][key] for key in ("market", "platform", "campaign_id", "creative_id")},
        "summary": finding.claim if finding else "当前数据未达到异常阈值；已保存可复核的事实快照，未创建高风险动作。",
        "metrics": facts["target"]["metrics"],
        "feedback": feedback,
        "correlation": correlation,
        "finding": None if not finding else {"id": finding.id, "title": finding.title, "claim": finding.claim, "incident_priority": finding.incident_priority, "confidence": finding.confidence, "link_strength": finding.link_strength, "causality_status": finding.causality_status},
        "evidence": [{"type": e.evidence_type, "label": e.label, "value": e.value} for e in evidence],
        "actions": [{"title": a.title, "risk_level": a.risk_level, "status": a.status, "executed": a.executed, "details": a.details} for a in actions],
        "experiments": [{"title": e.title, "primary_metric": e.primary_metric, "guardrails": e.guardrails, "duration_days": e.duration_days, "validation_goal": e.validation_goal, "stop_condition": e.stop_condition} for e in experiments],
        "approval": None if not approval else {"id": approval.id, "status": approval.status, "decided_at_ms": approval.decided_at_ms},
        "ticket": None if not ticket else {"ticket_no": ticket.ticket_no, "title": ticket.title, "status": ticket.status},
        "external_writes": 0,
        "agents": {key: value["agent"] for key in ("feedback", "performance", "correlation", "planner", "auditor")
                   if (value := _artifact(session, run.id, key)) and value.get("agent")},
        "limitations": ["本结果展示证据关联，不构成因果证明。", "数据来源为合成数据。" if dataset.source == "golden" else "上传数据由用户确认已脱敏；结论取决于输入质量。", "系统未执行任何真实广告平台或外部工单写入。"],
        "versions": {"dag": "dag-1.0", "formula": "metrics-1.0", "policy": "policy-1.0", "schema": "artifacts-1.0"},
    }


def _report_markdown(payload: dict) -> str:
    lines = ["# GrowthTriage 诊断报告", "", f'数据来源：{payload["source"]} · 执行模式：{payload.get("execution_mode", "fallback")}', "", "## Executive Summary", payload["summary"], "", "## 指标事实 · metrics-1.0", "| 指标 | Baseline | Current | Delta % |", "| --- | ---: | ---: | ---: |"]
    for name, values in payload["metrics"].items():
        cells = ["N/A" if values[key] is None else f'{values[key]:.4f}' for key in ("baseline", "current", "delta_pct")]
        lines.append(f'| {name} | {cells[0]} | {cells[1]} | {cells[2]} |')
    lines.extend(["", "## 反馈主题", f'相关主题：{payload["feedback"]["related"]["baseline"]} → {payload["feedback"]["related"]["current"]}', "", "## Finding 与证据", payload["finding"]["claim"] if payload["finding"] else "未达到异常阈值。"])
    for item in payload["evidence"]:
        lines.append(f'- {item["label"]}：{item["value"]}')
    lines.extend(["", "## 实验卡"])
    for item in payload["experiments"]:
        lines.append(f'- {item["title"]}；主指标 {item["primary_metric"]}；护栏 {", ".join(item["guardrails"])}；{item["validation_goal"]}')
    if payload.get("agents"):
        lines.extend(["", "## 六 Agent 协作 · LangGraph"])
        for agent in payload["agents"].values():
            lines.append(f'- {agent["name"]}：{agent["analysis"]["summary"]}')
        lines.append("- 诊断报告 Agent：生成本报告摘要；指标和执行状态由程序渲染。")
        if payload.get("agent_review"):
            lines.append(f'- 终稿审核：{payload["agent_review"]["verdict"]}；{payload["agent_review"]["summary"]}')
    lines.extend(["", "## Harness 与审批"])
    lines.append(f'审批：{payload["approval"]["status"] if payload["approval"] else "无需审批"}；内部工单：{payload["ticket"]["ticket_no"] if payload["ticket"] else "无"}')
    for item in payload["actions"]:
        lines.append(f'- {item["risk_level"]} {item["title"]}：{item["status"]}；executed={str(item["executed"]).lower()}')
    lines.extend(["", "## 限制与版本"])
    lines.extend(f'- {item}' for item in payload["limitations"])
    lines.append(f'版本：{json.dumps(payload["versions"], ensure_ascii=False)}')
    return "\n".join(lines) + "\n"


def _redaction_passed(payload: dict, markdown: str) -> bool:
    text = json.dumps(payload, ensure_ascii=False) + markdown
    return not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:[A-Z]:\\|/Users/|/home/)|\bsk-[A-Za-z0-9]{12,}\b", text)


async def resume_after_approval(run_id: str) -> None:
    with db_session() as session:
        validator = _artifact(session, run_id, "validator") or {}
    if validator.get("graph") == "six-agents-v1":
        from .agent_graph import finish
        await finish(run_id)
        return
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        if not run or run.status != "queued":
            return
        if run.expires_at_ms and run.expires_at_ms <= now_ms():
            _touch(run, status="expired", phase="expired")
            session.commit()
            return
        existing_report = session.scalar(select(ReportBundle).where(ReportBundle.run_id == run_id))
        if existing_report:
            _touch(run, status="succeeded", phase="complete")
            session.commit()
            return
        _touch(run, status="running", phase="report_builder")
        session.commit()
    if not _begin_step(run_id, "report_builder"):
        return
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        if not run or run.status != "running":
            return
        payload = _report_payload(session, run)
        payload["execution_mode"] = run.execution_mode
        markdown = _report_markdown(payload)
        redaction_passed = _redaction_passed(payload, markdown)
        session.add(ReportBundle(id=new_id("report"), workspace_id=run.workspace_id, run_id=run.id, status="ready" if redaction_passed else "blocked", payload=payload, markdown=markdown, redaction_passed=redaction_passed))
        session.commit()
    _complete_step(run_id, "report_builder", {"report": "ready" if redaction_passed else "blocked", "redaction_passed": redaction_passed})
    with WRITE_LOCK, db_session() as session:
        run = session.get(AnalysisRun, run_id)
        _touch(run, status="succeeded", phase="complete")
        _audit(session, run, "run.succeeded", {"external_writes": 0})
        session.commit()


def decide_approval(approval_id: str, workspace_id: str, decision: str, decision_key: str, comment: str) -> tuple[ApprovalRequest, SandboxTicket | None, bool]:
    normalized_comment = comment.strip()
    payload_hash = hashlib.sha256(json.dumps({"approval_id": approval_id, "decision": decision, "comment": normalized_comment}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with WRITE_LOCK, db_session() as session:
        approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id, ApprovalRequest.workspace_id == workspace_id))
        if not approval:
            raise KeyError("RESOURCE_NOT_FOUND")
        if approval.status != "pending":
            if approval.decision_key == decision_key and approval.decision_payload_sha256 == payload_hash:
                ticket = session.scalar(select(SandboxTicket).where(SandboxTicket.approval_id == approval.id))
                return approval, ticket, False
            raise ValueError("APPROVAL_ALREADY_DECIDED")
        if approval.expires_at_ms <= now_ms():
            approval.status = "expired"
            session.commit()
            raise TimeoutError("APPROVAL_EXPIRED")
        run = session.get(AnalysisRun, approval.run_id)
        tool = session.get(ToolCall, approval.tool_call_id)
        approval.status = decision
        approval.decision_key = decision_key
        approval.decision_payload_sha256 = payload_hash
        approval.comment = normalized_comment or None
        approval.decided_at_ms = now_ms()
        ticket = None
        if decision == "approved":
            tool.status = "succeeded"
            tool.executed = True
            action = session.scalar(select(ActionPlan).where(ActionPlan.run_id == run.id, ActionPlan.risk_level == "R2"))
            action.status = "executed"
            action.executed = True
            finding = session.scalar(select(Finding).where(Finding.run_id == run.id))
            ticket = SandboxTicket(id=new_id("ticket"), workspace_id=workspace_id, run_id=run.id, approval_id=approval.id, ticket_no=f"GT-{uuid.uuid4().hex[:8].upper()}", title=f"{finding.title} 诊断", status="open")
            session.add(ticket)
        else:
            tool.status = "rejected"
            tool.executed = False
            action = session.scalar(select(ActionPlan).where(ActionPlan.run_id == run.id, ActionPlan.risk_level == "R2"))
            action.status = "rejected"
            action.executed = False
        _touch(run, status="queued", phase="resume_after_approval")
        _audit(session, run, "approval.decided", {"approval_id": approval.id, "decision": decision})
        session.commit()
        session.refresh(approval)
        if ticket:
            session.refresh(ticket)
        return approval, ticket, True


_runner_loop: asyncio.AbstractEventLoop | None = None
_runner_slots: asyncio.Semaphore | None = None


def schedule(coro_factory) -> asyncio.Task:
    global _runner_loop, _runner_slots
    loop = asyncio.get_running_loop()
    if _runner_loop is not loop:
        _runner_loop = loop
        _runner_slots = asyncio.Semaphore(2)
    slots = _runner_slots

    async def run_limited() -> None:
        async with slots:
            await coro_factory()

    return loop.create_task(run_limited())
