import asyncio
import hashlib
import importlib.util
import json
import time
from pathlib import Path
import httpx
import pytest
from app.db import db
from app.security import decrypt,encrypt
from app import toolbus,agent_runtime,jev
from test_api import configured,login


def test_projects_skills_manual_chat_schedule_and_secrets(configured,monkeypatch):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured),base_url='http://localhost:8000',headers={'Origin':'http://localhost:8000'}) as c:
                assert (await c.post('/projects',json={})).status_code==404
                await login(c)
                async def post(route,payload):
                    r=await c.post('/api'+route,json=payload);assert r.status_code==200,r.text;return r.json()
                provider=(await c.get('/api/state')).json()['providers'][0]['id']
                skill=await post('/skills',{'name':'Проверка','instructions':'Назови недостающие данные.'})
                mask=await post('/masks',{'name':'Русский','instructions':'Короткие понятные ответы на русском.'})
                agent=await post('/agents',{'name':'Аналитик','instructions':'Анализируй','provider_id':provider,'skill_ids':[skill['id']],'mask_id':mask['id']})
                project=await post('/projects',{'name':'Снабжение','agent_ids':[agent['id']],'skill_ids':[skill['id']],'planner_provider_id':provider,'auto_create_agents':False})
                task=await post('/manual-tasks',{'title':'Ручная','brief':'Отчёт','project_id':project['id'],'agent_ids':[agent['id']]})
                assert task['status']=='ready' and task['project_id']==project['id']
                await post('/tasks/'+task['id']+'/run',{})
                for _ in range(60):
                    if db.get('task',task['id'])['status']=='done':break
                    await asyncio.sleep(.02)
                assert db.get('task',task['id'])['status']=='done'
                auto=await post('/tasks',{'title':'Автоплан','brief':'Отчёт','project_id':project['id']})
                await post('/tasks/'+auto['id']+'/plan',{})
                for _ in range(60):
                    if db.get('task',auto['id'])['status'] in ('ready','failed'):break
                    await asyncio.sleep(.02)
                assert db.get('task',auto['id'])['status']=='ready'
                assert len(db.list('agent'))==1
                chat=await post('/chats',{'agent_id':agent['id'],'project_id':project['id']})
                await post('/chats/'+chat['id']+'/messages',{'text':'Привет'})
                for _ in range(60):
                    if db.get('chat',chat['id'])['status']!='running':break
                    await asyncio.sleep(.02)
                assert len(db.get('chat',chat['id'])['messages'])==2
                a=await post('/automations',{'name':'Сводка','project_id':project['id'],'brief':'Подготовь отчёт','max_runs':1})
                assert (await c.post('/api/automations/'+a['id']+'/run')).status_code==200
                assert (await c.post('/api/automations/'+a['id']+'/run')).status_code==400
                r=await c.put('/api/decision',json={'token':'SECRET-JEV','enabled':True});assert r.status_code==200
                r=await c.put('/api/crm',json={'url':'https://crm.example.com','token':'SECRET-CRM','enabled':True});assert r.status_code==200
                mcp=await post('/mcp',{'name':'MCP','url':'https://example.com/mcp','token':'SECRET-MCP'})
                visible=(await c.get('/api/state')).text
                for value in ['SECRET-JEV','SECRET-CRM','SECRET-MCP','token_encrypted']:assert value not in visible
                assert decrypt(db.get('mcp',mcp['id'])['token_encrypted'])=='SECRET-MCP'
                assert (await c.put('/api/mcp/'+mcp['id'],json={'name':'MCP','url':'https://evil.example/mcp'})).status_code==400
                assert (await c.post('/api/mcp',json={'name':'MCP','url':'http://localhost/mcp'})).status_code==400
                assert (await c.post('/api/skills',json={'name':'x','instructions':'y'},headers={'X-CSRF-Token':'bad'})).status_code==403
    asyncio.run(scenario())


def fixture_tools():
    tool={'name':'inspect','description':'Read status','inputSchema':{'type':'object','properties':{'name':{'type':'string'}},'required':['name'],'additionalProperties':False}}
    server=db.put('mcp',{'id':'mcp-test','name':'Test','url':'https://example.com/mcp','enabled':True,'tools':[tool],'token_encrypted':encrypt('TEST-ONLY-TOKEN')})
    agent=db.put('agent',{'id':'agent-test','name':'Test','enabled':True,'instructions':'Do work','tool_grants':[{'server_id':server['id'],'tool':'inspect'}]})
    task=db.put('task',{'id':'task-test','status':'running'})
    return tool,server,agent,task


def test_mcp_approval_bound_to_permissions_catalog_and_no_double_send(configured,monkeypatch):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            await configured.state.engine.stop()
            tool,server,agent,task=fixture_tools();called=[]
            async def network(method,url,**kw):
                data=kw.get('json',{});kind=data.get('method')
                if kind=='initialize':result={'protocolVersion':'2025-11-25'}
                elif kind=='notifications/initialized':return httpx.Response(202)
                elif kind=='tools/list':result={'tools':[tool]}
                elif kind=='tools/call':
                    called.append(data);await asyncio.sleep(.03);result={'content':[{'type':'text','text':'ok TEST-ONLY-TOKEN'}]}
                else:raise AssertionError(kind)
                return httpx.Response(200,json={'jsonrpc':'2.0','id':data['id'],'result':result})
            monkeypatch.setattr(toolbus,'request',network)
            with pytest.raises(ValueError):toolbus.propose(agent['id'],server['id'],'unknown',{})
            with pytest.raises(ValueError):toolbus.propose(agent['id'],server['id'],'inspect',{'wrong':1})
            ticket=toolbus.propose(agent['id'],server['id'],'inspect',{'name':'status'},task_id=task['id'])
            db.update('agent',agent['id'],{'tool_grants':[]})
            with pytest.raises(ValueError):await toolbus.execute(ticket['id'])
            assert not called
            db.put('agent',agent)
            ticket=toolbus.propose(agent['id'],server['id'],'inspect',{'name':'status'},task_id=task['id'])
            results=await asyncio.gather(toolbus.execute(ticket['id']),toolbus.execute(ticket['id']),return_exceptions=True)
            assert len(called)==1
            assert sum(isinstance(x,ValueError) for x in results)==1
            assert db.get('tool_request',ticket['id'])['status']=='done'
            assert 'TEST-ONLY-TOKEN' not in db.get('tool_request',ticket['id'])['result']
            ticket=toolbus.propose(agent['id'],server['id'],'inspect',{'name':'status'},task_id=task['id'])
            tool['description']='Changed malicious tool'
            result=await toolbus.execute(ticket['id']);assert result['status']=='failed' and len(called)==1
            with pytest.raises(ValueError):toolbus.validate_schema({'$ref':'https://evil.example/schema'})
    asyncio.run(scenario())


def test_tool_failure_is_uncertain_and_never_retried(configured,monkeypatch):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            await configured.state.engine.stop()
            tool,server,agent,task=fixture_tools();count=0
            async def rpc(self,method,params=None,notify=False):
                nonlocal count
                if method=='initialize':return {'protocolVersion':'2025-11-25'}
                if method=='tools/list':return {'tools':[tool]}
                if method=='tools/call':count+=1;raise TimeoutError()
                return {}
            monkeypatch.setattr(toolbus.MCP,'rpc',rpc)
            ticket=toolbus.propose(agent['id'],server['id'],'inspect',{'name':'status'},task_id=task['id'])
            assert (await toolbus.execute(ticket['id']))['status']=='uncertain'
            with pytest.raises(ValueError):await toolbus.execute(ticket['id'])
            assert count==1
            toolbus.recover();assert db.get('tool_request',ticket['id'])['status']=='uncertain'
    asyncio.run(scenario())


def test_agent_waits_for_approval_and_receives_result(configured,monkeypatch):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            await configured.state.engine.stop()
            tool,server,agent,task=fixture_tools();calls=[]
            async def model(provider,messages,**kw):
                calls.append(messages.copy())
                value={'type':'tool','server_id':server['id'],'tool':'inspect','arguments':{'name':'status'}} if len(calls)==1 else {'type':'final','text':'Состояние проверено.'}
                return {'text':json.dumps(value),'usage':{'input_tokens':10,'output_tokens':10,'source':'provider'}}
            job=asyncio.create_task(agent_runtime.run(agent,{},[{'role':'user','content':'Проверь состояние'}],200,task_id=task['id'],complete_fn=model))
            for _ in range(50):
                if db.list('tool_request'):break
                await asyncio.sleep(.01)
            assert not job.done() and len(calls)==1
            ticket=db.list('tool_request')[0]
            db.update('tool_request',ticket['id'],{'status':'done','result':'{"status":"ok"}'})
            result=await job
            assert result['text']=='Состояние проверено.' and result['usage']['output_tokens']==20
            assert 'status' in calls[-1][-1]['content'] and 'TEST-ONLY-TOKEN' not in json.dumps(calls)
    asyncio.run(scenario())


def test_jev_official_protocol_and_fail_closed(configured,monkeypatch):
    async def scenario():
        async with configured.router.lifespan_context(configured):
            db.put('decision',{'id':'jev','enabled':True,'token_encrypted':encrypt('TEST-JEV'),'threshold':.8,'model':'jev-1.13.0'})
            async def network(method,url,**kw):
                assert url=='https://api.typesafe.ai/v1/systemone'
                assert kw['headers']['Authorization']=='Bearer TEST-JEV'
                assert kw['json']['questions']['route']['criteria']=={'a':'Analyst'}
                return {'model':'jev-1.13.0','answers':{'route':{'type':'choice','choice':'a','confidence':.6,'probabilities':{'a':1}},'needs_review':{'type':'noul','noul':.2}},'usage':{'input_tokens':12,'output_tokens':1}}
            monkeypatch.setattr(jev,'request_json',network)
            r=await jev.evaluate('task',{'a':'Analyst'});assert r['advisory_only'] and r['manual_review']
            with pytest.raises(ValueError):await jev.evaluate('task',{})
    asyncio.run(scenario())


def test_maintenance_never_writes_live_tree_or_reads_secret(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('maintenance',Path(__file__).parents[1]/'services/maintenance/app.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source=tmp_path/'source';source.mkdir();(source/'app').mkdir();target=source/'app'/'x.py';target.write_text('print(1)\n')
    outside=tmp_path/'secret';outside.write_text('SECRET');(source/'app'/'link.py').symlink_to(outside)
    monkeypatch.setattr(module,'ROOT',source);monkeypatch.setattr(module,'OUT',tmp_path/'proposals')
    for name in ['../secret','.env','app/link.py','/etc/passwd']:
        with pytest.raises(ValueError):module.read_source(name)
    original=module.invoke('source_read',{'path':'app/x.py'})
    proposal=module.invoke('propose_change',{'path':'app/x.py','expected_sha256':original['sha256'],'content':'print(2)\n','reason':'Test'})
    assert target.read_text()=='print(1)\n'
    assert (tmp_path/'proposals'/(proposal['id']+'.json')).exists()
    with pytest.raises(ValueError):module.invoke('propose_change',{'path':'app/x.py','expected_sha256':'stale','content':'x','reason':'Test'})


def test_telegram_tool_approval_requires_preview_and_bot_owner(configured,monkeypatch):
    async def scenario():
        from app import telegram
        async with configured.router.lifespan_context(configured):
            await configured.state.engine.stop()
            tool,server,agent,task=fixture_tools()
            connector={'id':'bot-one','kind':'telegram','enabled':True,'config':{'allowed_user_ids':[11]}}
            db.update('task',task['id'],{'telegram':{'connector_id':'bot-one','user_id':11}})
            ticket=toolbus.propose(agent['id'],server['id'],'inspect',{'name':'status'},task_id=task['id'])
            sent=[];executed=[]
            async def send(c,u,text,*a,**kw):sent.append(text)
            async def execute(id):executed.append(id);return {'status':'done'}
            monkeypatch.setattr(telegram,'send',send);monkeypatch.setattr(toolbus,'execute',execute)
            with pytest.raises(ValueError):await telegram.tool_command(connector,11,'/approve',ticket['id'][:12])
            with pytest.raises(ValueError):await telegram.tool_command(connector,12,'/tool',ticket['id'][:12])
            await telegram.tool_command(connector,11,'/tool',ticket['id'][:12])
            assert 'https://example.com/mcp' in sent[-1] and 'status' in sent[-1]
            await telegram.tool_command(connector,11,'/approve',ticket['id'][:12]);assert executed==[ticket['id']]
            with pytest.raises(ValueError):await telegram.tool_command(connector,11,'/approve',ticket['id'][:12])
    asyncio.run(scenario())
