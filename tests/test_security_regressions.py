"""Approval and cancellation regressions; all external calls are isolated fakes."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from app import integrations, publications, toolbus
from app.db import db
from app.security import encrypt


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv('MASTER_KEY', Fernet.generate_key().decode())
    db.init(str(tmp_path / 'security.sqlite3'))


def tool_request(token='test-only-token'):
    tools = [
        {'name': name, 'description': 'Read status', 'inputSchema': {'type': 'object'}}
        for name in ('inspect', 'another_tool')
    ]
    db.put('mcp', {'id': 'server', 'enabled': True, 'url': 'https://example.com/mcp',
                   'tools': tools, 'token_encrypted': encrypt(token)})
    db.put('agent', {'id': 'agent', 'enabled': True,
                     'tool_grants': [{'server_id': 'server', 'tool': 'inspect'}]})
    db.put('task', {'id': 'task', 'status': 'running'})
    ticket = toolbus.propose('agent', 'server', 'inspect', {'name': 'status'}, task_id='task')
    return tools, ticket


def fake_session(monkeypatch, client):
    @asynccontextmanager
    async def session(server):
        yield client
    monkeypatch.setattr(toolbus, 'session', session)


def test_mcp_changed_unselected_catalog_entry_requires_new_approval(monkeypatch):
    tools, ticket = tool_request()
    changed = copy.deepcopy(tools)
    changed[1]['description'] = 'Changed meaning of the catalog'
    client = AsyncMock()
    client.tools.return_value = changed
    fake_session(monkeypatch, client)

    result = asyncio.run(toolbus.execute(ticket['id']))

    assert result['status'] == 'failed'
    client.rpc.assert_not_awaited()
    with pytest.raises(ValueError):
        asyncio.run(toolbus.execute(ticket['id']))


def test_owner_can_revoke_mcp_while_catalog_check_is_pending(monkeypatch):
    tools, ticket = tool_request()
    client = AsyncMock()
    fake_session(monkeypatch, client)

    async def scenario():
        checking, resume = asyncio.Event(), asyncio.Event()

        async def catalog():
            checking.set()
            await resume.wait()
            return tools

        client.tools.side_effect = catalog
        job = asyncio.create_task(toolbus.execute(ticket['id']))
        await asyncio.wait_for(checking.wait(), 1)
        assert db.get('tool_request', ticket['id'])['status'] == 'checking'
        rejected = toolbus.reject(ticket['id'])
        resume.set()
        result = await asyncio.wait_for(job, 1)
        assert result['status'] == 'rejected'
        assert result['error'] == rejected['error']
        client.rpc.assert_not_awaited()

    asyncio.run(scenario())


def test_mcp_execution_cannot_be_relabelled_as_rejected(monkeypatch):
    tools, ticket = tool_request()
    client = AsyncMock()
    client.tools.return_value = tools
    fake_session(monkeypatch, client)

    async def scenario():
        sending, resume = asyncio.Event(), asyncio.Event()

        async def call(*args):
            sending.set()
            await resume.wait()
            return {'content': []}

        client.rpc.side_effect = call
        job = asyncio.create_task(toolbus.execute(ticket['id']))
        await asyncio.wait_for(sending.wait(), 1)
        with pytest.raises(ValueError):
            toolbus.reject(ticket['id'])
        resume.set()
        assert (await asyncio.wait_for(job, 1))['status'] == 'done'
        client.rpc.assert_awaited_once()

    asyncio.run(scenario())


def test_mcp_redacts_token_before_json_escaping(monkeypatch):
    token = 'opaque"token\\with-escapes'
    tools, ticket = tool_request(token)
    client = AsyncMock()
    client.tools.return_value = tools
    client.rpc.return_value = {'content': [{'type': 'text', 'text': 'echo: ' + token}],
                               'structuredContent': {token: [token, 42, True]}}
    fake_session(monkeypatch, client)

    result = asyncio.run(toolbus.execute(ticket['id']))

    assert result['status'] == 'done'
    decoded = json.loads(result['result'])
    assert decoded['content'][0]['text'] == 'echo: [секрет скрыт]'
    assert decoded['structuredContent'] == {'[секрет скрыт]': ['[секрет скрыт]', 42, True]}


def test_cancelled_publication_is_uncertain_and_cannot_be_sent_again(monkeypatch):
    db.put('connector', {'id': 'connector', 'kind': 'webhook', 'enabled': True,
                         'config': {'url': 'https://example.com/events'}})
    draft = publications.create_publication({'connector_id': 'connector', 'text': 'Reviewed text'})
    publications.approve_publication(draft['id'])
    publish = AsyncMock()
    monkeypatch.setattr(integrations, 'publish', publish)

    async def scenario():
        sending = asyncio.Event()

        async def send(*args):
            sending.set()
            await asyncio.Event().wait()

        publish.side_effect = send
        job = asyncio.create_task(publications.publish_publication(draft['id']))
        await asyncio.wait_for(sending.wait(), 1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert db.get('publication', draft['id'])['status'] == 'uncertain'
        assert publications._active is False
        with pytest.raises(ValueError):
            await publications.publish_publication(draft['id'])
        publish.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize('response', [
    {}, {'result': {'message_id': 5}}, {'ok': 'true', 'result': {'message_id': 5}},
])
def test_malformed_telegram_write_response_is_uncertain(monkeypatch, response):
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(integrations, 'request_json', request)
    with pytest.raises(integrations.IntegrationError) as error:
        asyncio.run(integrations._telegram('test-token', 'sendMessage', {'text': 'Reviewed'}, effect=True))
    assert error.value.uncertain is True
    request.assert_awaited_once()


def test_explicit_telegram_rejection_is_not_reported_as_uncertain(monkeypatch):
    monkeypatch.setattr(integrations, 'request_json', AsyncMock(return_value={'ok': False, 'error_code': 403}))
    with pytest.raises(integrations.IntegrationError) as error:
        asyncio.run(integrations._telegram('test-token', 'sendMessage', {'text': 'Reviewed'}, effect=True))
    assert error.value.uncertain is False
