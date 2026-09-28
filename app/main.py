"""HTTP application; single owner and single durable execution process."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.db import db, now, uid
from app.process_lock import lock_file
from app.security import (check_password, create_session, decrypt, demo_mode, encrypt, get_session,
                          login_allowed, public_origin, record_login_failure, sanitize_connector,
                          sanitize_provider, secure_cookies, validate_environment, verify_totp)

STATIC = Path(__file__).parent / 'static'
COOKIE = 'agentos_session'
AUTH_ACTIVE = 0


@asynccontextmanager
async def lifespan(app):
    validate_environment()
    directory = Path(os.getenv('APP_DATA_DIR', 'data'))
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lock = open(directory / 'application.lock', 'a+')
    try:
        lock_file(lock.fileno())
    except BlockingIOError:
        lock.close()
        raise RuntimeError('База уже используется другим процессом. Запускайте приложение с одним worker.')
    db.init(directory / 'agentos.sqlite3')
    os.chmod(directory / 'agentos.sqlite3', 0o600)
    verifier = db.setting('encryption_check')
    if verifier:
        try:
            if decrypt(verifier) != 'agent-os-key-check-v1':
                raise ValueError
        except Exception:
            db.close()
            lock.close()
            raise RuntimeError('MASTER_KEY не подходит к этой базе. Восстановите исходные настройки.') from None
    else:
        db.set_setting('encryption_check', encrypt('agent-os-key-check-v1'))
    auth_revision = hashlib.sha256((os.getenv('ADMIN_USERNAME', 'sergey') + ':' + os.environ['ADMIN_PASSWORD_HASH']).encode()).hexdigest()
    if db.setting('auth_revision') != auth_revision:
        for existing in db.list('session'):
            db.delete('session', existing['id'])
        db.set_setting('auth_revision', auth_revision)
    # Restart must never replay an external publication with unknown outcome.
    for item in db.list('publication'):
        if item['status'] == 'approved' and not item.get('content_fingerprint'):
            db.update('publication', item['id'], {'status': 'draft', 'approved_at': None})
        if item['status'] == 'publishing':
            db.update('publication', item['id'], {'status': 'uncertain', 'error': 'Процесс прерван во время публикации. Проверьте соцсеть перед повторной отправкой.'})
    for session in db.list('session'):
        if session['expires_at'] < time.time():
            db.delete('session', session['id'])
    if demo_mode() and not db.list('provider'):
        db.put('provider', {'id': uid(), 'name': 'Демо · без внешних API', 'kind': 'mock', 'model': 'demo', 'base_url': '', 'enabled': True})
    from app.engine import engine
    from app.integrations import telegram_loop
    from app.workflow import Workflow
    app.state.engine = engine
    app.state.stop_event = asyncio.Event()
    await engine.start()
    app.state.workflow = Workflow(engine)
    intake = asyncio.create_task(app.state.workflow.loop(app.state.stop_event))
    telegram = asyncio.create_task(telegram_loop(app.state.stop_event, engine, app.state.workflow))
    from app.workspace import Workspace
    app.state.workspace = Workspace(engine)
    workspace_job = asyncio.create_task(app.state.workspace.loop(app.state.stop_event))
    try:
        yield
    finally:
        app.state.stop_event.set()
        telegram.cancel()
        intake.cancel()
        workspace_job.cancel()
        await app.state.workspace.stop()
        await asyncio.gather(telegram, intake, workspace_job, return_exceptions=True)
        await engine.stop()
        db.close()
        lock.close()


app = FastAPI(title='Агентная', version='2.0.1', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def boundary(request: Request, call_next):
    origin = public_origin()
    expected_host = urlparse(origin).netloc.lower()
    if request.headers.get('host', '').lower() != expected_host:
        return JSONResponse({'detail': 'Недопустимый Host'}, 400)
    if request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
        if request.headers.get('origin') != origin:
            return JSONResponse({'detail': 'Источник запроса не разрешён'}, 403)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'Некорректная длина запроса'}, 400)
        if length > 1_048_576:
            return JSONResponse({'detail': 'Слишком большой запрос'}, 413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1_048_576:
                return JSONResponse({'detail': 'Слишком большой запрос'}, 413)
            chunks.append(chunk)
        body = b''.join(chunks)
        request._body = body
        if body and 'application/json' not in request.headers.get('content-type', ''):
            return JSONResponse({'detail': 'Ожидается application/json'}, 415)
    if request.url.path.startswith('/api/'):
        request.state.session = get_session(request.cookies.get(COOKIE))
        public = request.url.path in ('/api/health', '/api/auth/login', '/api/auth/session')
        if not public and not request.state.session:
            return JSONResponse({'detail': 'Требуется вход'}, 401)
        if not public and request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            if not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), request.state.session['csrf_token'].encode()):
                return JSONResponse({'detail': 'Некорректный CSRF-токен. Обновите страницу.'}, 403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    if secure_cookies():
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, exc):
    return JSONResponse({'detail': '; '.join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())}, 422)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(StrictModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)
    totp: str = Field(default='', max_length=6)


class ProviderInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    base_url: str = Field(default='', max_length=2048)
    model: str = Field(min_length=1, max_length=150)
    api_key: str = Field(default='', max_length=4096)
    enabled: bool = True


class ToolGrant(StrictModel):
    server_id: str = Field(min_length=1,max_length=100)
    tool: str = Field(min_length=1,max_length=128)


class AgentInput(StrictModel):
    skill_ids: list[str] = Field(default_factory=list, max_length=100)
    mask_id: str = Field(default='', max_length=100)
    tool_grants: list[ToolGrant] = Field(default_factory=list, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(default='', max_length=200)
    instructions: str = Field(min_length=1, max_length=30000)
    provider_id: str = Field(min_length=1, max_length=100)
    model: str = Field(default='', max_length=150)
    temperature: float = Field(default=.3, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=128, le=32000)
    enabled: bool = True


class TaskInput(StrictModel):
    project_id: str = Field(default='', max_length=100)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=30000)
    planner_provider_id: str = Field(default='', max_length=100)
    auto_run: bool = False
    max_steps: int = Field(default=8, ge=1, le=16)
    max_output_tokens: int = Field(default=24000, ge=1024, le=200000)


class IdeaInput(StrictModel):
    text: str = Field(min_length=1, max_length=16000)
    connector_id: str = Field(min_length=1, max_length=100)


class IntakeDecision(StrictModel):
    revision: int = Field(ge=1, strict=True)


class IntakeRevision(IntakeDecision):
    feedback: str = Field(min_length=1, max_length=4000)


class ConnectorInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    config: dict = Field(default_factory=dict)
    secrets: dict = Field(default_factory=dict)
    enabled: bool = True


class PublicationInput(StrictModel):
    task_id: str = Field(default='', max_length=100)
    connector_id: str = Field(min_length=1, max_length=100)
    text: str = Field(default='', max_length=40000)
    media_url: str = Field(default='', max_length=2048)
    title: str = Field(default='', max_length=200)


def require(kind, identifier):
    row = db.get(kind, identifier)
    if row is None:
        raise HTTPException(404, 'Не найдено')
    return row


def validate_model(cls, data):
    try:
        return cls.model_validate(data).model_dump()
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ValueError('; '.join(errors)) from None


async def patch_data(request, model, old, *, hidden=()):
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError('Ожидается JSON-объект')
    fields = set(model.model_fields)
    if set(data) - fields:
        raise ValueError('Неизвестные поля: ' + ', '.join(set(data) - fields))
    combined = {k: v for k, v in old.items() if k in fields and k not in hidden}
    combined.update(data)
    return validate_model(model, combined), data


@app.get('/api/health')
async def health():
    return {'status': 'ok'}


@app.get('/api/auth/session')
async def session(request: Request):
    s = request.state.session
    return {'authenticated': bool(s), 'user': s['user'] if s else None, 'csrf_token': s['csrf_token'] if s else None, 'demo_mode': demo_mode()}


@app.post('/api/auth/login')
async def login(data: Login, request: Request):
    global AUTH_ACTIVE
    ip = request.client.host if request.client else 'unknown'
    if not login_allowed(ip) or AUTH_ACTIVE >= 2:
        raise HTTPException(429, 'Слишком много попыток входа. Повторите позже.')
    record_login_failure(ip)  # Reserve attempt before slow password hashing.
    AUTH_ACTIVE += 1
    try:
        password_ok = await run_in_threadpool(check_password, data.password)
    finally:
        AUTH_ACTIVE -= 1
    username_ok = hmac.compare_digest(data.username.encode(), os.getenv('ADMIN_USERNAME', 'sergey').encode())
    totp_secret = db.setting('totp_secret')
    valid = username_ok and password_ok
    if valid and totp_secret:
        valid = verify_totp(decrypt(totp_secret), data.totp)
    if not valid:
        raise HTTPException(401, 'Неверные данные входа или код 2FA')
    for old in db.list('session'):
        if old['expires_at'] < time.time():
            db.delete('session', old['id'])
    token, csrf = create_session()
    response = JSONResponse({'authenticated': True, 'user': os.getenv('ADMIN_USERNAME', 'sergey'), 'csrf_token': csrf, 'demo_mode': demo_mode()})
    response.set_cookie(COOKIE, token, max_age=43200, httponly=True, secure=secure_cookies(), samesite='strict', path='/')
    db.event('', 'Выполнен вход владельца')
    return response


@app.post('/api/auth/logout')
async def logout(request: Request):
    db.delete('session', request.state.session['id'])
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE, path='/', secure=secure_cookies(), httponly=True, samesite='strict')
    return response


@app.get('/api/state')
async def state():
    from app.toolbus import sanitize
    return {'projects':db.list('project'), 'skills':db.list('skill'), 'masks':db.list('mask'),
            'mcp_servers':[sanitize(x) for x in db.list('mcp')],
            'chats':[{k:v for k,v in c.items() if k!='messages'} for c in db.list('chat')],
            'automations':db.list('automation'), 'tool_requests':db.list('tool_request')[:200],
            'decision':sanitize(db.get('decision','jev') or {}), 'crm':sanitize(db.get('crm','twenty') or {}),
            'tasks': db.list('task'), 'agents': db.list('agent'),
            'providers': [sanitize_provider(p) for p in db.list('provider')],
            'connectors': [sanitize_connector(c) for c in db.list('connector')],
            'events': db.list('event')[:200], 'publications': db.list('publication'),
            'settings': {'demo_mode': demo_mode(), 'telegram_configured': any(c.get('enabled') and c['kind'] == 'telegram' and c.get('config', {}).get('controller_enabled', True) and c.get('config', {}).get('allowed_user_ids') for c in db.list('connector'))}}


async def validate_provider(data):
    if data['kind'] not in ('openai', 'anthropic', 'gemini', 'openai_compatible', 'mock'):
        raise ValueError('Неизвестный тип провайдера')
    if data['kind'] == 'mock' and not demo_mode():
        raise ValueError('Демо-провайдер доступен только при DEMO_MODE=true')
    fixed = {'openai': 'https://api.openai.com/v1', 'anthropic': 'https://api.anthropic.com/v1', 'gemini': 'https://generativelanguage.googleapis.com/v1beta'}
    if data['kind'] in fixed:
        if data['base_url'] and data['base_url'].rstrip('/') != fixed[data['kind']]:
            raise ValueError('Для стандартного провайдера используется только официальный адрес')
        data['base_url'] = fixed[data['kind']]
    elif data['kind'] == 'openai_compatible':
        parsed = urlparse(data['base_url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Укажите публичный HTTPS-адрес API без пароля и параметров URL')
    return data


def save_provider(data, old=None):
    key = data.pop('api_key', '')
    encrypted = encrypt(key) if key else (old or {}).get('api_key_encrypted', '')
    if data['kind'] != 'mock' and not encrypted:
        raise ValueError('Добавьте API-ключ')
    return db.put('provider', {**(old or {}), **data, 'id': old['id'] if old else uid(), 'api_key_encrypted': encrypted})


@app.post('/api/providers')
async def provider_create(data: ProviderInput):
    result = save_provider(await validate_provider(data.model_dump()))
    db.event('', 'Добавлен провайдер', provider_id=result['id'])
    return sanitize_provider(result)


@app.patch('/api/providers/{identifier}')
async def provider_patch(identifier: str, request: Request):
    old = require('provider', identifier)
    data, raw = await patch_data(request, ProviderInput, old, hidden=('api_key',))
    if (data['kind'] != old['kind'] or data['base_url'].rstrip('/') != old.get('base_url', '').rstrip('/')) and not data.get('api_key'):
        raise ValueError('При смене типа или адреса введите новый API-ключ')
    result = save_provider(await validate_provider(data), old)
    db.event('', 'Изменён провайдер', provider_id=identifier)
    return sanitize_provider(result)


@app.delete('/api/providers/{identifier}')
async def provider_delete(identifier: str):
    require('provider', identifier)
    if any(c.get('provider_id')==identifier for c in db.list('chat')) or any(p.get('planner_provider_id')==identifier for p in db.list('project')):
        raise ValueError('Провайдер используется в чате или проекте. Отключите вместо удаления.')
    if (any(a['provider_id'] == identifier for a in db.list('agent'))
            or any(t.get('planner_provider_id') == identifier or t.get('intake', {}).get('provider_id') == identifier for t in db.list('task'))
            or any(identifier in [c.get('config', {}).get(k) for k in ('planner_provider_id', 'intake_provider_id', 'transcription_provider_id')] for c in db.list('connector'))):
        raise ValueError('Провайдер используется агентом или задачей. Отключите его вместо удаления.')
    db.delete('provider', identifier)
    return {'ok': True}


@app.post('/api/providers/{identifier}/test')
async def provider_test(identifier: str):
    from app.providers import test_provider
    return await test_provider(require('provider', identifier))


def validate_agent(data):
    from app.workspace import refs
    from app.toolbus import available
    refs('skill',data['skill_ids'])
    if data['mask_id']: refs('mask',[data['mask_id']])
    for grant in data['tool_grants']:
        server=require('mcp',grant['server_id'])
        if not server.get('enabled') or not any(t['name']==grant['tool'] for t in server.get('tools',[])):
            raise ValueError('Выберите инструмент из проверенного каталога MCP.')
    provider = require('provider', data['provider_id'])
    if not provider.get('enabled'):
        raise ValueError('Провайдер отключён')
    return data


@app.post('/api/agents')
async def agent_create(data: AgentInput):
    return db.put('agent', {'id': uid(), **validate_agent(data.model_dump())})


@app.patch('/api/agents/{identifier}')
async def agent_patch(identifier: str, request: Request):
    old = require('agent', identifier)
    data, raw = await patch_data(request, AgentInput, old)
    if any(t['status'] in ('running', 'planning') and any(s['agent_id'] == identifier for s in t.get('steps', [])) for t in db.list('task')):
        raise ValueError('Сначала приостановите задачу, использующую агента')
    return db.put('agent', {**old, **validate_agent(data)})


@app.delete('/api/agents/{identifier}')
async def agent_delete(identifier: str):
    require('agent', identifier)
    if any(c.get('agent_id')==identifier for c in db.list('chat')) or any(identifier in p.get('agent_ids',[]) for p in db.list('project')):
        raise ValueError('Агент используется в проекте или чате. Отключите вместо удаления.')
    if any(any(s['agent_id'] == identifier for s in t.get('steps', [])) for t in db.list('task')):
        raise ValueError('Агент используется в задаче. Отключите его вместо удаления.')
    db.delete('agent', identifier)
    return {'ok': True}


@app.post('/api/tasks')
async def task_create(data: TaskInput):
    from app.workspace import project_task
    payload = project_task(data.model_dump())
    if payload['planner_provider_id']:
        require('provider', payload['planner_provider_id'])
    return app.state.engine.create_task(payload)


@app.post('/api/ideas')
async def idea_create(data: IdeaInput):
    connector = require('connector', data.connector_id)
    config = connector.get('config', {})
    if connector['kind'] != 'telegram' or not connector.get('enabled', True) or not config.get('controller_enabled', True):
        raise ValueError('Выберите включённый Telegram-контроллер')
    users = config.get('allowed_user_ids', [])
    if not users:
        raise ValueError('Укажите разрешённый Telegram user ID в настройках бота')
    return app.state.workflow.create_idea(data.text, config=config, source='web',
                                         connector_id=connector['id'], user_id=users[0])


@app.post('/api/tasks/{identifier}/intake/approve')
async def intake_approve(identifier: str, data: IntakeDecision):
    require('task', identifier)
    return await app.state.workflow.approve(identifier, data.revision)


@app.post('/api/tasks/{identifier}/intake/revise')
async def intake_revise(identifier: str, data: IntakeRevision):
    require('task', identifier)
    return app.state.workflow.revise(identifier, data.feedback, data.revision)


@app.post('/api/tasks/{identifier}/intake/reject')
async def intake_reject(identifier: str, data: IntakeDecision):
    require('task', identifier)
    return app.state.workflow.reject(identifier, data.revision)


@app.get('/api/tasks/{identifier}')
async def task_get(identifier: str):
    return {'task': require('task', identifier), 'events': [e for e in db.list('event') if e.get('task_id') == identifier][:300]}


@app.get('/api/tasks/{identifier}/export')
async def task_export(identifier: str):
    from app.reports import task_report
    task = require('task', identifier)
    project = db.get('project', task.get('project_id', ''))
    filename = 'task-' + re.sub(r'[^A-Za-z0-9_-]', '_', task['id'])[:100] + '.md'
    return Response(task_report(task, project, db.list('agent')), media_type='text/markdown',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.patch('/api/tasks/{identifier}')
async def task_patch(identifier: str, request: Request):
    raw = await request.json()
    if not isinstance(raw, dict) or set(raw) - {'title', 'brief', 'steps', 'planner_provider_id', 'max_steps', 'max_output_tokens'}:
        raise ValueError('Недопустимые поля задачи')
    def change(task):
        if task.get('intake') and task['intake'].get('state') != 'approved' and set(raw) - {'max_output_tokens'}:
            raise ValueError('Для идеи используйте согласование и внесение правок к предложению')
        if task['status'] not in ('backlog', 'ready', 'paused', 'failed', 'review'):
            raise ValueError('Сейчас задача недоступна для редактирования')
        # A paused task may still have in-flight calls; their outputs belong to the old brief.
        started = any(s.get('attempts', 0) for s in task.get('steps', []))
        if started and any(k in raw and raw[k] != task.get(k) for k in ('brief', 'planner_provider_id', 'max_steps')):
            raise ValueError('После начала выполнения нельзя менять исходное задание и состав плана. Создайте новую задачу.')
        if 'title' in raw and (not isinstance(raw['title'], str) or not 1 <= len(raw['title'].strip()) <= 200):
            raise ValueError('Название: от 1 до 200 символов')
        if 'brief' in raw and (not isinstance(raw['brief'], str) or not 1 <= len(raw['brief'].strip()) <= 30000):
            raise ValueError('Описание: от 1 до 30 000 символов')
        if 'planner_provider_id' in raw:
            provider = require('provider', raw['planner_provider_id'])
            if not provider.get('enabled'):
                raise ValueError('Провайдер отключён')
        if 'max_steps' in raw:
            if type(raw['max_steps']) is not int or not 1 <= raw['max_steps'] <= 16 or raw['max_steps'] < len(raw.get('steps', task.get('steps', []))):
                raise ValueError('Лимит шагов: от текущего числа шагов до 16')
        if 'max_output_tokens' in raw:
            budget = raw['max_output_tokens']
            reserved = sum(s.get('reserved_tokens', 0) for s in task.get('steps', [])) + task.get('planner_reserved_tokens', 0) + task.get('intake', {}).get('reserved_tokens', 0)
            if type(budget) is not int or not 1024 <= budget <= 200000 or budget < task.get('output_tokens_used', 0) + reserved:
                raise ValueError('Бюджет: от уже потраченного и зарезервированного до 200 000 выходных токенов')
        if 'steps' in raw:
            if task['status'] != 'ready' or any(s['status'] != 'pending' for s in task.get('steps', [])):
                raise ValueError('План можно менять только до первого запуска')
            steps = raw['steps']
            if not isinstance(steps, list) or not 1 <= len(steps) <= raw.get('max_steps', task['max_steps']):
                raise ValueError('Недопустимое количество шагов')
            clean = []
            for s in steps:
                if not isinstance(s, dict) or not {'id', 'name', 'agent_id', 'depends_on'} <= set(s):
                    raise ValueError('Некорректный шаг')
                if any(not isinstance(s[k], str) or not 1 <= len(s[k]) <= 200 for k in ('id', 'name', 'agent_id')):
                    raise ValueError('Некорректные поля шага')
                require('agent', s['agent_id'])
                input_text = s.get('input', '')
                if not isinstance(input_text, str) or len(input_text) > 30000:
                    raise ValueError('Слишком длинное задание шага')
                clean.append({'id': s['id'], 'name': s['name'], 'agent_id': s['agent_id'], 'depends_on': s['depends_on'], 'input': input_text, 'status': 'pending', 'output': '', 'error': '', 'attempts': 0, 'tokens_used': 0})
            app.state.engine.validate_steps(clean)
            raw['steps'] = clean
        return {**task, **raw}
    result = db.mutate('task', identifier, change)
    if result is None:
        raise HTTPException(404, 'Не найдено')
    db.event(identifier, 'Параметры задачи изменены владельцем')
    return result


@app.post('/api/tasks/{identifier}/{action}')
async def task_action(identifier: str, action: str):
    if action not in ('plan', 'run', 'pause', 'resume', 'cancel', 'retry'):
        raise HTTPException(404, 'Неизвестное действие')
    require('task', identifier)
    return await app.state.engine.action(identifier, action)


SECRET_FIELDS = {
    'telegram': {'bot_token'}, 'vk': {'access_token'}, 'instagram': {'access_token'},
    'youtube': {'client_secret', 'refresh_token'}, 'webhook': {'bearer_token'},
}


from app.publications import connector_fingerprint
CONFIG_FIELDS = {
    'telegram': {'allowed_user_ids', 'chat_id', 'controller_enabled', 'planner_provider_id', 'intake_provider_id', 'intake_instructions', 'voice_enabled', 'transcription_provider_id', 'transcription_model', 'notifications_enabled', 'max_steps', 'max_output_tokens', 'project_id', 'agent_id', 'mode'}, 'vk': {'owner_id', 'api_version'},
    'instagram': {'account_id', 'api_version', 'media_type'},
    'youtube': {'client_id', 'privacy_status', 'category_id'}, 'webhook': {'url'},
}


def validate_connector(data):
    kind = data['kind']
    if kind not in SECRET_FIELDS:
        raise ValueError('Неизвестная интеграция')
    if set(data['secrets']) - SECRET_FIELDS[kind] or set(data['config']) - CONFIG_FIELDS[kind]:
        raise ValueError('Неизвестные параметры интеграции')
    if any(not isinstance(v, str) or len(v) > 10000 for v in data['secrets'].values()):
        raise ValueError('Некорректный секрет')
    if kind == 'telegram':
        config = data['config']
        from app.workspace import refs
        if config.get('mode','ideas') not in ('ideas','chat'): raise ValueError('Режим бота: идеи или чат.')
        for k,entity in (('project_id','project'),('agent_id','agent')):
            if config.get(k): refs(entity,[config[k]])
        if config.get('mode')=='chat' and not config.get('agent_id'): raise ValueError('Для чат-бота выберите агента.')
        if config.get('project_id') and config.get('agent_id') and config['agent_id'] not in require('project',config['project_id'])['agent_ids']:
            raise ValueError('Назначьте агента в выбранный проект.')
        for field in ('controller_enabled', 'voice_enabled', 'notifications_enabled'):
            if field in config and type(config[field]) is not bool:
                raise ValueError('Настройка Telegram должна быть логической: ' + field)
        ids = config.get('allowed_user_ids', [])
        if not isinstance(ids, list) or len(ids) > 10 or any(type(v) is not int or not 0 < v < 2**53 for v in ids):
            raise ValueError('Укажите до 10 числовых Telegram user ID')
        if config.get('controller_enabled', True) and not ids:
            raise ValueError('Для управления Telegram укажите хотя бы один разрешённый user ID')
        chat = config.get('chat_id', '')
        if type(chat) is int and abs(chat) < 2**53:
            chat = str(chat)
            config['chat_id'] = chat
        if not isinstance(chat, str) or chat and not re.fullmatch(r'(?:-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})', chat):
            raise ValueError('Telegram chat_id: число или @имя канала')
        for field in ('planner_provider_id', 'intake_provider_id', 'transcription_provider_id'):
            value = config.get(field, '')
            if not isinstance(value, str) or len(value) > 100:
                raise ValueError('Некорректный провайдер Telegram')
            if value:
                provider = require('provider', value)
                if not provider.get('enabled', True):
                    raise ValueError('Выбранный провайдер отключён')
                if field == 'transcription_provider_id' and provider.get('kind') != 'openai':
                    raise ValueError('Голосовые сообщения: выберите провайдера OpenAI')
        if config.get('voice_enabled') and not config.get('transcription_provider_id'):
            raise ValueError('Для голосовых выберите провайдера распознавания OpenAI')
        model = config.get('transcription_model', 'gpt-4o-mini-transcribe')
        if model not in ('gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1', 'gpt-4o-mini-transcribe-2025-12-15'):
            raise ValueError('Выберите поддерживаемую модель распознавания речи')
        instructions = config.get('intake_instructions', '')
        if not isinstance(instructions, str) or len(instructions) > 8000:
            raise ValueError('Инструкции первого агента: до 8000 символов')
        for field, low, high in (('max_steps', 1, 16), ('max_output_tokens', 1024, 200000)):
            if field in config and (type(config[field]) is not int or not low <= config[field] <= high):
                raise ValueError('Недопустимый лимит Telegram: ' + field)
    if kind == 'webhook':
        parsed = urlparse(str(data['config'].get('url', '')))
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Webhook: публичный HTTPS URL без секретов в строке адреса')
    return data


def save_connector(data, old=None):
    private = json.loads(decrypt(old['secret_encrypted'])) if old and old.get('secret_encrypted') else {}
    private.update({k: v for k, v in data.pop('secrets', {}).items() if v})
    if old and old['kind'] != data['kind']:
        raise ValueError('Создайте новую интеграцию для другого типа')
    if data['kind'] == 'telegram' and data.get('enabled', True) and data.get('config', {}).get('controller_enabled', True):
        token = private.get('bot_token')
        for other in db.list('connector'):
            if (other['id'] != (old or {}).get('id') and other.get('kind') == 'telegram'
                    and other.get('enabled', True) and other.get('config', {}).get('controller_enabled', True)
                    and token and json.loads(decrypt(other.get('secret_encrypted', '')) or '{}').get('bot_token') == token):
                raise ValueError('Этот бот уже используется другим контроллером. Для канала выключите управление или используйте текущее подключение.')
    return db.put('connector', {**(old or {}), **data, 'id': old['id'] if old else uid(), 'secret_encrypted': encrypt(json.dumps(private))})


@app.post('/api/connectors')
async def connector_create(data: ConnectorInput):
    result = save_connector(validate_connector(data.model_dump()))
    db.event('', 'Добавлена интеграция', connector_id=result['id'])
    return sanitize_connector(result)


@app.patch('/api/connectors/{identifier}')
async def connector_patch(identifier: str, request: Request):
    old = require('connector', identifier)
    data, raw = await patch_data(request, ConnectorInput, old, hidden=('secrets',))
    result = save_connector(validate_connector(data), old)
    db.event('', 'Изменена интеграция', connector_id=identifier)
    return sanitize_connector(result)


@app.delete('/api/connectors/{identifier}')
async def connector_delete(identifier: str):
    require('connector', identifier)
    if any(p['connector_id'] == identifier for p in db.list('publication')):
        raise ValueError('Интеграция используется публикацией. Отключите её вместо удаления.')
    db.delete('connector', identifier)
    return {'ok': True}


@app.post('/api/connectors/{identifier}/test')
async def connector_test(identifier: str):
    from app.integrations import test_connector
    return await test_connector(require('connector', identifier))


@app.post('/api/publications')
async def publication_create(data: PublicationInput):
    from app.publications import create_publication
    return create_publication(data.model_dump())


@app.patch('/api/publications/{identifier}')
async def publication_patch(identifier: str, request: Request):
    from app.publications import patch_publication
    old = require('publication', identifier)
    data, raw = await patch_data(request, PublicationInput, old)
    return patch_publication(identifier, data)


def publication_version(request):
    version = request.headers.get('x-resource-version', '')
    if not version or len(version) > 100:
        raise ValueError('Откройте актуальную карточку публикации и подтвердите её заново.')
    return version


@app.post('/api/publications/{identifier}/approve')
async def publication_approve(identifier: str, request: Request):
    from app.publications import approve_publication
    require('publication', identifier)
    return approve_publication(identifier, expected_updated_at=publication_version(request))


@app.post('/api/publications/{identifier}/publish')
async def publication_publish(identifier: str, request: Request):
    from app.publications import publish_publication
    require('publication', identifier)
    return await publish_publication(identifier, expected_updated_at=publication_version(request))


@app.post('/api/publications/{identifier}/reject')
async def publication_reject(identifier: str):
    from app.publications import reject_publication
    require('publication', identifier)
    return reject_publication(identifier)


@app.delete('/api/publications/{identifier}')
async def publication_delete(identifier: str):
    p = require('publication', identifier)
    if p['status'] != 'draft':
        raise ValueError('Удалить можно только черновик')
    db.delete('publication', identifier)
    return {'ok': True}


@app.get('/api/settings')
async def settings():
    return {'demo_mode': demo_mode(), 'totp_enabled': bool(db.setting('totp_secret'))}


@app.post('/api/settings/totp/setup')
async def totp_setup(request: Request):
    if db.setting('totp_secret'):
        raise ValueError('2FA уже включена')
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
    db.set_setting('totp_pending', {'secret': encrypt(secret), 'expires': time.time() + 600, 'session_id': request.state.session['id']})
    username = os.getenv('ADMIN_USERNAME', 'sergey')
    return {'secret': secret, 'uri': f'otpauth://totp/AgentOS:{quote(username)}?secret={secret}&issuer=AgentOS&algorithm=SHA1&digits=6&period=30'}


class CodeInput(StrictModel):
    code: str = Field(min_length=6, max_length=6)


@app.post('/api/settings/totp/enable')
async def totp_enable(data: CodeInput, request: Request):
    ip = request.client.host if request.client else 'unknown'
    if not login_allowed('2fa:' + ip):
        raise HTTPException(429, 'Слишком много попыток. Повторите позже.')
    record_login_failure('2fa:' + ip)
    if db.setting('totp_secret'):
        raise ValueError('2FA уже включена')
    pending = db.setting('totp_pending')
    if not pending or pending['expires'] < time.time() or pending['session_id'] != request.state.session['id']:
        raise ValueError('Настройка истекла. Начните заново.')
    if not verify_totp(decrypt(pending['secret']), data.code):
        raise ValueError('Неверный код')
    db.set_setting('totp_secret', pending['secret'])
    db.delete('setting', 'totp_pending')
    for s in db.list('session'):
        db.delete('session', s['id'])
    db.event('', 'Включена двухфакторная аутентификация')
    response = JSONResponse({'ok': True, 'totp_enabled': True, 'login_required': True})
    response.delete_cookie(COOKIE, path='/')
    return response


class DisableTOTP(StrictModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    password: str = Field(max_length=1024)
    code: str = Field(min_length=6, max_length=6)


@app.post('/api/settings/totp/disable')
async def totp_disable(data: DisableTOTP, request: Request):
    global AUTH_ACTIVE
    ip = request.client.host if request.client else 'unknown'
    if not login_allowed('2fa:' + ip) or AUTH_ACTIVE >= 2:
        raise HTTPException(429, 'Слишком много попыток. Повторите позже.')
    record_login_failure('2fa:' + ip)
    secret = db.setting('totp_secret')
    AUTH_ACTIVE += 1
    try:
        password_ok = await run_in_threadpool(check_password, data.password)
    finally:
        AUTH_ACTIVE -= 1
    if not secret or not password_ok or not verify_totp(decrypt(secret), data.code):
        raise ValueError('Неверный пароль или код')
    db.delete('setting', 'totp_secret')
    db.delete('setting', 'totp_last_counter')
    for s in db.list('session'):
        db.delete('session', s['id'])
    db.event('', 'Двухфакторная аутентификация отключена', level='warning')
    response = JSONResponse({'ok': True, 'totp_enabled': False, 'login_required': True})
    response.delete_cookie(COOKIE, path='/')
    return response


app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')


@app.get('/')
async def home():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})

# All extension routes use the same authentication/Origin/CSRF boundary.
from app.workspace import router as workspace_router
app.include_router(workspace_router)
