from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .config import settings
from .db import (
    ActionPlan,
    AgentArtifact,
    AgentStep,
    AnalysisRun,
    AppSession,
    ApprovalRequest,
    AuditEvent,
    AnalysisWindow,
    DataQualityIssue,
    Dataset,
    EvidenceRef,
    ExperimentCard,
    Finding,
    ReportBundle,
    SandboxTicket,
    ToolCall,
    Workspace,
    WRITE_LOCK,
    db_session,
    init_db,
    now_ms,
)
from .schemas import AdminLoginIn, ApprovalDecisionIn
from .services.golden import create_golden_dataset, manifest, new_id
from .services.admin_auth import verify_password
from .services.deepseek import probe_model
from .services.orchestrator import STEP_DEFS, create_steps, decide_approval, execute_run, reserve_probe_attempt, resume_after_approval, schedule
from .services.upload import FEEDBACK_TEMPLATE, METRIC_TEMPLATE, UploadValidationError, store_dataset
from .services.recovery import recover_runs
from .services.request_limits import RequestLimits


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def utc_iso(epoch_ms: int | None = None) -> str:
    value = datetime.fromtimestamp((epoch_ms or now_ms()) / 1000, tz=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def envelope(data, request_id: str | None = None) -> dict:
    return {"data": data, "meta": {"request_id": request_id or f"req_{uuid.uuid4().hex}", "server_time": utc_iso()}}


def api_error(code: str, message: str, status: int, field_errors: list | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "field_errors": field_errors or []})


def _load_session(request: Request) -> tuple[Workspace, AppSession] | None:
    raw = request.cookies.get("gt_session")
    if not raw:
        return None
    with db_session() as session:
        app_session = session.scalar(select(AppSession).where(AppSession.token_sha256 == sha256(raw), AppSession.revoked_at_ms.is_(None)))
        if not app_session or (app_session.expires_at_ms and app_session.expires_at_ms <= now_ms()):
            return None
        workspace = session.get(Workspace, app_session.workspace_id)
        if not workspace or workspace.status != "active" or (workspace.expires_at_ms and workspace.expires_at_ms <= now_ms()):
            return None
        session.expunge(workspace)
        session.expunge(app_session)
        return workspace, app_session


def require_session(request: Request) -> tuple[Workspace, AppSession]:
    loaded = _load_session(request)
    if not loaded:
        raise api_error("SESSION_REQUIRED", "请先创建访客工作区。", 401)
    return loaded


def require_csrf(request: Request, loaded=Depends(require_session)) -> tuple[Workspace, AppSession]:
    _workspace, app_session = loaded
    cookie = request.cookies.get("gt_csrf")
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not secrets.compare_digest(cookie, header) or sha256(header) != app_session.csrf_sha256:
        raise api_error("CSRF_INVALID", "安全令牌已失效，请刷新页面。", 403)
    return loaded


def require_admin(loaded=Depends(require_session)) -> tuple[Workspace, AppSession]:
    if loaded[0].role != "admin":
        raise api_error("ADMIN_REQUIRED", "需要管理员登录。", 403)
    return loaded


def require_admin_csrf(request: Request, loaded=Depends(require_csrf)) -> tuple[Workspace, AppSession]:
    if loaded[0].role != "admin":
        raise api_error("ADMIN_REQUIRED", "需要管理员登录。", 403)
    return loaded


ADMIN_ATTEMPTS: dict[str, list[float]] = {}


def cleanup_once() -> dict:
    current = now_ms()
    with WRITE_LOCK, db_session() as session:
        soft = session.scalars(select(Workspace).where(Workspace.role == "guest", Workspace.status == "active", Workspace.expires_at_ms.is_not(None), Workspace.expires_at_ms <= current)).all()
        for workspace in soft:
            workspace.status = "expiring"
            runs = session.scalars(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id, AnalysisRun.status.in_(["queued", "waiting_approval"]))).all()
            for run in runs:
                run.status = "expired"
                run.revision += 1
            approvals = session.scalars(select(ApprovalRequest).where(ApprovalRequest.workspace_id == workspace.id, ApprovalRequest.status == "pending")).all()
            for approval in approvals:
                approval.status = "expired"
        hard = session.scalars(select(Workspace).where(Workspace.role == "guest", Workspace.hard_expires_at_ms.is_not(None), Workspace.hard_expires_at_ms <= current)).all()
        for workspace in hard:
            session.delete(workspace)
        session.commit()
        return {"soft_expired": len(soft), "hard_deleted": len(hard)}


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(30)
        cleanup_once()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.validate_startup()
    init_db()
    cleanup_once()
    for run_id, decided in recover_runs():
        schedule((lambda run_id=run_id: resume_after_approval(run_id)) if decided else (lambda run_id=run_id: execute_run(run_id)))
    cleanup = asyncio.create_task(cleanup_loop())
    yield
    cleanup.cancel()


app = FastAPI(title="GrowthTriage", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(RequestLimits)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc.lower() != request.headers.get("host", "").lower():
            return JSONResponse(status_code=403, content={"error": {"code": "ORIGIN_INVALID", "message": "请求来源不匹配。", "field_errors": []}, "request_id": f"req_{uuid.uuid4().hex}"})
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if request.url.path.startswith("/api/") or response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail), "field_errors": []}
    return JSONResponse(status_code=exc.status_code, content={"error": detail, "request_id": f"req_{uuid.uuid4().hex}"})


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    with db_session() as session:
        session.execute(select(func.count()).select_from(Workspace)).scalar_one()
    return {
        "status": "ready",
        "database": "ok",
        "runner_slots": 2,
        "model": "configured_unverified" if settings.deepseek_api_key else "fallback",
        "model_configured": bool(settings.deepseek_api_key),
        "cookie_secure": settings.cookie_secure,
    }


@app.post("/api/v1/sessions/guest")
def create_guest(request: Request, response: Response):
    current = _load_session(request)
    if current:
        workspace, app_session = current
        csrf = request.cookies.get("gt_csrf") or secrets.token_urlsafe(24)
    else:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        created = now_ms()
        expires = created + settings.guest_ttl_seconds * 1000
        workspace = Workspace(id=new_id("ws"), role="guest", status="active", created_at_ms=created, expires_at_ms=expires, hard_expires_at_ms=expires + settings.hard_grace_seconds * 1000, run_limit=3)
        app_session = AppSession(id=new_id("session"), workspace_id=workspace.id, token_sha256=sha256(token), csrf_sha256=sha256(csrf), created_at_ms=created, expires_at_ms=expires)
        with WRITE_LOCK, db_session() as session:
            session.add(workspace)
            session.flush()
            session.add(app_session)
            session.add(AuditEvent(id=new_id("audit"), workspace_id=workspace.id, event_type="workspace.guest_created", subject_hash=sha256(workspace.id)[:16], payload={}))
            session.commit()
        response.set_cookie("gt_session", token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.guest_ttl_seconds, path="/")
    response.set_cookie("gt_csrf", csrf, httponly=False, secure=settings.cookie_secure, samesite="lax", max_age=settings.guest_ttl_seconds, path="/")
    return envelope({"role": workspace.role, "workspace_id": workspace.id, "expires_at": utc_iso(workspace.expires_at_ms), "hard_expires_at": utc_iso(workspace.hard_expires_at_ms), "run_limit": workspace.run_limit, "csrf_token": csrf})


@app.post("/api/v1/admin/login")
def admin_login(body: AdminLoginIn, request: Request, response: Response):
    if not settings.admin_password_hash:
        raise api_error("ADMIN_NOT_CONFIGURED", "管理员入口未配置；访客体验仍可使用。", 503)
    address = request.client.host if request.client else "unknown"
    with WRITE_LOCK:
        recent = [stamp for stamp in ADMIN_ATTEMPTS.get(address, []) if time.monotonic() - stamp < 900]
        if len(recent) >= 5:
            raise api_error("ADMIN_RATE_LIMIT", "登录尝试过多，请稍后重试。", 429)
        if not verify_password(body.password, settings.admin_password_hash):
            ADMIN_ATTEMPTS[address] = recent + [time.monotonic()]
            raise api_error("ADMIN_LOGIN_FAILED", "密码错误。", 401)
        ADMIN_ATTEMPTS.pop(address, None)
        with db_session() as session:
            workspace = session.scalar(select(Workspace).where(Workspace.role == "admin"))
            if not workspace:
                workspace = Workspace(id=new_id("ws"), role="admin", status="active", expires_at_ms=None, hard_expires_at_ms=None, run_limit=1000)
                session.add(workspace)
                session.flush()
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            expires = now_ms() + 12 * 3600 * 1000
            session.add(AppSession(id=new_id("session"), workspace_id=workspace.id, token_sha256=sha256(token), csrf_sha256=sha256(csrf), expires_at_ms=expires))
            session.commit()
    response.set_cookie("gt_session", token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=12 * 3600, path="/")
    response.set_cookie("gt_csrf", csrf, httponly=False, secure=settings.cookie_secure, samesite="lax", max_age=12 * 3600, path="/")
    return envelope({"role": "admin", "workspace_id": workspace.id})


@app.get("/api/v1/session")
def get_session(loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run_count = session.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id))
        active = session.scalar(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id, AnalysisRun.status.in_(["queued", "running", "waiting_approval"])).order_by(AnalysisRun.created_at_ms.desc()))
    return envelope({"role": workspace.role, "workspace_id": workspace.id, "status": workspace.status, "expires_at": utc_iso(workspace.expires_at_ms) if workspace.expires_at_ms else None, "hard_expires_at": utc_iso(workspace.hard_expires_at_ms) if workspace.hard_expires_at_ms else None, "run_count": run_count, "run_limit": workspace.run_limit, "active_run_id": active.id if active else None})


@app.delete("/api/v1/session")
def delete_session(request: Request, response: Response, loaded=Depends(require_csrf)):
    workspace, _ = loaded
    with WRITE_LOCK, db_session() as session:
        if workspace.role == "admin":
            app_session = session.get(AppSession, loaded[1].id)
            app_session.revoked_at_ms = now_ms()
        else:
            target = session.get(Workspace, workspace.id)
            session.delete(target)
        session.commit()
    response.delete_cookie("gt_session", path="/")
    response.delete_cookie("gt_csrf", path="/")
    return envelope({"deleted": True})


@app.get("/api/v1/runs")
def list_runs(loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        runs = session.scalars(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id).order_by(AnalysisRun.created_at_ms.desc())).all()
        data = [{"run_id": r.id, "status": r.status, "execution_mode": r.execution_mode, "created_at": utc_iso(r.created_at_ms), "updated_at": utc_iso(r.updated_at_ms)} for r in runs]
    return envelope(data)


@app.get("/api/v1/templates/feedback.csv")
def feedback_template():
    return PlainTextResponse(FEEDBACK_TEMPLATE, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="feedback.csv"'})


@app.get("/api/v1/templates/ad_metrics.csv")
def metric_template():
    return PlainTextResponse(METRIC_TEMPLATE, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="ad_metrics.csv"'})


@app.post("/api/v1/datasets")
async def upload_dataset(feedback: UploadFile = File(...), ad_metrics: UploadFile = File(...), consent: bool = Form(...), loaded=Depends(require_csrf)):
    workspace, _ = loaded
    if not consent:
        raise api_error("CONSENT_REQUIRED", "请确认文件仅包含合成或匿名数据。", 422)
    if not (feedback.filename or "").lower().endswith(".csv") or not (ad_metrics.filename or "").lower().endswith(".csv"):
        raise api_error("FILE_INVALID", "请上传两个 CSV 文件。", 422)
    feedback_raw = await feedback.read(1_000_001)
    metrics_raw = await ad_metrics.read(2_000_001)
    await feedback.close()
    await ad_metrics.close()
    try:
        with WRITE_LOCK, db_session() as session:
            count = session.scalar(select(func.count()).select_from(Dataset).where(Dataset.workspace_id == workspace.id, Dataset.source == "upload"))
            if count >= 5:
                raise api_error("DATASET_LIMIT_REACHED", "当前工作区最多上传 5 组数据。", 409)
            dataset, warnings = store_dataset(session, workspace.id, feedback_raw, metrics_raw)
            session.commit()
        return envelope({"dataset_id": dataset.id, "status": "ready", "feedback_rows": dataset.feedback_rows, "metric_rows": dataset.metric_rows, "warnings": warnings})
    except UploadValidationError as exc:
        raise api_error("DATASET_BLOCKED", "CSV 质检未通过，请按行修复后重新上传。", 422, exc.issues)


@app.get("/api/v1/datasets/{dataset_id}")
def dataset_detail(dataset_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        dataset = session.scalar(select(Dataset).where(Dataset.id == dataset_id, Dataset.workspace_id == workspace.id))
        if not dataset:
            raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
        windows = session.scalars(select(AnalysisWindow).where(AnalysisWindow.dataset_id == dataset_id)).all()
        issues = session.scalars(select(DataQualityIssue).where(DataQualityIssue.dataset_id == dataset_id)).all()
        return envelope({"dataset_id": dataset.id, "source": dataset.source, "status": dataset.status, "feedback_rows": dataset.feedback_rows, "metric_rows": dataset.metric_rows, "sha256": dataset.sha256, "windows": [{"window": w.window_type, "start": w.start_date, "end": w.end_date} for w in windows], "issues": [{"severity": i.severity, "code": i.code, "message": i.message} for i in issues]})


@app.get("/api/v1/datasets/{dataset_id}/quality")
def dataset_quality(dataset_id: str, loaded=Depends(require_session)):
    return dataset_detail(dataset_id, loaded)


@app.post("/api/v1/datasets/{dataset_id}/runs")
async def create_uploaded_run(dataset_id: str, loaded=Depends(require_csrf), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    workspace, _ = loaded
    key = idempotency_key or str(uuid.uuid4())
    with WRITE_LOCK, db_session() as session:
        dataset = session.scalar(select(Dataset).where(Dataset.id == dataset_id, Dataset.workspace_id == workspace.id, Dataset.status == "ready"))
        if not dataset:
            raise api_error("RESOURCE_NOT_FOUND", "已校验的数据集不存在。", 404)
        existing = session.scalar(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id, AnalysisRun.idempotency_key == key))
        request_hash = sha256(f"dataset:{dataset_id}:{dataset.sha256}")
        if existing:
            if existing.request_sha256 != request_hash:
                raise api_error("IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求。", 409)
            return envelope({"run_id": existing.id, "status": existing.status})
        active = session.scalar(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id, AnalysisRun.status.in_(["queued", "running", "waiting_approval"])))
        if active:
            raise api_error("ACTIVE_RUN_EXISTS", "当前工作区已有活动诊断。", 409, [{"run_id": active.id}])
        count = session.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id))
        if count >= workspace.run_limit:
            raise api_error("RUN_LIMIT_REACHED", "当前工作区的诊断次数已用完。", 409)
        run = AnalysisRun(id=new_id("run"), workspace_id=workspace.id, dataset_id=dataset_id, status="queued", execution_mode="fallback", current_phase="queued", idempotency_key=key, request_sha256=request_hash, expires_at_ms=workspace.expires_at_ms, hard_expires_at_ms=workspace.hard_expires_at_ms)
        session.add(run)
        session.flush()
        create_steps(session, run)
        session.add(AuditEvent(id=new_id("audit"), workspace_id=workspace.id, run_id=run.id, event_type="run.created", subject_hash=sha256(workspace.id)[:16], payload={"source": "upload"}))
        session.commit()
    schedule(lambda: execute_run(run.id))
    return envelope({"run_id": run.id, "status": "queued"})


@app.post("/api/v1/runs/golden")
async def create_golden_run(request: Request, loaded=Depends(require_csrf), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    workspace, _ = loaded
    key = idempotency_key or str(uuid.uuid4())
    request_hash = sha256("golden:v1")
    with WRITE_LOCK, db_session() as session:
        existing = session.scalar(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id, AnalysisRun.idempotency_key == key))
        if existing:
            if existing.request_sha256 != request_hash:
                raise api_error("IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求。", 409)
            return envelope({"run_id": existing.id, "status": existing.status})
        active = session.scalar(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id, AnalysisRun.status.in_(["queued", "running", "waiting_approval"])))
        if active:
            raise api_error("ACTIVE_RUN_EXISTS", "当前工作区已有活动 Run。", 409, [{"run_id": active.id}])
        count = session.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.workspace_id == workspace.id))
        if count >= workspace.run_limit:
            raise api_error("RUN_LIMIT_REACHED", "本次访客工作区已达到 3 个 Run 上限。", 409)
        dataset = create_golden_dataset(session, workspace.id)
        run = AnalysisRun(id=new_id("run"), workspace_id=workspace.id, dataset_id=dataset.id, status="queued", execution_mode="fallback", current_phase="queued", idempotency_key=key, request_sha256=request_hash, expires_at_ms=workspace.expires_at_ms, hard_expires_at_ms=workspace.hard_expires_at_ms)
        session.add(run)
        session.flush()
        create_steps(session, run)
        session.add(AuditEvent(id=new_id("audit"), workspace_id=workspace.id, run_id=run.id, event_type="run.created", subject_hash=sha256(workspace.id)[:16], payload={"source": "golden"}))
        session.commit()
    schedule(lambda: execute_run(run.id))
    return envelope({"run_id": run.id, "status": "queued"})


def _authorized_run(session, workspace_id: str, run_id: str) -> AnalysisRun:
    run = session.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id, AnalysisRun.workspace_id == workspace_id))
    if not run:
        raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
    return run


@app.get("/api/v1/runs/{run_id}/status")
def run_status(run_id: str, request: Request, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)).all()
        active = [s.step_key for s in steps if s.status == "running"]
        approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id, ApprovalRequest.status == "pending"))
        data = {"run_id": run.id, "status": run.status, "execution_mode": run.execution_mode, "error_code": run.error_code, "current_phase": run.current_phase, "active_step_keys": active, "completed_steps": run.completed_steps, "total_steps": run.total_steps, "pending_approval_id": approval.id if approval else None, "revision": run.revision, "updated_at": utc_iso(run.updated_at_ms), "poll_after_ms": 1500}
    etag = f'W/"run:{run_id}:{data["revision"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=envelope(data), headers={"ETag": etag})


def _result_data(session, run: AnalysisRun) -> dict:
    dataset = session.get(Dataset, run.dataset_id)
    snapshot_artifact = session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run.id, AgentArtifact.step_key == "snapshot", AgentArtifact.is_current.is_(True)))
    feedback_artifact = session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run.id, AgentArtifact.step_key == "feedback", AgentArtifact.is_current.is_(True)))
    correlation_artifact = session.scalar(select(AgentArtifact).where(AgentArtifact.run_id == run.id, AgentArtifact.step_key == "correlation", AgentArtifact.is_current.is_(True)))
    finding = session.scalar(select(Finding).where(Finding.run_id == run.id))
    evidence = session.scalars(select(EvidenceRef).where(EvidenceRef.run_id == run.id)).all()
    actions = session.scalars(select(ActionPlan).where(ActionPlan.run_id == run.id)).all()
    experiments = session.scalars(select(ExperimentCard).where(ExperimentCard.run_id == run.id)).all()
    approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id))
    ticket = session.scalar(select(SandboxTicket).where(SandboxTicket.run_id == run.id))
    report = session.scalar(select(ReportBundle).where(ReportBundle.run_id == run.id))
    return {
        "run_id": run.id,
        "status": run.status,
        "execution_mode": run.execution_mode,
        "fixture": manifest() if dataset.source == "golden" else None,
        "source": dataset.source,
        "scope": None if not snapshot_artifact else {key: snapshot_artifact.payload["target"][key] for key in ("market", "platform", "campaign_id", "creative_id")},
        "metrics": None if not snapshot_artifact else snapshot_artifact.payload["target"]["metrics"],
        "raw_metrics": None if not snapshot_artifact else snapshot_artifact.payload["target"]["raw"],
        "feedback": None if not feedback_artifact else feedback_artifact.payload,
        "correlation": None if not correlation_artifact else correlation_artifact.payload,
        "finding": None if not finding else {"id": finding.id, "title": finding.title, "claim": finding.claim, "incident_priority": finding.incident_priority, "confidence": finding.confidence, "link_strength": finding.link_strength, "causality_status": finding.causality_status},
        "evidence": [{"id": e.id, "type": e.evidence_type, "label": e.label, "value": e.value} for e in evidence],
        "actions": [{"title": a.title, "risk_level": a.risk_level, "status": a.status, "executed": a.executed, "details": a.details} for a in actions],
        "experiments": [{"title": e.title, "primary_metric": e.primary_metric, "guardrails": e.guardrails, "duration_days": e.duration_days, "validation_goal": e.validation_goal, "stop_condition": e.stop_condition} for e in experiments],
        "approval": None if not approval else {"id": approval.id, "status": approval.status, "expires_at": utc_iso(approval.expires_at_ms), "decided_at": utc_iso(approval.decided_at_ms) if approval.decided_at_ms else None},
        "ticket": None if not ticket else {"ticket_no": ticket.ticket_no, "title": ticket.title, "status": ticket.status},
        "report_ready": bool(report and report.redaction_passed),
        "external_writes": 0,
    }


@app.get("/api/v1/runs/{run_id}/results")
def run_results(run_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        data = _result_data(session, run)
    return envelope(data)


@app.get("/api/v1/runs/{run_id}/trace")
def run_trace(run_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)).all()
        tools = session.scalars(select(ToolCall).where(ToolCall.run_id == run.id)).all()
        audits = session.scalars(select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.created_at_ms)).all()
        data = {
            "steps": [{"step_key": s.step_key, "label": s.label, "status": s.status, "execution_mode": s.execution_mode, "actual_model": s.actual_model, "attempt_count": s.attempt_count, "latency_ms": s.latency_ms, "error_code": s.error_code} for s in steps],
            "artifacts": [{"id": a.id, "step_key": a.step_key, "artifact_type": a.artifact_type, "payload": a.payload} for a in session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run.id)).all()],
            "tools": [{"tool_name": t.tool_name, "risk_level": t.risk_level, "status": t.status, "executed": t.executed} for t in tools],
            "events": [{"event_type": a.event_type, "created_at": utc_iso(a.created_at_ms), "payload": a.payload} for a in audits],
        }
    return envelope(data)


@app.get("/api/v1/findings/{finding_id}/evidence")
def finding_evidence(finding_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        finding = session.scalar(select(Finding).where(Finding.id == finding_id, Finding.workspace_id == workspace.id))
        if not finding:
            raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
        evidence = session.scalars(select(EvidenceRef).where(EvidenceRef.finding_id == finding.id, EvidenceRef.workspace_id == workspace.id)).all()
        return envelope({"finding_id": finding.id, "items": [{"id": item.id, "type": item.evidence_type, "target_id": item.target_id, "label": item.label, "value": item.value} for item in evidence]})


@app.get("/api/v1/artifacts/{artifact_id}")
def artifact_detail(artifact_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        artifact = session.scalar(select(AgentArtifact).where(AgentArtifact.id == artifact_id, AgentArtifact.workspace_id == workspace.id))
        if not artifact:
            raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
        return envelope({"id": artifact.id, "run_id": artifact.run_id, "step_key": artifact.step_key, "artifact_type": artifact.artifact_type, "schema_version": artifact.schema_version, "payload": artifact.payload})


@app.get("/api/v1/approvals")
def list_approvals(loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        approvals = session.scalars(select(ApprovalRequest).where(ApprovalRequest.workspace_id == workspace.id).order_by(ApprovalRequest.requested_at_ms.desc())).all()
        return envelope([{"approval_id": item.id, "run_id": item.run_id, "status": item.status, "expires_at": utc_iso(item.expires_at_ms)} for item in approvals])


@app.get("/api/v1/approvals/{approval_id}")
def approval_detail(approval_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        approval = session.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id, ApprovalRequest.workspace_id == workspace.id))
        if not approval:
            raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
        run = session.get(AnalysisRun, approval.run_id)
        decision = session.scalar(select(ToolCall).where(ToolCall.id == approval.tool_call_id))
        finding = session.scalar(select(Finding).where(Finding.run_id == run.id))
        data = {"approval_id": approval.id, "run_id": run.id, "status": approval.status, "requested_at": utc_iso(approval.requested_at_ms), "expires_at": utc_iso(approval.expires_at_ms), "tool": decision.tool_name, "risk_level": decision.risk_level, "title": f"创建 {finding.title} 内部工单", "incident_priority": finding.incident_priority, "policy": {"decision": "require_approval", "matched_rule": "R2_SANDBOX_WRITE", "version": "policy-1.0"}, "safety": ["不会连接真实广告平台", "不会调整预算、出价、停投或扩量", "不会创建外部工单", "决定会写入 AuditEvent"]}
    return envelope(data)


@app.post("/api/v1/approvals/{approval_id}/decision")
async def approval_decision(approval_id: str, body: ApprovalDecisionIn, loaded=Depends(require_csrf)):
    workspace, _ = loaded
    try:
        approval, ticket, should_resume = decide_approval(approval_id, workspace.id, body.decision, body.decision_key, body.comment)
    except KeyError:
        raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
    except TimeoutError:
        raise api_error("APPROVAL_EXPIRED", "审批已过期，无法再修改。", 410)
    except ValueError:
        raise api_error("APPROVAL_ALREADY_DECIDED", "该审批已在另一页面完成。", 409)
    if should_resume:
        schedule(lambda: resume_after_approval(approval.run_id))
    return envelope({"approval_id": approval.id, "status": approval.status, "run_id": approval.run_id, "run_status": "queued", "ticket": None if not ticket else {"ticket_no": ticket.ticket_no, "title": ticket.title, "status": ticket.status}})


@app.get("/api/v1/runs/{run_id}/ticket")
def run_ticket(run_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        ticket = session.scalar(select(SandboxTicket).where(SandboxTicket.run_id == run.id, SandboxTicket.workspace_id == workspace.id))
        if not ticket:
            raise api_error("RESOURCE_NOT_FOUND", "资源不存在。", 404)
        return envelope({"ticket_no": ticket.ticket_no, "title": ticket.title, "status": ticket.status, "approval_id": ticket.approval_id})


@app.post("/api/v1/runs/{run_id}/retry-report")
async def retry_report(run_id: str, loaded=Depends(require_csrf)):
    from .services.agent_graph import queue_report_retry, finish
    from .services.agent_runtime import AgentError
    workspace, _ = loaded
    try:
        queued = queue_report_retry(run_id, workspace.id)
    except AgentError as exc:
        raise api_error(exc.code, "此任务暂不能重试报告，请检查任务状态。", 404 if exc.code == "RESOURCE_NOT_FOUND" else 409)
    if queued:
        schedule(lambda: finish(run_id))
    return envelope({"run_id": run_id, "queued": queued})


@app.get("/api/v1/runs/{run_id}/report")
def report_json(run_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        report = session.scalar(select(ReportBundle).where(ReportBundle.run_id == run.id))
        if not report:
            raise api_error("REPORT_NOT_READY", "报告仍在生成。", 409)
        if not report.redaction_passed:
            raise api_error("REPORT_REDACTION_FAILED", "报告脱敏检查未通过。", 409)
        return envelope(report.payload)


@app.get("/api/v1/runs/{run_id}/report.md")
def report_markdown(run_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        report = session.scalar(select(ReportBundle).where(ReportBundle.run_id == run.id))
        if not report or not report.redaction_passed:
            raise api_error("REPORT_NOT_READY", "报告尚不可下载。", 409)
        return PlainTextResponse(report.markdown, media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="growthtriage-{run.id}.md"'})


@app.get("/api/v1/runs/{run_id}/report.json")
def report_download_json(run_id: str, loaded=Depends(require_session)):
    workspace, _ = loaded
    with db_session() as session:
        run = _authorized_run(session, workspace.id, run_id)
        report = session.scalar(select(ReportBundle).where(ReportBundle.run_id == run.id))
        if not report or not report.redaction_passed:
            raise api_error("REPORT_NOT_READY", "报告尚不可下载。", 409)
        return JSONResponse(report.payload, headers={"Content-Disposition": f'attachment; filename="growthtriage-{run.id}.json"'})


@app.get("/api/v1/admin/health")
def admin_health(loaded=Depends(require_admin)):
    with db_session() as session:
        workspaces = session.scalar(select(func.count()).select_from(Workspace).where(Workspace.role == "guest", Workspace.status == "active"))
        runs = session.scalar(select(func.count()).select_from(AnalysisRun))
        active = session.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.status.in_(["queued", "running", "waiting_approval"])))
        day = datetime.now(timezone.utc).date().isoformat()
        from .db import DailyLiveQuota
        quota = session.get(DailyLiveQuota, day)
    return envelope({"database": "ok", "runner": "online", "model_configured": bool(settings.deepseek_api_key), "model": settings.deepseek_model if settings.deepseek_api_key else None, "guest_workspaces": workspaces, "runs": runs, "active_runs": active, "live_quota_used": quota.reserved_count if quota else 0, "live_quota_limit": settings.live_daily_limit, "tunnel": "由本地 cloudflared 进程单独管理"})


@app.post("/api/v1/admin/model/probe")
async def admin_model_probe(loaded=Depends(require_admin_csrf)):
    return envelope(await probe_model(reserve_attempt=reserve_probe_attempt))


@app.get("/api/v1/admin/workspaces")
def admin_workspaces(loaded=Depends(require_admin)):
    with db_session() as session:
        workspaces = session.scalars(select(Workspace).where(Workspace.role == "guest").order_by(Workspace.created_at_ms.desc()).limit(100)).all()
        return envelope([{"workspace_id": item.id, "status": item.status, "created_at": utc_iso(item.created_at_ms), "expires_at": utc_iso(item.expires_at_ms) if item.expires_at_ms else None} for item in workspaces])


@app.get("/api/v1/admin/runs")
def admin_runs(loaded=Depends(require_admin)):
    with db_session() as session:
        runs = session.scalars(select(AnalysisRun).order_by(AnalysisRun.created_at_ms.desc()).limit(100)).all()
        return envelope([{"run_id": item.id, "workspace_id": item.workspace_id, "status": item.status, "execution_mode": item.execution_mode, "error_code": item.error_code, "created_at": utc_iso(item.created_at_ms)} for item in runs])


@app.delete("/api/v1/admin/workspaces/{workspace_id}")
def admin_delete_workspace(workspace_id: str, confirm_workspace_id: str | None = Header(default=None, alias="X-Confirm-Workspace-Id"), loaded=Depends(require_admin_csrf)):
    if confirm_workspace_id != workspace_id:
        raise api_error("CONFIRMATION_REQUIRED", "需再次确认完整的 Workspace ID。", 422)
    with WRITE_LOCK, db_session() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.id == workspace_id, Workspace.role == "guest"))
        if not workspace:
            raise api_error("RESOURCE_NOT_FOUND", "访客工作区不存在。", 404)
        session.add(AuditEvent(id=new_id("audit"), workspace_id=None, run_id=None, event_type="admin.workspace_deleted", subject_hash=sha256(workspace_id)[:16], payload={}))
        session.delete(workspace)
        session.commit()
    return envelope({"deleted": True})


@app.post("/api/v1/admin/cleanup")
def admin_cleanup(loaded=Depends(require_admin_csrf)):
    return envelope(cleanup_once())


@app.post("/api/v1/admin/logout")
def admin_logout(request: Request, response: Response, loaded=Depends(require_admin_csrf)):
    return delete_session(request, response, loaded)


dist = settings.frontend_dist
if dist.exists():
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        candidate = (dist / path).resolve()
        if path and candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
else:
    @app.get("/")
    def root():
        return {"name": "GrowthTriage", "status": "frontend_not_built", "health": "/health/ready"}
