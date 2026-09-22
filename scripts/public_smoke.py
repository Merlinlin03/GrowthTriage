"""Run a disposable Golden Case through a temporary HTTPS demo URL."""

from __future__ import annotations

import sys
import time
import uuid
import re
from urllib.parse import urlparse

import httpx


def main(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".trycloudflare.com"):
        raise SystemExit("Expected a temporary https://*.trycloudflare.com URL")
    base_url = base_url.rstrip("/")
    with httpx.Client(base_url=base_url, timeout=15, follow_redirects=False) as client:
        ready = client.get("/health/ready")
        ready.raise_for_status()
        assert ready.json()["cookie_secure"] is True
        page = client.get("/")
        page.raise_for_status()
        assert "GrowthTriage" in page.text
        asset = re.search(r'"(/assets/[^\"]+\.js)"', page.text)
        assert asset is not None
        client.get(asset.group(1)).raise_for_status()
        csrf = None
        try:
            guest = client.post("/api/v1/sessions/guest", headers={"Origin": base_url})
            guest.raise_for_status()
            assert all("secure" in value.lower() for value in guest.headers.get_list("set-cookie"))
            csrf = guest.json()["data"]["csrf_token"]
            created = client.post("/api/v1/runs/golden", headers={"Origin": base_url, "X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
            created.raise_for_status()
            run_id = created.json()["data"]["run_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = client.get(f"/api/v1/runs/{run_id}/status").json()["data"]
                if status["status"] == "waiting_approval":
                    break
                if status["status"] in {"failed", "expired"}:
                    raise RuntimeError(f"Golden Run ended as {status['status']}")
                time.sleep(0.25)
            else:
                raise TimeoutError("Golden Run did not reach approval")
            decision = client.post(
                f"/api/v1/approvals/{status['pending_approval_id']}/decision",
                headers={"Origin": base_url, "X-CSRF-Token": csrf},
                json={"decision": "approved", "decision_key": str(uuid.uuid4()), "comment": "public smoke test"},
            )
            decision.raise_for_status()
            while time.monotonic() < deadline:
                status = client.get(f"/api/v1/runs/{run_id}/status").json()["data"]
                if status["status"] == "succeeded":
                    break
                if status["status"] in {"failed", "expired"}:
                    raise RuntimeError(f"Golden Run ended as {status['status']}")
                time.sleep(0.25)
            else:
                raise TimeoutError("Golden Run did not complete")
            report = client.get(f"/api/v1/runs/{run_id}/report")
            report.raise_for_status()
            payload = report.json()["data"]
            assert payload["ticket"] is not None
            assert payload["external_writes"] == 0
            assert all(not item["executed"] for item in payload["actions"] if item["risk_level"] == "R3")
            client.get(f"/api/v1/runs/{run_id}/report.md").raise_for_status()
            print("PASS: HTTPS page/assets, guest, Golden, R2 approval, report; Secure cookies; no external writes")
        finally:
            if csrf:
                deleted = client.delete("/api/v1/session", headers={"Origin": base_url, "X-CSRF-Token": csrf})
                deleted.raise_for_status()
                print("Disposable smoke-test workspace deleted")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/public_smoke.py https://<name>.trycloudflare.com")
    main(sys.argv[1])
