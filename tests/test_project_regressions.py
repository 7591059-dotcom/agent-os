import asyncio

import httpx
import pytest

from app.db import db
from app import workspace, integrations
from app.reports import task_report
from test_api import configured, login


def test_report_requires_login_and_exports_team_results_without_configuration(configured):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured),
                                        base_url='http://localhost:8000',
                                        headers={'Origin': 'http://localhost:8000'}) as client:
                assert (await client.get('/api/tasks/missing/export')).status_code == 401
                await login(client)
                assert (await client.get('/api/tasks/missing/export')).status_code == 404
                project = db.put('project', {'id': 'p', 'name': 'Редакция', 'description': 'Общий контекст'})
                db.put('agent', {'id': 'a', 'name': 'Редактор', 'instructions': 'PRIVATE-INSTRUCTION',
                                 'provider_id': 'PRIVATE-PROVIDER'})
                task = db.put('task', {'id': 't', 'title': 'Проверяемая работа', 'brief': 'Подготовить текст',
                                      'status': 'done', 'project_id': project['id'], 'result': 'Итог команды',
                                      'usage_uncertain': True, 'output_tokens_used': 12,
                                      'steps': [{'id': 's1', 'agent_id': 'a', 'name': 'Правка',
                                                 'status': 'done', 'output': 'Работа редактора',
                                                 'depends_on': [], 'input': 'Проверить факты'}]})
                response = await client.get('/api/tasks/t/export')
                assert response.status_code == 200
                assert response.headers['content-disposition'] == 'attachment; filename="task-t.md"'
                assert response.headers['cache-control'] == 'no-store'
                assert response.headers['content-type'].startswith('text/markdown')
                for text in ('Итог команды', 'Работа редактора', 'Общий контекст', 'Редактор', 'неопределённые'):
                    assert text in response.text
                assert 'PRIVATE-' not in response.text
                assert db.get('task', 't') == task
    asyncio.run(scenario())


def test_report_keeps_html_and_markdown_inside_literal_fences():
    result = '```\n<script>alert(1)</script>\n# Подмена раздела'
    report = task_report({'id': 't', 'title': '<img src=x>', 'result': result})
    assert f'````text\n{result}\n````' in report
    assert '```text\n<img src=x>\n```' in report


def test_stale_publication_approval_cannot_authorize_changed_text(configured, monkeypatch):
    async def scenario():
        sent = []
        async def publish(item, connector):
            sent.append(item['text'])
            return {'external_id': 'test-only'}
        monkeypatch.setattr(integrations, 'publish', publish)
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured),
                                        base_url='http://localhost:8000',
                                        headers={'Origin': 'http://localhost:8000'}) as client:
                await login(client)
                connector = db.put('connector', {'id': 'c', 'kind': 'vk', 'enabled': True, 'config': {}})
                first = (await client.post('/api/publications', json={'connector_id': connector['id'], 'text': 'Первая версия'})).json()
                path = '/api/publications/' + first['id']
                revised = (await client.patch(path, json={'text': 'Вторая версия'})).json()
                assert (await client.post(path + '/approve')).status_code == 400
                assert (await client.post(path + '/approve', headers={'X-Resource-Version': first['updated_at']})).status_code == 400
                assert db.get('publication', first['id'])['status'] == 'draft'
                approved = await client.post(path + '/approve', headers={'X-Resource-Version': revised['updated_at']})
                assert approved.status_code == 200
                assert (await client.post(path + '/publish', headers={'X-Resource-Version': revised['updated_at']})).status_code == 400
                assert not sent
                assert (await client.post(path + '/publish', headers={'X-Resource-Version': approved.json()['updated_at']})).status_code == 200
                assert sent == ['Вторая версия']
    asyncio.run(scenario())


def test_late_chat_reply_preserves_stop_and_marks_uncertain_usage(configured, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return {'text': 'Запоздалый ответ', 'usage': {'output_tokens': 20, 'source': 'unknown'}}
        monkeypatch.setattr(workspace, 'run_agent', delayed)
        async with configured.router.lifespan_context(configured):
            provider = db.list('provider')[0]
            chat = workspace.create_chat({'title': 'Тест', 'provider_id': provider['id']})
            configured.state.workspace.message(chat['id'], 'Вопрос')
            await asyncio.wait_for(started.wait(), 2)
            job = configured.state.workspace.jobs[chat['id']]
            await workspace.stop_chat(chat['id'])
            release.set()
            await asyncio.wait_for(job, 2)
            result = db.get('chat', chat['id'])
            assert result['status'] == 'stopped'
            assert len(result['messages']) == 1
            assert result['tokens_used'] == 20 and result['usage_uncertain'] is True
    asyncio.run(scenario())


@pytest.mark.parametrize('request_started', [False, True])
def test_interrupted_chat_accounts_for_whether_a_request_was_started(configured, monkeypatch, request_started):
    async def scenario():
        started = asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(workspace, 'run_agent', delayed)
        async with configured.router.lifespan_context(configured):
            if not request_started:
                monkeypatch.setattr(configured.state.engine, '_slots', asyncio.Semaphore(0))
            chat = workspace.create_chat({'title': 'Прерывание', 'provider_id': db.list('provider')[0]['id']})
            configured.state.workspace.message(chat['id'], 'Вопрос')
            if request_started:
                await asyncio.wait_for(started.wait(), 2)
            else:
                await asyncio.sleep(0)
            await configured.state.workspace.stop()
            saved = db.get('chat', chat['id'])
            assert saved['status'] == 'failed'
            assert saved['usage_uncertain'] is request_started
            assert saved['request_started'] is False
    asyncio.run(scenario())


def test_chat_recovery_marks_legacy_and_stopped_inflight_requests_uncertain(configured):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            for identifier, fields in [
                ('legacy', {'status': 'running'}),
                ('queued', {'status': 'running', 'request_started': False}),
                ('stopped', {'status': 'stopped', 'request_started': True}),
            ]:
                db.put('chat', {'id': identifier, 'usage_uncertain': False, **fields})
            workspace.Workspace(configured.state.engine)
            assert db.get('chat', 'legacy')['usage_uncertain'] is True
            assert db.get('chat', 'queued')['usage_uncertain'] is False
            assert db.get('chat', 'stopped')['usage_uncertain'] is True
            assert db.get('chat', 'stopped')['status'] == 'stopped'
    asyncio.run(scenario())


def test_demo_chat_does_not_claim_uncertain_usage(configured):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            chat = workspace.create_chat({'title': 'Демо', 'provider_id': db.list('provider')[0]['id']})
            configured.state.workspace.message(chat['id'], 'Привет')
            await asyncio.wait_for(configured.state.workspace.jobs[chat['id']], 2)
            saved = db.get('chat', chat['id'])
            assert saved['status'] == 'idle'
            assert saved['tokens_used'] == 0 and saved['usage_uncertain'] is False
    asyncio.run(scenario())
