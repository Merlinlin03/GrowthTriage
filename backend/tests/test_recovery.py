import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.db import (AgentStep, AnalysisRun, ApprovalRequest, ReportBundle, SandboxTicket,
                    Workspace, db_session, init_db, now_ms)
from app.services.golden import create_golden_dataset, new_id
from app.services.orchestrator import create_steps, decide_approval, execute_run, resume_after_approval
from app.services.recovery import recover_runs


def make_run():
    init_db()
    with db_session() as session:
        ws = Workspace(id=new_id("ws"), role="guest", expires_at_ms=now_ms() + 60000)
        session.add(ws)
        session.flush()
        dataset = create_golden_dataset(session, ws.id)
        run = AnalysisRun(id=new_id("run"), workspace_id=ws.id, dataset_id=dataset.id,
                          status="queued", idempotency_key=str(uuid.uuid4()), request_sha256="test",
                          expires_at_ms=ws.expires_at_ms)
        session.add(run)
        session.flush()
        create_steps(session, run)
        session.commit()
        return run.id, ws.id


def test_uncertain_running_fails_without_requeue():
    run_id, _ = make_run()
    with db_session() as session:
        session.get(AnalysisRun, run_id).status = "running"
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.step_key == "feedback"))
        step.status = "running"
        step.attempt_count = 1
        session.commit()
    assert all(item[0] != run_id for item in recover_runs())
    with db_session() as session:
        assert session.get(AnalysisRun, run_id).error_code == "PROCESS_INTERRUPTED"
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.step_key == "feedback"))
        assert step.status == "failed" and step.attempt_count == 1
        assert not session.scalar(select(ReportBundle).where(ReportBundle.run_id == run_id))


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_pending_and_committed_approval_recovery(decision):
    run_id, workspace_id = make_run()
    asyncio.run(execute_run(run_id))
    assert all(item[0] != run_id for item in recover_runs())
    with db_session() as session:
        approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id))
        approval_id = approval.id
    decide_approval(approval_id, workspace_id, decision, str(uuid.uuid4()), "recovery")
    # Simulate crash after the queued run was acquired for report construction.
    with db_session() as session:
        session.get(AnalysisRun, run_id).status = "running"
        session.commit()
    assert (run_id, True) in recover_runs()
    asyncio.run(resume_after_approval(run_id))
    # Simulate report commit before final run status commit.
    with db_session() as session:
        session.get(AnalysisRun, run_id).status = "running"
        session.commit()
    assert all(item[0] != run_id for item in recover_runs())
    with db_session() as session:
        assert session.get(AnalysisRun, run_id).status == "succeeded"
        assert session.scalar(select(func.count()).select_from(ReportBundle).where(ReportBundle.run_id == run_id)) == 1
        assert session.scalar(select(func.count()).select_from(SandboxTicket).where(SandboxTicket.run_id == run_id)) == (1 if decision == "approved" else 0)


def test_expired_run_is_not_requeued():
    run_id, _ = make_run()
    with db_session() as session:
        session.get(AnalysisRun, run_id).expires_at_ms = now_ms() - 1
        session.commit()
    assert all(item[0] != run_id for item in recover_runs())
    with db_session() as session:
        assert session.get(AnalysisRun, run_id).status == "expired"
