"""Owner-approved remote MCP calls. No shell, local network or implicit authority."""
import asyncio
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource
from app.db import db, uid, now
from app.net import request, _url_host
from app.security import decrypt

PROTOCOL = '2025-11-25'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sanitize(server):
    return {**{k: v for k, v in server.items() if k != 'token_encrypted'}, 'has_token': bool(server.get('token_encrypted'))}


def validate_url(url):
    _url_host(url)
    if urlsplit(url).query:
        raise ValueError('Секреты и параметры в URL MCP запрещены. Используйте отдельное поле токена.')
    return url


def validate_schema(schema):
    if not isinstance(schema, dict) or len(json.dumps(schema)) > 30000:
        raise ValueError('Схема инструмента слишком велика или некорректна.')
    def walk(v, depth=0):
        if depth > 32: raise ValueError('Слишком глубокая схема.')
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ('$ref', '$dynamicRef') and (not isinstance(x,str) or not x.startswith('#')):
                    raise ValueError('Внешние ссылки JSON Schema не разрешены.')
                walk(x, depth+1)
        elif isinstance(v, list):
            for x in v: walk(x, depth+1)
    walk(schema)
    Draft202012Validator.check_schema(schema)


def check_arguments(schema, arguments):
    validate_schema(schema)
    if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 40000:
        raise ValueError('Аргументы должны быть JSON-объектом до 40 000 символов.')
    def reject(uri): raise NoSuchResource(ref=uri)
    try:
        errors = list(Draft202012Validator(schema, registry=Registry(retrieve=reject)).iter_errors(arguments))
        if errors: raise ValueError('Аргументы не соответствуют схеме инструмента.')
    except ValueError: raise
    except Exception: raise ValueError('Не удалось проверить схему инструмента.') from None


class MCP:
    def __init__(self, server):
        self.server = server
        self.headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if server.get('token_encrypted'):
            self.headers['Authorization'] = 'Bearer ' + decrypt(server['token_encrypted'])
        self.counter = 0

    async def rpc(self, method, params=None, notify=False):
        self.counter += 1
        body = {'jsonrpc':'2.0','method':method,'params':params or {}}
        if not notify: body['id'] = self.counter
        response = await request('POST', self.server['url'], headers=self.headers, json=body, timeout=50, max_bytes=1_000_000)
        sid = response.headers.get('mcp-session-id')
        if sid:
            if len(sid)>512 or any(ord(c)<33 or ord(c)>126 for c in sid): raise ValueError('Некорректная сессия MCP.')
            self.headers['Mcp-Session-Id'] = sid
        if notify: return {}
        try:
            if 'text/event-stream' in response.headers.get('content-type',''):
                entries=[]
                for event in response.text.replace('\r\n','\n').split('\n\n'):
                    data='\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                    if data: entries.append(json.loads(data))
                data=next(x for x in entries if x.get('id')==body['id'])
            else: data=response.json()
            if data.get('id')!=body['id'] or data.get('jsonrpc')!='2.0' or 'error' in data or not isinstance(data.get('result'),dict):
                raise ValueError
            return data['result']
        except Exception: raise ValueError('MCP вернул ошибку или некорректный ответ. Текст сервера скрыт для защиты секретов.') from None

    async def initialize(self):
        result=await self.rpc('initialize',{'protocolVersion':PROTOCOL,'capabilities':{},'clientInfo':{'name':'agentnaya','version':'2.0.0'}})
        version=result.get('protocolVersion')
        if version not in (PROTOCOL,'2025-06-18','2025-03-26'): raise ValueError('Версия протокола MCP пока не поддерживается.')
        self.headers['MCP-Protocol-Version']=version
        await self.rpc('notifications/initialized',notify=True)

    async def tools(self):
        out=[]; cursor=None; seen=set()
        for _ in range(10):
            result=await self.rpc('tools/list', {'cursor':cursor} if cursor else {})
            batch=result.get('tools')
            if not isinstance(batch,list): raise ValueError('MCP не вернул список инструментов.')
            for item in batch:
                if not isinstance(item,dict) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}',str(item.get('name',''))): raise ValueError('Некорректное имя инструмента MCP.')
                if item['name'] in seen: raise ValueError('Повторяющиеся имена инструментов MCP.')
                seen.add(item['name']); validate_schema(item.get('inputSchema'))
                out.append({'name':item['name'],'description':str(item.get('description',''))[:4000], 'inputSchema':item['inputSchema']})
            cursor=result.get('nextCursor')
            if len(out)>300: raise ValueError('Подключите сервер с каталогом до 300 инструментов.')
            if not cursor: return out
        raise ValueError('Слишком много страниц каталога MCP.')

    async def close(self):
        if self.headers.get('Mcp-Session-Id'):
            try: await request('DELETE', self.server['url'], headers=self.headers, timeout=8, max_bytes=10000)
            except Exception: pass


@asynccontextmanager
async def session(server):
    client=MCP(server)
    try:
        await client.initialize()
        yield client
    finally: await client.close()


async def discover(identifier):
    server=db.get('mcp',identifier)
    if not server or not server.get('enabled'): raise ValueError('MCP-сервер отключён или не найден.')
    async with session(server) as client: tools=await client.tools()
    current=db.get('mcp',identifier)
    if digest(current)!=digest(server): raise ValueError('Настройки изменились во время подключения. Повторите проверку.')
    db.update('mcp',identifier,{'tools':tools,'catalog_hash':digest(tools),'checked_at':now()})
    db.event('', 'Каталог MCP обновлён. Разрешения на инструменты назначаются отдельно.', mcp_id=identifier)
    return sanitize(db.get('mcp',identifier))


def available(agent):
    result=[]
    for grant in agent.get('tool_grants',[]):
        server=db.get('mcp',grant['server_id'])
        if not server or not server.get('enabled'): continue
        tool=next((t for t in server.get('tools',[]) if t['name']==grant['tool']),None)
        if tool: result.append({'server_id':server['id'],**tool})
    return result


def require_permission(agent_id, server_id, name):
    agent=db.get('agent',agent_id); server=db.get('mcp',server_id)
    if not agent or not agent.get('enabled') or not server or not server.get('enabled'):
        raise ValueError('Агент или MCP-сервер отключён.')
    if not any(g['server_id']==server_id and g['tool']==name for g in agent.get('tool_grants',[])):
        raise ValueError('Агенту не разрешён этот инструмент.')
    tool=next((t for t in server.get('tools',[]) if t['name']==name),None)
    if not tool: raise ValueError('Инструмент отсутствует в проверенном каталоге.')
    return agent,server,tool


def binding(agent,server,tool):
    return digest({'agent':agent,'server':server,'tool':tool})


def redact_token(value, token):
    if isinstance(value, str):
        return value.replace(token, '[секрет скрыт]') if token else value
    if isinstance(value, list):
        return [redact_token(item, token) for item in value]
    if isinstance(value, dict):
        return {redact_token(key, token): redact_token(item, token) for key, item in value.items()}
    return value


def finish_error(identifier, sent, message):
    def change(record):
        # Keep an owner's rejection made while the network check was pending.
        if not sent and record['status'] == 'rejected':
            return None
        return {**record, 'status': 'uncertain' if sent else 'failed', 'error': message, 'finished_at': now()}
    return db.mutate('tool_request', identifier, change)


def propose(agent_id,server_id,name,arguments,*,task_id='',chat_id=''):
    agent,server,tool=require_permission(agent_id,server_id,name)
    check_arguments(tool['inputSchema'],arguments)
    record=db.put('tool_request',{'id':uid(),'agent_id':agent_id,'server_id':server_id,'tool':name,'arguments':arguments,
        'task_id':task_id,'chat_id':chat_id,'status':'pending','expires_at':time.time()+1800,
        'binding':binding(agent,server,tool),'result':'','error':''})
    db.event(task_id,'Агент запросил инструмент. Ожидается одобрение владельца.',tool_request_id=record['id'])
    return record


def live_parent(record):
    if record.get('task_id'):
        parent=db.get('task',record['task_id'])
        if not parent or parent['status']!='running': raise ValueError('Задача уже не выполняется.')
    if record.get('chat_id'):
        parent=db.get('chat',record['chat_id'])
        if not parent or parent.get('status')!='running': raise ValueError('Чат уже не выполняется.')


async def execute(identifier):
    record=db.get('tool_request',identifier)
    if not record or record['status']!='pending' or record['expires_at']<time.time(): raise ValueError('Запрос уже обработан или истёк.')
    live_parent(record)
    agent,server,tool=require_permission(record['agent_id'],record['server_id'],record['tool'])
    if binding(agent,server,tool)!=record['binding']: raise ValueError('Права или настройки изменились. Нужно новое предложение агента.')
    # Reserve before any await: double clicks cannot execute twice.
    db.update('tool_request',identifier,{'status':'checking'})
    sent=False
    try:
        async with session(server) as client:
            fresh=await client.tools()
            if fresh!=server.get('tools',[]): raise ValueError('Каталог изменился на сервере. Обновите каталог и разрешения.')
            latest=db.get('tool_request',identifier)
            a,s,t=require_permission(record['agent_id'],record['server_id'],record['tool'])
            if binding(a,s,t)!=record['binding'] or latest['status']!='checking' or latest['expires_at']<time.time(): raise ValueError('Разрешение отозвано во время проверки.')
            live_parent(record)
            db.update('tool_request',identifier,{'status':'executing','approved_at':now()})
            sent=True
            result=await client.rpc('tools/call',{'name':record['tool'],'arguments':record['arguments']})
        # Redact our own token even if a misconfigured server echoes it.
        token=decrypt(server.get('token_encrypted',''))
        text=json.dumps(redact_token(result,token),ensure_ascii=False)
        text=text[:60000]
        status='failed' if result.get('isError') else 'done'
        db.update('tool_request',identifier,{'status':status,'result':text,'finished_at':now()})
    except asyncio.CancelledError:
        finish_error(identifier,sent,'Процесс прерван. Автоматического повтора нет.')
        raise
    except Exception:
        finish_error(identifier,sent,'Результат не подтверждён. Проверьте внешний сервис; автоматического повтора нет.' if sent else 'Проверка не пройдена: изменились права, каталог или связь с сервером.')
    db.event(record.get('task_id',''),'Запрос MCP завершён.',tool_request_id=identifier)
    return db.get('tool_request',identifier)


def reject(identifier):
    def change(record):
        if record['status'] not in ('pending','checking'): raise ValueError('Запрос уже обработан.')
        return {**record,'status':'rejected','error':'Владелец отклонил действие.','finished_at':now()}
    record=db.mutate('tool_request',identifier,change)
    if record is None: raise ValueError('Запрос уже обработан.')
    return record


def recover():
    for r in db.list('tool_request'):
        if r['status'] in ('pending','checking','executing'):
            db.update('tool_request',r['id'],{'status':'uncertain' if r['status']=='executing' else 'rejected','error':'Процесс перезапущен. Требуется новое согласование.'})
