from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, default="guest")
    status: Mapped[str] = mapped_column(String, default="active")
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)
    expires_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hard_expires_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_limit: Mapped[int] = mapped_column(Integer, default=3)


class AppSession(Base):
    __tablename__ = "app_session"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_sha256: Mapped[str] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)
    expires_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DailyLiveQuota(Base):
    __tablename__ = "daily_live_quota"
    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    reserved_count: Mapped[int] = mapped_column(Integer, default=0)
    limit_snapshot: Mapped[int] = mapped_column(Integer, default=30)
    purge_after_ms: Mapped[int] = mapped_column(Integer)


class Dataset(Base):
    __tablename__ = "dataset"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String, default="golden")
    status: Mapped[str] = mapped_column(String, default="ready")
    name: Mapped[str] = mapped_column(String, default="Golden Fixture")
    feedback_rows: Mapped[int] = mapped_column(Integer, default=0)
    metric_rows: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)


class AnalysisWindow(Base):
    __tablename__ = "analysis_window"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), index=True)
    window_type: Mapped[str] = mapped_column(String)
    start_date: Mapped[str] = mapped_column(String)
    end_date: Mapped[str] = mapped_column(String)
    timezone_name: Mapped[str] = mapped_column(String, default="UTC")


class FeedbackRecord(Base):
    __tablename__ = "feedback_record"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), index=True)
    window_type: Mapped[str] = mapped_column(String)
    locale: Mapped[str] = mapped_column(String)
    market: Mapped[str] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String)
    campaign_id: Mapped[str] = mapped_column(String)
    creative_id: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    audience_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    link_method: Mapped[str | None] = mapped_column(String, nullable=True)
    topic: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)


class MetricSlice(Base):
    __tablename__ = "metric_slice"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), index=True)
    window_type: Mapped[str] = mapped_column(String)
    market: Mapped[str] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String)
    campaign_id: Mapped[str] = mapped_column(String)
    creative_id: Mapped[str] = mapped_column(String)
    spend: Mapped[float] = mapped_column(Float)
    impressions: Mapped[int] = mapped_column(Integer)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clicks: Mapped[int] = mapped_column(Integer)
    installs: Mapped[int] = mapped_column(Integer)
    revenue_d7: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, default="USD")
    conversion_source: Mapped[str | None] = mapped_column(String, nullable=True)
    attribution_window: Mapped[str | None] = mapped_column(String, nullable=True)


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issue"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), index=True)
    severity: Mapped[str] = mapped_column(String)
    code: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    execution_mode: Mapped[str] = mapped_column(String, default="fallback")
    current_phase: Mapped[str] = mapped_column(String, default="queued")
    completed_steps: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=9)
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_sha256: Mapped[str] = mapped_column(String(64))
    live_reserved_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hard_expires_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_run_idempotency"),)


class AgentStep(Base):
    __tablename__ = "agent_step"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    execution_mode: Mapped[str] = mapped_column(String, default="local")
    actual_model: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    input_sha256: Mapped[str] = mapped_column(String(64), default="")
    started_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (UniqueConstraint("run_id", "step_key", name="uq_run_step"),)


class AgentArtifact(Base):
    __tablename__ = "agent_artifact"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String)
    artifact_type: Mapped[str] = mapped_column(String)
    schema_version: Mapped[str] = mapped_column(String, default="artifacts-1.0")
    payload: Mapped[dict] = mapped_column(JSON)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)


class Finding(Base):
    __tablename__ = "finding"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), unique=True)
    title: Mapped[str] = mapped_column(String)
    claim: Mapped[str] = mapped_column(Text)
    incident_priority: Mapped[str] = mapped_column(String, default="P1")
    confidence: Mapped[float] = mapped_column(Float, default=0.82)
    link_strength: Mapped[str] = mapped_column(String, default="A")
    causality_status: Mapped[str] = mapped_column(String, default="unverified")


class EvidenceRef(Base):
    __tablename__ = "evidence_ref"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id", ondelete="CASCADE"), index=True)
    evidence_type: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)


class ActionPlan(Base):
    __tablename__ = "action_plan"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String)
    risk_level: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="planned")
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ActionFinding(Base):
    __tablename__ = "action_finding"
    action_id: Mapped[str] = mapped_column(ForeignKey("action_plan.id", ondelete="CASCADE"), primary_key=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id", ondelete="CASCADE"), primary_key=True)


class ExperimentCard(Base):
    __tablename__ = "experiment_card"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String)
    primary_metric: Mapped[str] = mapped_column(String)
    guardrails: Mapped[list] = mapped_column(JSON)
    duration_days: Mapped[int] = mapped_column(Integer, default=7)
    validation_goal: Mapped[str] = mapped_column(Text)
    stop_condition: Mapped[str] = mapped_column(Text)


class ToolCall(Base):
    __tablename__ = "tool_call"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String)
    risk_level: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    args_sha256: Mapped[str] = mapped_column(String(64))
    executed: Mapped[bool] = mapped_column(Boolean, default=False)


class PolicyDecision(Base):
    __tablename__ = "policy_decision"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    tool_call_id: Mapped[str] = mapped_column(ForeignKey("tool_call.id", ondelete="CASCADE"), unique=True)
    policy_version: Mapped[str] = mapped_column(String, default="policy-1.0")
    decision: Mapped[str] = mapped_column(String)
    matched_rule: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)


class ApprovalRequest(Base):
    __tablename__ = "approval_request"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    tool_call_id: Mapped[str] = mapped_column(ForeignKey("tool_call.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    requested_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)
    expires_at_ms: Mapped[int] = mapped_column(Integer)
    decision_key: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (Index("uq_approval_decision_key", "workspace_id", "decision_key", unique=True, sqlite_where=(decision_key.is_not(None))),)


class SandboxTicket(Base):
    __tablename__ = "sandbox_ticket"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_request.id", ondelete="CASCADE"), unique=True)
    ticket_no: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)


class ReportBundle(Base):
    __tablename__ = "report_bundle"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String, default="ready")
    payload: Mapped[dict] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text)
    redaction_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    subject_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(Integer, default=now_ms)


db_path = settings.database_url.removeprefix("sqlite:///")
if settings.database_url.startswith("sqlite:///"):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
WRITE_LOCK = threading.RLock()


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Preserve existing local demo databases while adding the upload metadata.
    with engine.begin() as connection:
        for table, columns in {
            "feedback_record": ("created_at", "audience_stage", "link_method"),
            "metric_slice": ("conversion_source", "attribution_window"),
        }.items():
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR")


@contextmanager
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
