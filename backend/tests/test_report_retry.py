import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.main import app
from app.db import AnalysisRun, AgentArtifact, AgentStep, SandboxTicket, db_session
from app.services import agent_runtime as runtime, agent_graph as graph
from test_agents import Provider, REAL_REQUEST, setup_live, start_uploaded
from test_golden_flow import wait_for_status


@pytest.mark.parametrize('failure,code', [(httpx.ConnectError('secret'), 'LLM_TRANSPORT_ERROR'),
    (httpx.RemoteProtocolError('secret'), 'LLM_TRANSPORT_ERROR'),
    (httpx.ReadTimeout('secret'), 'LLM_TIMEOUT'), (ValueError('secret'), 'LLM_RESPONSE_INVALID')])
def test_provider_errors_are_precise_and_safe(monkeypatch, failure, code):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): raise failure
    monkeypatch.setattr(runtime.httpx, 'AsyncClient', lambda **kwargs: Client())
    with pytest.raises(runtime.AgentError) as exc:
        asyncio.run(REAL_REQUEST([], [], require_tool=False))
    assert str(exc.value) == code


@pytest.mark.parametrize('recover', [True, False])
def test_transport_retry_once_and_charged(monkeypatch, recover):
    provider = Provider()
    calls, reservations = [], []
    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1 or not recover:
            raise runtime.AgentError('LLM_TRANSPORT_ERROR')
        return await provider(*args, **kwargs)
    monkeypatch.setattr(runtime, 'request', flaky)
    async def run():
        return await runtime.run_agent('performance', {'facts': {}}, reserve=lambda: reservations.append(1), record_tool=lambda *a: None)
    if recover:
        result = asyncio.run(run())
        assert result['model_requests'] == 3
    else:
        with pytest.raises(runtime.AgentError, match='LLM_TRANSPORT_ERROR'):
            asyncio.run(run())
        assert len(calls) == 2
    assert len(calls) == len(reservations)


def test_report_retry_reuses_draft_and_preserves_ticket(monkeypatch):
    provider = Provider()
    broken = [True]
    async def flaky(messages, tools, **kwargs):
        keys = tools[0]['function']['parameters']['properties']['key']['enum']
        if 'report' in keys and broken[0]:
            raise runtime.AgentError('LLM_PROVIDER_ERROR')
        return await provider(messages, tools, **kwargs)
    setup_live(monkeypatch, flaky)
    with TestClient(app) as client:
        csrf, run_id = start_uploaded(client)
        waiting = wait_for_status(client, run_id, {'waiting_approval'})
        client.post(f"/api/v1/approvals/{waiting['pending_approval_id']}/decision", headers={'X-CSRF-Token': csrf},
                    json={'decision': 'approved', 'decision_key': str(uuid.uuid4()), 'comment': 'test'})
        failed = wait_for_status(client, run_id, {'failed'})
        assert failed['current_phase'] == 'final_review'
        with TestClient(app) as stranger:
            other = stranger.post('/api/v1/sessions/guest').json()['data']['csrf_token']
            assert stranger.post(f'/api/v1/runs/{run_id}/retry-report', headers={'X-CSRF-Token': other}).status_code == 404
        assert client.post(f'/api/v1/runs/{run_id}/retry-report').status_code == 403
        broken[0] = False
        assert client.post(f'/api/v1/runs/{run_id}/retry-report', headers={'X-CSRF-Token': csrf}).status_code == 200
        assert wait_for_status(client, run_id, {'succeeded','failed'})['status'] == 'succeeded'
        assert provider.roles.count('feedback') == 1 and provider.roles.count('report_builder') == 1
        assert client.post(f'/api/v1/runs/{run_id}/retry-report', headers={'X-CSRF-Token': csrf}).json()['data']['queued'] is False
        with db_session() as session:
            assert session.scalar(select(func.count()).select_from(SandboxTicket).where(SandboxTicket.run_id == run_id)) == 1
            assert session.get(AnalysisRun, run_id).completed_steps == 9
