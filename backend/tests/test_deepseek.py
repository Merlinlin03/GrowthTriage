from __future__ import annotations

import asyncio
from dataclasses import replace
import httpx
import pytest

from app.config import settings
from app.services import deepseek


class FakeResponse:
    def __init__(self, content: str, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def json(self):
        return {"model": "deepseek-chat", "choices": [{"message": {"content": self.content}}]}


class FakeClient:
    def __init__(self, replies):
        self.replies = replies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            return FakeResponse(reply[1], reply[0])
        return FakeResponse(reply)


def test_model_json_is_consumed_and_invalid_schema_falls_back(monkeypatch):
    monkeypatch.setattr(deepseek, "settings", replace(settings, deepseek_api_key="test-only", deepseek_base_url="https://example.invalid"))
    rows = [{"id": "fb-1", "text": "I see this ad too often"}]
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", lambda **_kwargs: FakeClient(['{"labels":{"fb-1":"repeat_exposure"}}']))
    mode, model, _latency, error, labels = asyncio.run(deepseek.classify_feedback(rows))
    assert (mode, model, error, labels) == ("live", "deepseek-chat", None, {"fb-1": "repeat_exposure"})
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", lambda **_kwargs: FakeClient(['{"labels":{}}', '{"labels":{"fb-1":"invented"}}']))
    mode, _model, _latency, error, labels = asyncio.run(deepseek.classify_feedback(rows))
    assert (mode, error, labels) == ("fallback", "LLM_SCHEMA_INVALID", {})


@pytest.mark.parametrize("reply,expected", [((401, "{}"), "LLM_HTTP_401"), ((429, "{}"), "LLM_HTTP_429"), ((503, "{}"), "LLM_HTTP_503"), (httpx.TimeoutException("timeout"), "LLM_TIMEOUT")])
def test_provider_failures_are_safe(monkeypatch, reply, expected):
    monkeypatch.setattr(deepseek, "settings", replace(settings, deepseek_api_key="private-test-key", deepseek_base_url="https://example.invalid"))
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", lambda **_kwargs: FakeClient([reply]))
    mode, model, _latency, error, labels = asyncio.run(deepseek.classify_feedback([{"id": "fb-1", "text": "Too often"}]))
    assert (mode, model, error, labels) == ("fallback", None, expected, {})
    assert "private-test-key" not in str((mode, model, error, labels))


def test_batch_limit_and_request_reservation(monkeypatch):
    monkeypatch.setattr(deepseek, "settings", replace(settings, deepseek_api_key="test-only", deepseek_base_url="https://example.invalid"))
    rows = [{"id": f"fb-{index}", "text": "Too often"} for index in range(51)]
    assert asyncio.run(deepseek.classify_feedback(rows))[3] == "LLM_BATCH_TOO_LARGE"
    attempts = []
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", lambda **_kwargs: FakeClient(['{"labels":{}}', '{"labels":{"fb-0":"repeat_exposure"}}']))
    result = asyncio.run(deepseek.classify_feedback(rows[:1], reserve_attempt=lambda: attempts.append(1) or True))
    assert result[0] == "live"
    assert len(attempts) == 2


def test_probe_reserves_a_request_and_respects_limit(monkeypatch):
    monkeypatch.setattr(deepseek, "settings", replace(settings, deepseek_api_key="test-only", deepseek_base_url="https://example.invalid"))
    calls = []
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", lambda **_kwargs: FakeClient(['{"labels":{"probe":"repeat_exposure"}}']))
    probe = asyncio.run(deepseek.probe_model(reserve_attempt=lambda: calls.append(1) or True))
    assert probe["reachable"] is True
    assert len(calls) == 1
    limited = asyncio.run(deepseek.probe_model(reserve_attempt=lambda: False))
    assert limited["reachable"] is False
    assert limited["error_code"] == "LLM_DAILY_LIMIT"


def test_production_configuration_requires_key_and_https():
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        replace(settings, app_mode="production", deepseek_api_key=None).validate_startup()
    with pytest.raises(RuntimeError, match="HTTPS"):
        replace(settings, app_mode="production", deepseek_api_key="test-only", deepseek_base_url="http://example.invalid").validate_startup()
    replace(settings, app_mode="production", deepseek_api_key="test-only", deepseek_base_url="https://example.invalid").validate_startup()


def test_forty_feedback_rows_use_longer_bounded_timeout(monkeypatch):
    import json
    rows = [{"id": f"fb-{index}", "text": "This ad appears too often"} for index in range(40)]
    expected = {row["id"]: "repeat_exposure" for row in rows}
    observed = {}

    def client_factory(**kwargs):
        observed.update(kwargs)
        return FakeClient([json.dumps({"labels": expected})])

    monkeypatch.setattr(deepseek, "settings", replace(settings, deepseek_api_key="test-only", llm_request_timeout_seconds=60))
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", client_factory)
    result = asyncio.run(deepseek.classify_feedback(rows))
    assert result[0] == "live" and result[4] == expected
    assert observed["timeout"].read == 60
    assert observed["timeout"].connect == 10


def test_total_request_deadline_cancels_without_retry(monkeypatch):
    attempts = []
    cancelled = []

    class SlowClient(FakeClient):
        async def post(self, *_args, **_kwargs):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.append(True)

    monkeypatch.setattr(deepseek, "settings", replace(settings, deepseek_api_key="test-only", llm_request_timeout_seconds=0.01))
    monkeypatch.setattr(deepseek.httpx, "AsyncClient", lambda **_kwargs: SlowClient([]))
    result = asyncio.run(deepseek.classify_feedback([{"id": "fb-1", "text": "Too often"}],
                        reserve_attempt=lambda: attempts.append(1) or True))
    assert result[3] == "LLM_TIMEOUT" and result[4] == {}
    assert len(attempts) == 1 and cancelled == [True]


@pytest.mark.parametrize("timeout", [0, 9, 181, -1])
def test_invalid_llm_timeout_is_rejected(timeout):
    with pytest.raises(RuntimeError, match="GT_LLM_REQUEST_TIMEOUT_SECONDS"):
        replace(settings, llm_request_timeout_seconds=timeout).validate_startup()
