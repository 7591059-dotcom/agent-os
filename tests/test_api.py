import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from app.db import db
from app.security import password_hasher, totp


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('MASTER_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('ADMIN_PASSWORD_HASH', password_hasher.hash(' Long test password 458! '))
    monkeypatch.setenv('ADMIN_USERNAME', 'Сергей')
    monkeypatch.setenv('PUBLIC_URL', 'http://localhost:8000')
    monkeypatch.setenv('SECURE_COOKIES', 'false')
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.setenv('APP_DATA_DIR', str(tmp_path))
    from app.main import app
    return app


async def login(client):
    result = await client.post('/api/auth/login', json={'username': 'Сергей', 'password': ' Long test password 458! '})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf_token']
    return result


def test_auth_csrf_encryption_and_validation(configured):
    async def run():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured), base_url='http://localhost:8000', headers={'Origin': 'http://localhost:8000'}) as c:
                assert (await c.get('/api/state')).status_code == 401
                assert (await c.get('/api/auth/session')).json()['authenticated'] is False
                assert (await c.post('/api/auth/login', json={'username': 'Сергей', 'password': 'x'}, headers={'Origin': 'https://evil.example'})).status_code == 403
                assert (await c.post('/api/auth/login', json={'username': 'Сергей', 'password': 'x'})).status_code == 401
                response = await login(c)
                assert 'HttpOnly' in response.headers['set-cookie']
                assert 'SameSite=strict' in response.headers['set-cookie']
                assert (await c.post('/api/agents', json={}, headers={'X-CSRF-Token': 'wrong'})).status_code == 403
                # Payload values containing credentials must not echo in schema errors.
                bad = await c.post('/api/providers', json={'name': 'x', 'kind': 'openai', 'model': 'a', 'api_key': 'secret-to-not-echo', 'oops': 'hidden-secret'})
                assert bad.status_code == 422
                assert 'hidden-secret' not in bad.text and 'secret-to-not-echo' not in bad.text
                p = await c.post('/api/providers', json={'name': 'Compatible', 'kind': 'openai_compatible', 'model': 'test-model', 'base_url': 'https://api.example.com/v1', 'api_key': 'PRIVATE-TEST-KEY'})
                assert p.status_code == 200, p.text
                pid = p.json()['id']
                assert p.json()['has_api_key'] and 'api_key_encrypted' not in p.json()
                assert 'PRIVATE-TEST-KEY' not in (await c.get('/api/state')).text
                assert 'PRIVATE-TEST-KEY' not in json.dumps(db.get('provider', pid))
                changed = await c.patch('/api/providers/' + pid, json={'base_url': 'https://different.example/v1'})
                assert changed.status_code == 400
                assert (await c.patch('/api/providers/' + pid, json={'api_key_encrypted': 'attacker'})).status_code == 400
                assert (await c.post('/api/auth/logout')).status_code == 200
                assert (await c.get('/api/state')).status_code == 401
    asyncio.run(run())


def test_complete_demo_workflow_and_publication_approval(configured, monkeypatch):
    async def run():
        from app import integrations
        calls = []
        async def fake_publish(publication, connector):
            calls.append(publication['id'])
            await asyncio.sleep(.03)
            return {'external_id': 'test-post-id'}
        monkeypatch.setattr(integrations, 'publish', fake_publish)
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured), base_url='http://localhost:8000', headers={'Origin': 'http://localhost:8000'}) as c:
                await login(c)
                state = (await c.get('/api/state')).json()
                provider = next(p for p in state['providers'] if p['kind'] == 'mock')
                task = (await c.post('/api/tasks', json={'title': 'Контент для ВК', 'brief': 'Подготовь текст о новом канале без непроверенных фактов.', 'planner_provider_id': provider['id']})).json()
                tid = task['id']
                plan = await c.post(f'/api/tasks/{tid}/plan')
                assert plan.status_code == 200, plan.text
                for _ in range(100):
                    task = (await c.get(f'/api/tasks/{tid}')).json()['task']
                    if task['status'] in ('ready', 'failed'):
                        break
                    await asyncio.sleep(.05)
                assert task['status'] == 'ready', task
                assert len(task['steps']) and all(s['status'] == 'pending' for s in task['steps'])
                assert (await c.patch(f'/api/tasks/{tid}', json={'steps': task['steps']})).status_code == 200
                result = await c.post(f'/api/tasks/{tid}/run')
                assert result.status_code == 200, result.text
                for _ in range(200):
                    task = (await c.get(f'/api/tasks/{tid}')).json()['task']
                    if task['status'] in ('done', 'review', 'failed'):
                        break
                    await asyncio.sleep(.05)
                assert task['status'] in ('done', 'review'), task
                assert task['result'] and all(s['status'] == 'done' for s in task['steps'])
                connector = (await c.post('/api/connectors', json={'name': 'VK', 'kind': 'vk', 'config': {'owner_id': '-123'}, 'secrets': {'access_token': 'token'}})).json()
                p = (await c.post('/api/publications', json={'connector_id': connector['id'], 'task_id': tid, 'text': task['result']})).json()
                assert (await c.post(f"/api/publications/{p['id']}/publish")).status_code == 400
                assert len(calls) == 0
                approval = await c.post(f"/api/publications/{p['id']}/approve", headers={'X-Resource-Version':p['updated_at']})
                assert approval.status_code == 200
                version = {'X-Resource-Version':approval.json()['updated_at']}
                responses = await asyncio.gather(c.post(f"/api/publications/{p['id']}/publish", headers=version), c.post(f"/api/publications/{p['id']}/publish", headers=version))
                assert sorted(r.status_code for r in responses) in ([200, 400], [200, 409])
                assert calls == [p['id']]
                p2 = (await c.post('/api/publications', json={'connector_id': connector['id'], 'text': 'x'})).json()
                approval = await c.post(f"/api/publications/{p2['id']}/approve", headers={'X-Resource-Version':p2['updated_at']})
                assert approval.status_code == 200
                await c.patch(f"/api/connectors/{connector['id']}", json={'config': {'owner_id': '-999'}})
                assert (await c.post(f"/api/publications/{p2['id']}/publish", headers={'X-Resource-Version':approval.json()['updated_at']})).status_code == 400
                assert len(calls) == 1
    asyncio.run(run())


def test_totp_enable_revokes_sessions_and_replay(configured):
    async def run():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured), base_url='http://localhost:8000', headers={'Origin': 'http://localhost:8000'}) as c:
                await login(c)
                setup = (await c.post('/api/settings/totp/setup')).json()
                code = totp(setup['secret'])
                result = await c.post('/api/settings/totp/enable', json={'code': code})
                assert result.status_code == 200 and result.json()['login_required']
                assert (await c.get('/api/state')).status_code == 401
                replay = await c.post('/api/auth/login', json={'username': 'Сергей', 'password': ' Long test password 458! ', 'totp': code})
                assert replay.status_code == 401
    asyncio.run(run())


def test_login_reserves_attempts_before_password_hash(configured, monkeypatch):
    import app.main as main
    import time
    async def run():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured), base_url='http://localhost:8000', headers={'Origin': 'http://localhost:8000'}) as c:
                def slow_check(password):
                    time.sleep(.1)
                    return False
                monkeypatch.setattr(main, 'check_password', slow_check)
                results = await asyncio.gather(*[c.post('/api/auth/login', json={'username': 'x', 'password': 'x'}) for _ in range(8)])
                assert sum(r.status_code == 401 for r in results) <= 2
                assert sum(r.status_code == 429 for r in results) >= 6
    asyncio.run(run())
