"""Offline regressions for DAG validation, recovery and bounded agent turns."""
import asyncio
import importlib
import json

import pytest

from app.db import db
from app.engine import Engine, EngineError, validate_steps


engine_module = importlib.import_module("app.engine")
runtime = importlib.import_module("app.agent_runtime")


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    db.init(tmp_path / "engine-regressions.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "true")
    db.put("provider", {"id": "p", "kind": "mock", "enabled": True})


def ready(engine, *, allowance=100):
    db.put("agent", {"id": "a", "name": "Автор", "instructions": "Подготовь ответ",
                     "provider_id": "p", "enabled": True, "max_tokens": allowance})
    task = engine.create_task({"title": "Проверка", "brief": "Подготовь текст"})
    return db.update("task", task["id"], {"status": "running", "steps": [
        {"id": "s1", "name": "Ответ", "agent_id": "a", "depends_on": [],
         "input": "", "status": "pending", "attempts": 0, "reserved_tokens": 0}
    ]})


def response(tokens=7):
    return {"text": "Готово", "usage": {"input_tokens": 2, "output_tokens": tokens,
                                        "source": "provider"}}


@pytest.mark.parametrize("identifier", [" s1", "s1 ", "s1\n"])
def test_step_ids_cannot_change_between_validation_and_execution(identifier):
    steps = [{"id": identifier, "name": "Первый", "agent_id": "a", "depends_on": []},
             {"id": "s2", "name": "Второй", "agent_id": "a", "depends_on": ["s1"]}]
    with pytest.raises(EngineError):
        validate_steps(steps)


def test_shutdown_releases_a_step_that_never_acquired_a_model_slot(monkeypatch):
    async def scenario():
        calls = []

        async def complete(*args, **kwargs):
            calls.append(True)
            return response()

        monkeypatch.setattr(engine_module, "complete", complete)
        engine = Engine()
        engine._slots = asyncio.Semaphore(0)
        task = ready(engine)
        allowance = engine._reserve_step(task["id"], "s1")
        job = asyncio.create_task(engine._execute_step(task["id"], "s1", allowance))
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        saved = db.get("task", task["id"])
        assert calls == []
        assert saved["steps"][0]["status"] == "pending"
        assert saved["steps"][0]["reserved_tokens"] == 0
        assert saved["output_tokens_used"] == 0
        assert saved["usage_uncertain"] is False

    asyncio.run(scenario())


def test_recovery_does_not_charge_a_durable_but_unsent_reservation(monkeypatch):
    async def scenario():
        engine = Engine()
        task = ready(engine)
        engine._reserve_step(task["id"], "s1")
        # Simulate process termination before the queued coroutine began.
        db.update("task", task["id"], {"status": "paused"})
        await engine.start()
        try:
            saved = db.get("task", task["id"])
            assert saved["steps"][0]["status"] == "pending"
            assert saved["steps"][0]["reserved_tokens"] == 0
            assert saved["output_tokens_used"] == 0
            assert saved["usage_uncertain"] is False
        finally:
            await engine.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("allowance", [1, 31])
def test_remaining_small_budget_is_used_for_a_text_reply(monkeypatch, allowance):
    async def scenario():
        limits = []

        async def complete(*args, **kwargs):
            limits.append(kwargs["max_tokens"])
            return response(allowance)

        result = await runtime.run({"id": "a"}, {"id": "p"},
                                   [{"role": "user", "content": "Ответь кратко"}],
                                   allowance, complete_fn=complete)
        assert limits == [allowance]
        assert result["text"] == "Готово" and not result.get("error")

    asyncio.run(scenario())


def test_tool_proposal_is_not_created_without_remaining_answer_budget(monkeypatch):
    async def scenario():
        monkeypatch.setattr(runtime.toolbus, "available", lambda agent: [{"name": "tool"}])
        proposals = []

        def propose(*args, **kwargs):
            proposals.append(True)
            return {"id": "ticket", "status": "done", "result": "Выполнено"}

        monkeypatch.setattr(runtime.toolbus, "propose", propose)

        async def complete(*args, **kwargs):
            result = response(100)
            result["text"] = '{"type":"tool","server_id":"m","tool":"tool","arguments":{}}'
            return result

        result = await runtime.run({"id": "a"}, {"id": "p"},
                                   [{"role": "user", "content": "Задача"}],
                                   100, complete_fn=complete)
        assert proposals == []
        assert result.get("error")
        assert result["usage"]["output_tokens"] == 100

    asyncio.run(scenario())


@pytest.mark.parametrize("parent_kind", ["task", "chat"])
def test_project_context_reaches_agents_as_data_without_private_configuration(parent_kind):
    async def scenario():
        db.put("project", {"id": "project", "name": "Наш проект", "enabled": True,
                           "description": "Пишем коротко для владельцев кафе.",
                           "agent_ids": ["a"], "private_future_setting": "must-not-be-sent"})
        db.put(parent_kind, {"id": "parent", "project_id": "project", "status": "running"})
        captured = []

        async def complete(provider, messages, **kwargs):
            captured.extend(messages)
            return response()

        result = await runtime.run({"id": "a"}, {"id": "p"},
                                   [{"role": "user", "content": "Подготовь текст"}],
                                   100, complete_fn=complete, **{parent_kind + "_id": "parent"})
        assert not result.get("error")
        context = next(m for m in captured if "Пишем коротко для владельцев кафе." in m["content"])
        assert context["role"] == "user"
        assert "Наш проект" in context["content"]
        assert "must-not-be-sent" not in json.dumps(captured)
        assert captured[-1]["content"] == "Подготовь текст"

    asyncio.run(scenario())


def prepared_project_plan(engine):
    db.put("project", {"id": "project", "enabled": True, "agent_ids": [], "skill_ids": []})
    task = engine.create_task({"title": "Проверка", "brief": "Подготовь текст", "project_id": "project"})
    task = db.update("task", task["id"], {"status": "planning"})
    plan = engine._prepare_plan(task, {"agents": [{"id": "author", "name": "Автор", "role": "Автор",
        "instructions": "Подготовь ответ", "provider_id": "p"}],
        "steps": [{"id": "s1", "name": "Ответ", "agent_id": "author", "depends_on": []}]})
    return task, plan


def test_plan_commit_is_atomic_if_saving_task_fails(monkeypatch):
    engine = Engine()
    task, prepared = prepared_project_plan(engine)
    original_put = db._put

    def fail_task_write(kind, document):
        if kind == "task" and document["id"] == task["id"]:
            raise RuntimeError("simulated storage interruption")
        return original_put(kind, document)

    monkeypatch.setattr(db, "_put", fail_task_write)
    with pytest.raises(RuntimeError):
        engine._commit_plan(task["id"], prepared)
    assert db.list("agent") == []
    assert db.get("project", "project")["agent_ids"] == []
    assert db.get("task", task["id"])["status"] == "planning"


def test_stale_project_plan_does_not_create_orphaned_agents():
    engine = Engine()
    task, prepared = prepared_project_plan(engine)
    db.update("project", "project", {"enabled": False})
    with pytest.raises(EngineError):
        engine._commit_plan(task["id"], prepared)
    assert db.list("agent") == []
