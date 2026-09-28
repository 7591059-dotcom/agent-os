import asyncio
import importlib

import pytest
from app.db import db
from app.engine import Engine, EngineError
from app.workflow import Workflow

module = importlib.import_module('app.workflow')

@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    db.init(tmp_path / 'workflow.sqlite3')
    monkeypatch.setenv('DEMO_MODE', 'true')
    db.put('provider', {'id': 'p', 'kind': 'mock', 'enabled': True})


def idea(workflow, **config):
    return workflow.create_idea('Подготовь контент', config={'planner_provider_id': 'p', **config})


def response(text='Предлагаю подготовить три черновика. Одобрить?', tokens=9):
    return {'text': text, 'usage': {'input_tokens': 4, 'output_tokens': tokens, 'source': 'provider'}}


def test_review_gate_stale_revision_and_concurrent_approval(monkeypatch):
    async def fake(*args, **kwargs):
        return response()
    monkeypatch.setattr(module, 'complete', fake)
    async def scenario():
        engine = Engine()
        engine._slots = asyncio.Semaphore(3)
        workflow = Workflow(engine)
        task = idea(workflow)
        assert task['status'] == 'review' and task['intake']['state'] == 'queued'
        for action in ['plan', 'run', 'resume', 'retry']:
            with pytest.raises(EngineError):
                await engine.action(task['id'], action)
        await workflow._analyze(task['id'])
        assert db.list('agent') == []
        with pytest.raises(EngineError):
            await workflow.approve(task['id'], 2)
        results = await asyncio.gather(workflow.approve(task['id'], 1), workflow.approve(task['id'], 1), return_exceptions=True)
        assert sum(isinstance(r, EngineError) for r in results) == 1
        saved = db.get('task', task['id'])
        assert saved['status'] == 'planning' and saved['auto_run']
        assert saved['intake']['state'] == 'approved'
        assert 'три черновика' in saved['brief']
    asyncio.run(scenario())


def test_revisions_keep_feedback_and_reject_stale_buttons(monkeypatch):
    seen = []
    async def fake(provider, messages, **kwargs):
        seen.append(messages[-1]['content'])
        return response()
    monkeypatch.setattr(module, 'complete', fake)
    async def scenario():
        engine = Engine(); engine._slots = asyncio.Semaphore(3)
        workflow = Workflow(engine)
        task = idea(workflow)
        await workflow._analyze(task['id'])
        revised = workflow.revise(task['id'], 'Только VK', 1)
        assert revised['intake']['revision'] == 2 and revised['intake']['state'] == 'queued'
        with pytest.raises(EngineError):
            workflow.reject(task['id'], 1)
        await workflow._analyze(task['id'])
        assert 'Только VK' in seen[-1]
        assert db.get('task', task['id'])['output_tokens_used'] == 18
        assert workflow.reject(task['id'], 2)['status'] == 'cancelled'
        with pytest.raises(EngineError):
            await workflow.approve(task['id'], 2)
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', ['reject', 'engine'])
def test_cancel_inflight_settles_usage_without_resurrection(monkeypatch, cancel):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def fake(*args, **kwargs):
            entered.set(); await release.wait()
            return response()
        monkeypatch.setattr(module, 'complete', fake)
        engine = Engine(); engine._slots = asyncio.Semaphore(3)
        workflow = Workflow(engine); task = idea(workflow)
        job = asyncio.create_task(workflow._analyze(task['id']))
        await entered.wait()
        if cancel == 'reject':
            workflow.reject(task['id'], 1)
        else:
            await engine.action(task['id'], 'cancel')
        release.set(); await job
        saved = db.get('task', task['id'])
        assert saved['status'] == 'cancelled' and saved['intake']['state'] == 'rejected'
        assert saved['intake']['proposal'] == '' and saved['output_tokens_used'] == 9
    asyncio.run(scenario())


def test_crash_reservation_not_replayed_and_requires_explicit_revision(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def fake(*args, **kwargs):
            entered.set(); await asyncio.Event().wait()
        monkeypatch.setattr(module, 'complete', fake)
        engine = Engine(); engine._slots = asyncio.Semaphore(3)
        workflow = Workflow(engine); task = idea(workflow)
        job = asyncio.create_task(workflow._analyze(task['id']))
        await entered.wait(); job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        workflow.recover(); workflow.recover()
        saved = db.get('task', task['id'])
        assert saved['intake']['state'] == 'failed'
        assert saved['output_tokens_used'] == 2000 and saved['usage_uncertain']
        with pytest.raises(EngineError):
            await workflow.approve(task['id'], 1)
        assert workflow.revise(task['id'], 'Повторить анализ', 1)['intake']['state'] == 'queued'
    asyncio.run(scenario())


def test_analysis_budget_exhaustion_blocks_approval_and_retry(monkeypatch):
    async def fake(*args, **kwargs):
        return response(tokens=kwargs['max_tokens'])
    monkeypatch.setattr(module, 'complete', fake)
    async def scenario():
        engine = Engine(); engine._slots = asyncio.Semaphore(3)
        workflow = Workflow(engine); task = idea(workflow, max_output_tokens=256)
        await workflow._analyze(task['id'])
        with pytest.raises(EngineError):
            await workflow.approve(task['id'], 1)
        workflow.revise(task['id'], 'Повторить', 1)
        await workflow._analyze(task['id'])
        saved = db.get('task', task['id'])
        assert saved['intake']['state'] == 'failed' and saved['output_tokens_used'] == 256
    asyncio.run(scenario())


def test_telegram_attribution_and_validation():
    workflow = Workflow(Engine())
    with pytest.raises(EngineError):
        workflow.create_idea('Идея', config={})
    assert db.list('task') == []
    task = workflow.create_idea('Идея', config={'planner_provider_id': 'p'}, source='telegram', connector_id='c', user_id=123)
    assert task['telegram'] == {'connector_id': 'c', 'user_id': 123}


def test_intake_respects_shared_model_slots_and_does_not_duplicate_claim(monkeypatch):
    async def scenario():
        calls = 0
        entered, release = asyncio.Event(), asyncio.Event()
        async def fake(*args, **kwargs):
            nonlocal calls
            calls += 1; entered.set(); await release.wait()
            return response()
        monkeypatch.setattr(module, 'complete', fake)
        engine = Engine(); engine._slots = asyncio.Semaphore(1)
        workflow = Workflow(engine); task = idea(workflow)
        await engine._slots.acquire()
        first = asyncio.create_task(workflow._analyze(task['id']))
        second = asyncio.create_task(workflow._analyze(task['id']))
        await asyncio.sleep(.01)
        assert calls == 0 and db.get('task', task['id'])['intake']['state'] == 'queued'
        engine._slots.release(); await entered.wait()
        release.set(); await asyncio.gather(first, second)
        assert calls == 1 and db.get('task', task['id'])['output_tokens_used'] == 9
    asyncio.run(scenario())
