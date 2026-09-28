"""A restored backup must not replay Telegram approvals or paid intake work."""
import importlib.util
from pathlib import Path

from app.db import db


def load_restore(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('restore_under_test', scripts / 'restore.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_invalidates_review_and_stops_queued_calls(tmp_path, monkeypatch):
    db.init(tmp_path / 'restore.sqlite3')
    for state in ('queued', 'analyzing', 'review', 'approved'):
        db.put('task', {'id': state, 'status': 'running' if state == 'approved' else 'review',
            'auto_run': True, 'intake': {'state': state, 'revision': 5,
                'reserved_tokens': 1800 if state == 'analyzing' else 0}})
    db.put('connector', {'id': 'bot', 'kind': 'telegram', 'enabled': True})
    db.put('connector', {'id': 'vk', 'kind': 'vk', 'enabled': True})
    db.set_setting('telegram_offset_bot', 900)
    db.put('session', {'id': 'old-session'})
    load_restore(monkeypatch).neutralize_restored_actions(db.connection)
    for state in ('queued', 'analyzing', 'review', 'approved'):
        task = db.get('task', state)
        assert task['intake']['revision'] == 6
        assert task['auto_run'] is False
    assert db.get('task', 'queued')['intake']['state'] == 'failed'
    assert db.get('task', 'analyzing')['intake']['state'] == 'failed'
    assert db.get('task', 'analyzing')['intake']['reserved_tokens'] == 1800
    assert db.get('task', 'review')['intake']['state'] == 'review'
    assert db.get('task', 'approved')['status'] == 'paused'
    assert db.get('connector', 'bot')['enabled'] is False
    assert db.get('connector', 'vk')['enabled'] is True
    assert db.setting('telegram_offset_bot') is None
    assert db.list('session') == []


def test_restored_intake_reservation_charged_once_without_model_call(tmp_path, monkeypatch):
    from app.engine import Engine
    from app.workflow import Workflow
    db.init(tmp_path / 'recovery.sqlite3')
    db.put('task', {'id': 'paid', 'status': 'review', 'auto_run': False,
        'max_output_tokens': 24000, 'output_tokens_used': 10, 'input_tokens_used': 2,
        'tokens_used': 12, 'usage_uncertain': False,
        'intake': {'state': 'analyzing', 'revision': 1, 'reserved_tokens': 1000}})
    load_restore(monkeypatch).neutralize_restored_actions(db.connection)
    workflow = Workflow(Engine())
    workflow.recover()
    workflow.recover()
    restored = db.get('task', 'paid')
    assert restored['intake']['state'] == 'failed'
    assert restored['intake']['reserved_tokens'] == 0
    assert restored['output_tokens_used'] == 1010
    assert restored['usage_uncertain'] is True


def test_restore_clears_bot_capabilities_and_delivery_queue(tmp_path, monkeypatch):
    db.init(tmp_path / 'capabilities.sqlite3')
    kinds = ('telegram_callback', 'telegram_session', 'telegram_outbox', 'telegram_notice')
    for kind in kinds:
        db.put(kind, {'id': 'old', 'active': True, 'used': False, 'status': 'sending'})
    db.put('publication', {'id': 'draft', 'status': 'approved', 'approved_at': 'old'})
    db.put('publication', {'id': 'inflight', 'status': 'publishing'})
    load_restore(monkeypatch).neutralize_restored_actions(db.connection)
    for kind in kinds:
        assert db.list(kind) == []
    assert db.get('publication', 'draft')['status'] == 'draft'
    assert db.get('publication', 'draft')['approved_at'] is None
    assert db.get('publication', 'inflight')['status'] == 'uncertain'


def test_restore_disables_new_automations_and_revokes_mcp_authority(tmp_path, monkeypatch):
    db.init(tmp_path / 'v2-restore.sqlite3')
    for kind in ('mcp', 'decision', 'crm', 'automation'):
        db.put(kind, {'id': 'configured', 'enabled': True})
    for state in ('pending', 'checking', 'executing', 'succeeded'):
        db.put('tool_request', {'id': state, 'status': state})
    db.put('chat', {'id': 'dialog', 'status': 'running'})
    db.put('telegram_chat_reply', {'id': 'queued', 'status': 'pending'})
    db.set_setting('bot-chat:bot:owner', 'dialog')
    db.set_setting('tool-preview:bot:owner:request', {'expires': 9999999999})
    load_restore(monkeypatch).neutralize_restored_actions(db.connection)
    for kind in ('mcp', 'decision', 'crm', 'automation'):
        assert db.get(kind, 'configured')['enabled'] is False
    for state in ('pending', 'checking'):
        assert db.get('tool_request', state)['status'] == 'rejected'
    assert db.get('tool_request', 'executing')['status'] == 'uncertain'
    assert db.get('tool_request', 'succeeded')['status'] == 'succeeded'
    assert db.get('chat', 'dialog')['status'] == 'failed'
    assert db.list('telegram_chat_reply') == []
    assert db.setting('bot-chat:bot:owner') is None
    assert db.setting('tool-preview:bot:owner:request') is None
