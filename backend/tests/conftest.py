from __future__ import annotations

import os
import tempfile
from pathlib import Path


# Configure the database before importing app.main in test modules.
_test_dir = Path(tempfile.mkdtemp(prefix="growthtriage-tests-"))
os.environ["GT_DATABASE_URL"] = f"sqlite:///{(_test_dir / 'suite.db').as_posix()}"
os.environ["GT_APP_MODE"] = "local"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["GT_ADMIN_PASSWORD_HASH"] = ""
os.environ["GT_COOKIE_SECURE"] = "false"

import pytest


@pytest.fixture(autouse=True)
def no_real_agent_provider(monkeypatch):
    from app.services import agent_runtime

    async def blocked(*args, **kwargs):
        raise AssertionError("Tests must explicitly fake the agent provider")

    monkeypatch.setattr(agent_runtime, "request", blocked)
