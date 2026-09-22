"""Exercise a disposable local container with synthetic data and no model key."""
import argparse
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]


def data(response):
    response.raise_for_status()
    return response.json()["data"]


def ready(client):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            response = client.get("/health/ready")
            if response.status_code == 200:
                assert response.json()["model_configured"] is False
                assert response.json()["cookie_secure"] is False
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise TimeoutError("CONTAINER_NOT_READY")


def wait(client, run_id, wanted):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = data(client.get(f"/api/v1/runs/{run_id}/status"))
        assert status["status"] not in {"failed", "expired"}, status.get("error_code")
        if status["status"] == wanted:
            return status
        time.sleep(0.2)
    raise TimeoutError("RUN_NOT_READY")


def restart(name, client):
    if name:
        subprocess.run(["docker", "restart", "--time", "5", name], check=True, capture_output=True, timeout=30)
        ready(client)


def backup_restore(container, run_id):
    token = uuid.uuid4().hex
    script = f"/tmp/backup-tool-{token}.py"
    snapshot, restored = f"/tmp/snapshot-{token}.db", f"/tmp/restored-{token}.db"
    subprocess.run(["docker", "cp", str(ROOT / "scripts/db_backup.py"), f"{container}:{script}"], check=True, capture_output=True, timeout=30)
    for operation, source, target in [("backup", "/app/data/growthtriage.db", snapshot), ("restore", snapshot, restored)]:
        subprocess.run(["docker", "exec", container, "python", script, operation, "--source", source,
                        "--destination", target], check=True, capture_output=True, timeout=30)
    verification = (
        "import sqlite3,sys,json; c=sqlite3.connect(sys.argv[1]); r=sys.argv[2]; "
        "assert c.execute('SELECT status FROM analysis_run WHERE id=?',(r,)).fetchone()==('succeeded',); "
        "assert c.execute('SELECT count(*) FROM sandbox_ticket WHERE run_id=?',(r,)).fetchone()==(1,); "
        "p=json.loads(c.execute('SELECT payload FROM report_bundle WHERE run_id=?',(r,)).fetchone()[0]); "
        "assert p['external_writes']==0 and p['ticket'] is not None; "
        "assert not c.execute('PRAGMA foreign_key_check').fetchall(); c.close()"
    )
    subprocess.run(["docker", "exec", container, "python", "-c", verification, restored, run_id],
                   check=True, capture_output=True, timeout=30)
    print("PASS: running container database backup and restore to a new file")


def run(base_url, container):
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
        raise ValueError("LOCAL_HTTP_URL_REQUIRED")
    for source, decision in [("golden", "approved"), ("upload", "rejected")]:
        with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:
            ready(client)
            page = client.get("/")
            page.raise_for_status()
            asset = re.search(r'"(/assets/[^\"]+\.js)"', page.text)
            assert asset and client.get(asset.group(1)).status_code == 200
            csrf = data(client.post("/api/v1/sessions/guest"))["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())}
            try:
                endpoint = "/api/v1/runs/golden"
                if source == "upload":
                    uploaded = data(client.post("/api/v1/datasets", headers=headers, data={"consent": "true"}, files={
                        "feedback": ("feedback.csv", (ROOT / "examples/feedback.csv").read_bytes(), "text/csv"),
                        "ad_metrics": ("ad_metrics.csv", (ROOT / "examples/ad_metrics.csv").read_bytes(), "text/csv"),
                    }))
                    endpoint = f"/api/v1/datasets/{uploaded['dataset_id']}/runs"
                run_id = data(client.post(endpoint, headers=headers))["run_id"]
                pending = wait(client, run_id, "waiting_approval")
                if source == "golden":
                    restart(container, client)
                    assert wait(client, run_id, "waiting_approval")["pending_approval_id"] == pending["pending_approval_id"]
                approval_url = f"/api/v1/approvals/{pending['pending_approval_id']}/decision"
                body = {"decision": decision, "decision_key": str(uuid.uuid4()), "comment": "container acceptance"}
                data(client.post(approval_url, headers=headers, json=body))
                data(client.post(approval_url, headers=headers, json=body))
                assert wait(client, run_id, "succeeded")["completed_steps"] == 9
                report_url = f"/api/v1/runs/{run_id}/report"
                report = data(client.get(report_url))
                assert report["external_writes"] == 0 and report["source"] == source
                assert (report["ticket"] is not None) == (decision == "approved")
                assert all(not action["executed"] for action in report["actions"] if action["risk_level"] == "R3")
                trace = data(client.get(f"/api/v1/runs/{run_id}/trace"))
                assert len(trace["steps"]) == 9
                for extension in ("md", "json"):
                    assert client.get(report_url + "." + extension).status_code == 200
                if source == "golden":
                    restart(container, client)
                    assert data(client.get(report_url)) == report
                    if container:
                        backup_restore(container, run_id)
                with httpx.Client(base_url=base_url, trust_env=False) as stranger:
                    stranger_csrf = data(stranger.post("/api/v1/sessions/guest"))["csrf_token"]
                    try:
                        assert stranger.get(report_url).status_code == 404
                    finally:
                        stranger.delete("/api/v1/session", headers={"X-CSRF-Token": stranger_csrf}).raise_for_status()
                assert client.post("/api/v1/runs/golden").status_code == 403
                assert client.post("/api/v1/sessions/guest", headers={"Origin": "https://invalid.example"}).status_code == 403
                oversized = client.post("/api/v1/datasets", content=b"x" * 3_100_001)
                assert oversized.status_code == 413 and oversized.headers["X-Content-Type-Options"] == "nosniff"
                print(f"PASS: {source}, {decision}, trace, downloads, isolation, request boundaries")
            finally:
                client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf}).raise_for_status()
    print("PASS: container smoke" + (", restart persistence" if container else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--restart-container")
    args = parser.parse_args()
    run(args.base_url, args.restart_container)
