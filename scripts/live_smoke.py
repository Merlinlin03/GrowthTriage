"""Opt-in live-model smoke test. Uses synthetic CSVs and never prints credentials."""

from __future__ import annotations

import argparse
import os
import time
import uuid
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def data(response: httpx.Response) -> dict:
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(body["error"]["code"])
    return body["data"]


def run(base_url: str, timeout_seconds: int) -> None:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=15.0, follow_redirects=False) as guest:
        ready = guest.get("/health/ready")
        ready.raise_for_status()
        readiness = ready.json()
        if readiness.get("cookie_secure") and not base_url.startswith("https://"):
            raise RuntimeError("SECURE_COOKIE_REQUIRES_HTTPS_SMOKE_URL")
        if not readiness.get("model_configured"):
            raise RuntimeError("LLM_NOT_CONFIGURED")

        admin_password = os.getenv("GT_ADMIN_PASSWORD")
        if admin_password:
            with httpx.Client(base_url=base_url.rstrip("/"), timeout=15.0) as admin:
                data(admin.post("/api/v1/admin/login", json={"password": admin_password}))
                probe = data(admin.post("/api/v1/admin/model/probe", headers={"X-CSRF-Token": admin.cookies["gt_csrf"]}))
                if not probe["reachable"]:
                    raise RuntimeError(probe["error_code"] or "LLM_PROVIDER_ERROR")
                print(f"Model probe passed: {probe['actual_model']} ({probe['latency_ms']} ms)")

        csrf = data(guest.post("/api/v1/sessions/guest"))["csrf_token"]
        feedback = (ROOT / "examples" / "feedback.csv").read_bytes()
        metrics = (ROOT / "examples" / "ad_metrics.csv").read_bytes()
        uploaded = data(guest.post(
            "/api/v1/datasets",
            headers={"X-CSRF-Token": csrf},
            data={"consent": "true"},
            files={"feedback": ("feedback.csv", feedback, "text/csv"), "ad_metrics": ("ad_metrics.csv", metrics, "text/csv")},
        ))
        created = data(guest.post(
            f"/api/v1/datasets/{uploaded['dataset_id']}/runs",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())},
        ))
        run_id = created["run_id"]
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = data(guest.get(f"/api/v1/runs/{run_id}/status"))
            if status["status"] == "failed":
                raise RuntimeError(status.get("error_code") or "RUN_FAILED")
            if status["status"] == "waiting_approval":
                data(guest.post(
                    f"/api/v1/approvals/{status['pending_approval_id']}/decision",
                    headers={"X-CSRF-Token": csrf},
                    json={"decision": "rejected", "decision_key": str(uuid.uuid4()), "comment": "Live smoke: no sandbox write"},
                ))
            elif status["status"] == "succeeded":
                break
            time.sleep(1.5)
        else:
            raise TimeoutError(f"Run {run_id} did not finish within {timeout_seconds}s")

        trace = data(guest.get(f"/api/v1/runs/{run_id}/trace"))
        feedback_step = next(step for step in trace["steps"] if step["step_key"] == "feedback")
        agent_steps = [step for step in trace["steps"] if step["step_key"] in {"feedback", "performance", "correlation", "planner", "auditor", "report_builder"}]
        if len(agent_steps) != 6 or any(step["execution_mode"] != "live" or not step["actual_model"] for step in agent_steps):
            raise RuntimeError("LLM_NOT_LIVE")
        report = data(guest.get(f"/api/v1/runs/{run_id}/report"))
        if report["source"] != "upload":
            raise RuntimeError("REPORT_SOURCE_INVALID")
        if report.get("agent_review", {}).get("verdict") != "passed":
            raise RuntimeError("FINAL_REVIEW_NOT_PASSED")
        print(f"Live smoke passed: Run {run_id}, model {feedback_step['actual_model']}, report ready")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an uploaded-CSV analysis against a configured GrowthTriage service.")
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8088")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    run(args.base_url, args.timeout)
