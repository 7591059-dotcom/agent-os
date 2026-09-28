"""Shared HTTP/workflow/Telegram configuration checks; all outbound calls mocked."""
import asyncio

import httpx
import pytest

from test_api import configured, login
from app.db import db


@pytest.fixture(autouse=True)
def no_telegram_network(monkeypatch):
    from app import integrations
    async def fake(token, method, payload=None, **kwargs):
        if method == 'getUpdates':
            await asyncio.sleep(.01)
            return []
        raise AssertionError('Unexpected external Telegram operation')
    monkeypatch.setattr(integrations, '_telegram', fake)


def test_web_idea_review_revision_execution_and_telegram_publication(configured, monkeypatch):
    from app import integrations
    sent = []
    async def telegram(token, method, payload=None, **kwargs):
        if method == 'getUpdates':
            await asyncio.sleep(.01)
            return []
        raise AssertionError('Unexpected Telegram network operation')
    async def publish(publication, connector):
        sent.append((publication['id'], connector['config']['chat_id']))
        return {'external_id': 'channel:test'}
    monkeypatch.setattr(integrations, '_telegram', telegram)
    monkeypatch.setattr(integrations, 'publish', publish)
    async def run():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured), base_url='http://localhost:8000', headers={'Origin': 'http://localhost:8000'}) as c:
                assert (await c.post('/api/ideas', json={'text': 'x', 'connector_id': 'x'})).status_code == 401
                await login(c)
                provider = (await c.get('/api/state')).json()['providers'][0]['id']
                connection = (await c.post('/api/connectors', json={'name': 'Личный бот', 'kind': 'telegram',
                    'config': {'allowed_user_ids': [123], 'planner_provider_id': provider,
                               'notifications_enabled': False, 'chat_id': '@test_channel'},
                    'secrets': {'bot_token': '123456:abcdefghijklmnopqrstuvwxyz'}})).json()
                assert 'id' in connection, connection
                response = await c.post('/api/ideas', json={'text': 'Подготовить три варианта текста', 'connector_id': connection['id']})
                assert response.status_code == 200, response.text
                task = response.json(); tid = task['id']
                assert task['source'] == 'web' and task['telegram']['user_id'] == 123
                assert task['status'] == 'review' and not task['steps']
                assert (await c.post(f'/api/tasks/{tid}/plan')).status_code == 400
                assert (await c.patch(f'/api/tasks/{tid}', json={'brief': 'Подмена задания'})).status_code == 400
                assert (await c.patch(f'/api/tasks/{tid}', json={'max_output_tokens': 30000})).status_code == 200
                async def wait(state):
                    for _ in range(150):
                        task = (await c.get(f'/api/tasks/{tid}')).json()['task']
                        if (task.get('intake', {}).get('state') if state == 'review' else task['status']) == state:
                            return task
                        await asyncio.sleep(.03)
                    raise AssertionError(task)
                task = await wait('review')
                assert task['intake']['proposal']
                assert (await c.post(f'/api/tasks/{tid}/intake/revise', json={'revision': 1, 'feedback': 'Без призыва покупать'})).status_code == 200
                assert (await c.post(f'/api/tasks/{tid}/intake/approve', json={'revision': 1})).status_code == 400
                await wait('review')
                approved = await c.post(f'/api/tasks/{tid}/intake/approve', json={'revision': 2})
                assert approved.status_code == 200, approved.text
                assert (await c.post(f'/api/tasks/{tid}/intake/approve', json={'revision': 2})).status_code == 400
                task = await wait('done')
                assert task['result'] and task['steps']
                assert not sent
                publication = (await c.post('/api/publications', json={'task_id': tid, 'connector_id': connection['id'], 'text': task['result']})).json()
                assert 'id' in publication, publication
                pid = publication['id']
                assert (await c.post(f'/api/publications/{pid}/publish')).status_code == 400
                approved = await c.post(f'/api/publications/{pid}/approve', headers={'X-Resource-Version':publication['updated_at']})
                assert approved.status_code == 200
                assert not sent
                revised = (await c.patch(f'/api/publications/{pid}', json={'text': 'Финальная правка'})).json()
                assert revised['status'] == 'draft'
                assert (await c.post(f'/api/publications/{pid}/publish', headers={'X-Resource-Version':approved.json()['updated_at']})).status_code == 400
                approved = await c.post(f'/api/publications/{pid}/approve', headers={'X-Resource-Version':revised['updated_at']})
                assert approved.status_code == 200
                assert (await c.post(f'/api/publications/{pid}/publish', headers={'X-Resource-Version':approved.json()['updated_at']})).status_code == 200
                assert len(sent) == 1 and sent[0][1] == '@test_channel'
                assert (await c.post(f'/api/publications/{pid}/publish', headers={'X-Resource-Version':approved.json()['updated_at']})).status_code == 400
                assert len(sent) == 1
    asyncio.run(run())


def test_telegram_configuration_validation_and_duplicate_controller(configured, monkeypatch):
    async def run():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured), base_url='http://localhost:8000', headers={'Origin': 'http://localhost:8000'}) as c:
                await login(c)
                provider = (await c.get('/api/state')).json()['providers'][0]['id']
                base = {'name': 'bot', 'kind': 'telegram', 'config': {'allowed_user_ids': [321], 'planner_provider_id': provider, 'chat_id': -1001234567890}, 'secrets': {'bot_token': '123456:abcdefghijklmnopqrstuvwxyz'}}
                for extra in ({'voice_enabled': 'yes'}, {'voice_enabled': True}, {'transcription_provider_id': provider}, {'max_steps': True}, {'allowed_user_ids': [True]}, {'chat_id': 'https://evil.example'}):
                    response = await c.post('/api/connectors', json={**base, 'config': {**base['config'], **extra}})
                    assert response.status_code == 400, (extra, response.text)
                good = await c.post('/api/connectors', json=base)
                assert good.status_code == 200, good.text
                assert good.json()['config']['chat_id'] == '-1001234567890'
                assert (await c.post('/api/connectors', json=base)).status_code == 400
                assert (await c.delete('/api/providers/' + provider)).status_code == 400
                channel = await c.post('/api/connectors', json={**base, 'config': {'controller_enabled': False, 'chat_id': '@channel'}})
                assert channel.status_code == 200, channel.text
                assert (await c.post('/api/ideas', json={'text': 'x', 'connector_id': channel.json()['id']})).status_code == 400
    asyncio.run(run())
