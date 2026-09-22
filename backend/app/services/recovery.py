"""Conservative single-process recovery; never replay uncertain model requests."""
from sqlalchemy import select

from ..db import (AgentArtifact, AgentStep, AnalysisRun, ApprovalRequest, AuditEvent, ReportBundle,
                  WRITE_LOCK, db_session, now_ms)
from .golden import new_id


def recover_runs() -> list[tuple[str, bool]]:
    queued = []
    with WRITE_LOCK, db_session() as session:
        runs = session.scalars(select(AnalysisRun).where(
            AnalysisRun.status.in_(["queued", "running", "waiting_approval"])
        )).all()
        for run in runs:
            previous = run.status
            approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id))
            report = session.scalar(select(ReportBundle).where(ReportBundle.run_id == run.id))
            validator = session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run.id,
                AgentArtifact.step_key == "validator", AgentArtifact.is_current.is_(True)))
            uncertain_agent_call = previous == "running" and validator and validator.payload.get("graph") == "six-agents-v1"
            if run.expires_at_ms and run.expires_at_ms <= now_ms():
                run.status = "expired"
                run.current_phase = "expired"
                if approval and approval.status == "pending":
                    approval.status = "expired"
            elif report:
                run.status = "succeeded"
                run.current_phase = "complete"
                step = session.scalar(select(AgentStep).where(
                    AgentStep.run_id == run.id, AgentStep.step_key == "report_builder"))
                if step and step.status != "succeeded":
                    step.status = "succeeded"
                    step.completed_at_ms = now_ms()
                    run.completed_steps += 1
            elif approval and approval.status == "pending":
                run.status = "waiting_approval"
            elif approval and approval.status in {"approved", "rejected"} and not uncertain_agent_call:
                run.status = "queued"
                run.current_phase = "resume_after_approval"
                queued.append((run.id, True))
            elif previous == "queued" and not approval:
                queued.append((run.id, False))
            else:
                run.status = "failed"
                run.error_code = "PROCESS_INTERRUPTED"
                for step in session.scalars(select(AgentStep).where(
                    AgentStep.run_id == run.id, AgentStep.status == "running")):
                    step.status = "failed"
                    step.error_code = "PROCESS_INTERRUPTED"
                    step.completed_at_ms = now_ms()
            if run.status != previous or run.status == "failed":
                run.revision += 1
                run.updated_at_ms = now_ms()
                session.add(AuditEvent(id=new_id("audit"), workspace_id=run.workspace_id,
                    run_id=run.id, event_type="run.recovered",
                    payload={"previous_status": previous, "status": run.status}))
        session.commit()
    return queued
