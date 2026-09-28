"""Meaningful state, budget, recovery and transport-contract tests; no live API."""
import asyncio
import importlib
import json

import pytest
from cryptography.fernet import Fernet

from app.db import db
from app.engine import Engine, EngineError, validate_steps
from app.net import NetworkError
from app.providers import ProviderError
from app.security import encrypt

engmod = importlib.import_module("app.engine")
provmod = importlib.import_module("app.providers")


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    db.init(tmp_path / "engine.sqlite3")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("MASTER_KEY", Fernet.generate_key().decode())
    db.put("provider", {"id": "p", "name": "Demo", "kind": "mock", "enabled": True, "model": "demo"})


async def eventually(predicate, timeout=4):
    start = asyncio.get_running_loop().time()
    while not predicate():
        if asyncio.get_running_loop().time() - start > timeout:
            raise AssertionError("Expected durable state was not reached")
        await asyncio.sleep(.01)


def ready(engine, count=3, *, parallel=False, budget=24000, max_tokens=1000):
    db.put("agent", {"id": "a", "name": "Writer", "role": "Writer", "instructions": "Write the assigned result",
                     "provider_id": "p", "enabled": True, "max_tokens": max_tokens})
    task = engine.create_task({"title": "Test", "brief": "Make a useful draft", "planner_provider_id": "p", "max_output_tokens": budget})
    steps = [{"id": f"s{i}", "name": f"Stage {i}", "agent_id": "a",
              "depends_on": [] if parallel or not i else [f"s{i-1}"],
              "input": "Write section", "status": "pending", "output": "", "error": "", "attempts": 0,
              "tokens_used": 0, "reserved_tokens": 0} for i in range(count)]
    return db.update("task", task["id"], {"status": "ready", "steps": steps})


def response(text="complete", incoming=5, outgoing=7):
    return {"text": text, "model": "test", "usage": {"input_tokens": incoming, "output_tokens": outgoing, "source": "provider"}}


def test_plan_persists_editable_agents_and_runs_mock():
    async def scenario():
        engine = Engine()
        await engine.start()
        try:
            task = engine.create_task({"title": "Контент", "brief": "Подготовь три варианта текста", "planner_provider_id": "p"})
            await engine.action(task["id"], "plan")
            await eventually(lambda: db.get("task", task["id"])["status"] == "ready")
            planned = db.get("task", task["id"])
            assert len(planned["steps"]) == len(db.list("agent")) == 3
            assert all(s["status"] == "pending" for s in planned["steps"])
            first_agent = planned["steps"][0]["agent_id"]
            db.update("agent", first_agent, {"instructions": "Edited by owner"})
            await engine.action(task["id"], "run")
            await eventually(lambda: db.get("task", task["id"])["status"] == "done")
            finished = db.get("task", task["id"])
            assert "ДЕМОНСТРАЦИЯ" in finished["result"]
            assert finished["tokens_used"] == 0
            assert all(s["attempts"] == 1 for s in finished["steps"])
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_parallel_dag_passes_dependencies_and_limits_calls(monkeypatch):
    async def scenario():
        active = peak = 0
        seen = []
        async def fake(provider, messages, **kwargs):
            nonlocal active, peak
            ctx = json.loads(messages[-1]["content"])
            active += 1
            peak = max(peak, active)
            seen.append(ctx)
            await asyncio.sleep(.04)
            active -= 1
            return response(ctx["stage"])
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        task = ready(engine, 5, parallel=True)
        task["steps"][-1]["depends_on"] = [f"s{i}" for i in range(4)]
        db.put("task", task)
        await engine.start()
        try:
            await engine.action(task["id"], "run")
            await eventually(lambda: db.get("task", task["id"])["status"] == "done")
            assert peak == 3
            assert len(seen[-1]["dependency_outputs"]) == 4
            finished = db.get("task", task["id"])
            assert finished["result"] == "## Stage 4\n\nStage 4"
            assert finished["tokens_used"] == 5 * (5 + 7)
            assert finished["output_tokens_used"] == 5 * 7
        finally:
            await engine.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_pause_and_cancel_never_start_downstream(monkeypatch, action):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def fake(provider, messages, **kwargs):
            calls.append(json.loads(messages[-1]["content"])["stage"])
            entered.set()
            await release.wait()
            return response()
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        task = ready(engine, 2)
        await engine.start()
        try:
            await engine.action(task["id"], "run")
            await asyncio.wait_for(entered.wait(), 2)
            await engine.action(task["id"], action)
            release.set()
            await eventually(lambda: db.get("task", task["id"])["steps"][0]["status"] in {"done", "cancelled"})
            assert calls == ["Stage 0"]
            current = db.get("task", task["id"])
            if action == "pause":
                assert current["status"] == "paused" and current["steps"][1]["status"] == "pending"
                await engine.action(task["id"], "resume")
                await eventually(lambda: db.get("task", task["id"])["status"] == "done")
                assert calls == ["Stage 0", "Stage 1"]
            else:
                assert current["status"] == "cancelled" and current["steps"][1]["status"] == "cancelled"
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_retry_keeps_completed_outputs(monkeypatch):
    async def scenario():
        failed = False
        async def fake(provider, messages, **kwargs):
            nonlocal failed
            stage = json.loads(messages[-1]["content"])["stage"]
            if stage == "Stage 1" and not failed:
                failed = True
                raise ProviderError("Temporary safe error")
            return response(stage)
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        task = ready(engine, 2)
        await engine.start()
        try:
            await engine.action(task["id"], "run")
            await eventually(lambda: db.get("task", task["id"])["status"] == "failed")
            await engine.action(task["id"], "retry")
            await eventually(lambda: db.get("task", task["id"])["status"] == "done")
            steps = db.get("task", task["id"])["steps"]
            assert [s["attempts"] for s in steps] == [1, 2]
            assert steps[0]["output"] == "Stage 0"
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_parallel_reservations_do_not_overspend(monkeypatch):
    async def scenario():
        allowances = []
        async def fake(provider, messages, **kwargs):
            allowances.append(kwargs["max_tokens"])
            await asyncio.sleep(.03)
            return response(outgoing=kwargs["max_tokens"])
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        task = ready(engine, 5, parallel=True, budget=256, max_tokens=100)
        await engine.start()
        try:
            await engine.action(task["id"], "run")
            await eventually(lambda: db.get("task", task["id"])["status"] == "failed")
            current = db.get("task", task["id"])
            assert sum(allowances) == current["output_tokens_used"] == 256
            assert len(allowances) == 3
            assert sum(s["status"] == "done" for s in current["steps"]) == 3
            assert all(s["reserved_tokens"] == 0 for s in current["steps"])
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_restart_requeues_only_interrupted_steps(monkeypatch):
    async def scenario():
        async def fake(*args, **kwargs):
            return response("recovered")
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        task = ready(engine, 2)
        task["status"] = "running"
        task["steps"][0].update({"status": "done", "output": "keep me", "attempts": 1})
        task["steps"][1].update({"status": "running", "reserved_tokens": 100, "attempts": 1})
        db.put("task", task)
        await engine.start()
        try:
            await eventually(lambda: db.get("task", task["id"])["status"] == "done")
            current = db.get("task", task["id"])
            assert current["steps"][0]["output"] == "keep me"
            assert current["steps"][1]["attempts"] == 2
            assert current["output_tokens_used"] == 107
            assert current["usage_uncertain"] is True
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_planner_rejects_unconfigured_provider_and_persists_no_agents(monkeypatch):
    async def scenario():
        async def fake(*args, **kwargs):
            return response(json.dumps({"agents": [{"id": "a", "name": "Bad", "role": "Bad", "instructions": "Bad",
                                                   "provider_id": "secret-attacker"}],
                                        "steps": [{"id": "s", "name": "Bad", "agent_id": "a", "depends_on": []}]}))
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        await engine.start()
        try:
            task = engine.create_task({"title": "Test", "brief": "Draft", "planner_provider_id": "p"})
            await engine.action(task["id"], "plan")
            await eventually(lambda: db.get("task", task["id"])["status"] == "failed")
            assert db.list("agent") == []
            assert db.get("task", task["id"])["output_tokens_used"] == 7
        finally:
            await engine.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("steps", [
    [],
    [{"id": "s", "name": "X", "agent_id": "a", "depends_on": ["s"]}],
    [{"id": "s", "name": "X", "agent_id": "a", "depends_on": ["missing"]}],
    [{"id": "s", "name": "X", "agent_id": "a"}, {"id": "s", "name": "Y", "agent_id": "a"}],
    [{"id": "a", "name": "X", "agent_id": "a", "depends_on": ["b"]},
     {"id": "b", "name": "X", "agent_id": "a", "depends_on": ["a"]}],
])
def test_invalid_dag_rejected(steps):
    with pytest.raises(EngineError):
        validate_steps(steps)


def test_mock_must_be_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    with pytest.raises(ProviderError):
        asyncio.run(provmod.complete({"kind": "mock"}, [{"role": "user", "content": "test"}]))


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini", "openai_compatible"])
def test_real_adapters_build_protocol_and_redact_key(monkeypatch, kind):
    key = "super-secret-provider-key"
    captured = []
    async def network(method, url, **kwargs):
        captured.append((url, kwargs))
        if kind in {"openai", "openai_compatible"}:
            return {"choices": [{"message": {"content": "OK " + key}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
        if kind == "anthropic":
            return {"content": [{"type": "text", "text": "OK " + key}], "usage": {"input_tokens": 2, "output_tokens": 3}}
        return {"candidates": [{"content": {"parts": [{"text": "OK " + key}]}}],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2, "thoughtsTokenCount": 1}}
    monkeypatch.setattr(provmod, "request_json", network)
    provider = {"kind": kind, "model": "test-model", "api_key_encrypted": encrypt(key), "enabled": True,
                "base_url": "https://example.com/v1" if kind == "openai_compatible" else ""}
    result = asyncio.run(provmod.complete(provider, [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Question"}], max_tokens=100))
    assert result["usage"]["output_tokens"] == 3
    assert key not in result["text"] and "[REDACTED]" in result["text"]
    assert key not in captured[0][0] and key not in json.dumps(captured[0][1]["json"])
    if kind == "openai":
        assert captured[0][1]["json"]["max_completion_tokens"] == 100
    if kind == "anthropic":
        assert captured[0][1]["json"]["system"] == "Rules"
    if kind == "gemini":
        assert captured[0][1]["json"]["generationConfig"]["maxOutputTokens"] == 100


@pytest.mark.parametrize("status,expected", [(401, 1), (429, 3), (503, 3), (None, 1)])
def test_provider_retries_only_rate_limits_and_server_failures(monkeypatch, status, expected):
    calls = []
    async def network(*args, **kwargs):
        calls.append(True)
        raise NetworkError("sensitive body MUST NOT ESCAPE", status)
    async def no_sleep(*args):
        return None
    monkeypatch.setattr(provmod, "request_json", network)
    monkeypatch.setattr(provmod.asyncio, "sleep", no_sleep)
    provider = {"kind": "openai", "model": "test-model", "api_key_encrypted": encrypt("secret")}
    with pytest.raises(ProviderError) as exc:
        asyncio.run(provmod.complete(provider, [{"role": "user", "content": "test"}]))
    assert len(calls) == expected
    assert "sensitive" not in str(exc.value)


def test_empty_model_reply_keeps_usage_for_budget(monkeypatch):
    async def network(*args, **kwargs):
        return {"choices": [{"message": {"content": None}}], "usage": {"prompt_tokens": 10, "completion_tokens": 100}}
    monkeypatch.setattr(provmod, "request_json", network)
    provider = {"kind": "openai", "model": "test-model", "api_key_encrypted": encrypt("secret")}
    result = asyncio.run(provmod.complete(provider, [{"role": "user", "content": "test"}]))
    assert result["error"] and result["usage"]["output_tokens"] == 100


def test_truncated_reply_is_not_treated_as_completed_work(monkeypatch):
    async def network(*args, **kwargs):
        return {"choices": [{"message": {"content": "Partial result"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 100}}
    monkeypatch.setattr(provmod, "request_json", network)
    provider = {"kind": "openai", "model": "test-model", "api_key_encrypted": encrypt("secret")}
    result = asyncio.run(provmod.complete(provider, [{"role": "user", "content": "test"}]))
    assert result["error"] and result["text"] == "Partial result"
    assert result["usage"]["output_tokens"] == 100


def test_cancel_during_planning_never_commits_agents(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def fake(*args, **kwargs):
            entered.set()
            await release.wait()
            return response(json.dumps({"agents": [{"id": "a1", "name": "Agent", "role": "Writer", "instructions": "Write", "provider_id": "p"}],
                                        "steps": [{"id": "s1", "name": "Stage", "agent_id": "a1", "depends_on": []}]}))
        monkeypatch.setattr(engmod, "complete", fake)
        engine = Engine()
        await engine.start()
        try:
            task = engine.create_task({"title": "Test", "brief": "Draft", "planner_provider_id": "p", "auto_run": True})
            await engine.action(task["id"], "plan")
            await asyncio.wait_for(entered.wait(), 2)
            await engine.action(task["id"], "cancel")
            release.set()
            await eventually(lambda: db.get("task", task["id"]).get("planner_reserved_tokens") == 0)
            assert db.get("task", task["id"])["status"] == "cancelled"
            assert db.get("task", task["id"])["output_tokens_used"] == 7
            assert db.list("agent") == []
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_shutdown_and_restart_retains_durable_reservation(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(engmod, "complete", pending)
        first = Engine()
        task = ready(first, 1, max_tokens=100)
        await first.start()
        await first.action(task["id"], "run")
        await asyncio.wait_for(entered.wait(), 2)
        await first.stop()
        assert db.get("task", task["id"])["steps"][0]["reserved_tokens"] == 100
        async def recovered(*args, **kwargs):
            return response("Recovered response")
        monkeypatch.setattr(engmod, "complete", recovered)
        second = Engine()
        await second.start()
        try:
            await eventually(lambda: db.get("task", task["id"])["status"] == "done")
            current = db.get("task", task["id"])
            assert current["output_tokens_used"] == 107 and current["usage_uncertain"]
            assert current["steps"][0]["attempts"] == 2
        finally:
            await second.stop()
    asyncio.run(scenario())
