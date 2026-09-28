"""Projects, skills, personas, chats, recurring workflows, decision and MCP APIs."""
import asyncio
import json
import re
import shutil
import time
from pathlib import Path
import os
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app.db import db, now, uid
from app.security import encrypt, decrypt
from app import toolbus, jev
from app.agent_runtime import run as run_agent
from app.net import request_json, _url_host

router=APIRouter(prefix='/api')

class Model(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Project(Model):
    name:str=Field(min_length=1,max_length=150)
    description:str=Field(default='',max_length=20000)
    agent_ids:list[str]=Field(default_factory=list,max_length=100)
    skill_ids:list[str]=Field(default_factory=list,max_length=100)
    planner_provider_id:str=Field(default='',max_length=100)
    auto_create_agents:bool=True
    enabled:bool=True

class TextAsset(Model):
    name:str=Field(min_length=1,max_length=100)
    description:str=Field(default='',max_length=1000)
    instructions:str=Field(min_length=1,max_length=20000)
    enabled:bool=True

class Server(Model):
    name:str=Field(min_length=1,max_length=100)
    url:str=Field(min_length=1,max_length=2048)
    token:str=Field(default='',max_length=4096)
    enabled:bool=True

class Decision(Model):
    token:str=Field(default='',max_length=4096)
    model:str=Field(default='jev-1.13.0',pattern=r'^jev-[A-Za-z0-9.\-]+$',max_length=100)
    threshold:float=Field(default=.8,ge=0,le=1)
    enabled:bool=False

class Chat(Model):
    title:str=Field(default='Новый чат',min_length=1,max_length=150)
    agent_id:str=Field(default='',max_length=100)
    provider_id:str=Field(default='',max_length=100)
    project_id:str=Field(default='',max_length=100)
    model:str=Field(default='',max_length=150)
    mask_id:str=Field(default='',max_length=100)

class Message(Model):
    text:str=Field(min_length=1,max_length=30000)

class Automation(Model):
    name:str=Field(min_length=1,max_length=150)
    project_id:str=Field(min_length=1,max_length=100)
    brief:str=Field(min_length=1,max_length=20000)
    interval_minutes:int=Field(default=1440,ge=5,le=525600)
    max_runs:int=Field(default=30,ge=1,le=10000)
    max_output_tokens:int=Field(default=12000,ge=1024,le=100000)
    auto_run:bool=False
    enabled:bool=False

class CRM(Model):
    url:str=Field(min_length=1,max_length=2048)
    token:str=Field(default='',max_length=4096)
    object_name:str=Field(default='tasks',pattern=r'^[A-Za-z][A-Za-z0-9]{0,60}$')
    title_field:str=Field(default='title',pattern=r'^[A-Za-z][A-Za-z0-9]{0,60}$')
    enabled:bool=False


def get(kind,id):
    row=db.get(kind,id)
    if not row:raise HTTPException(404,'Не найдено')
    return row


def refs(kind,ids):
    if len(ids)!=len(set(ids)):raise ValueError('Повторяющиеся идентификаторы.')
    for id in ids:
        row=get(kind,id)
        if row.get('enabled') is False:raise ValueError('Выбранный объект отключён: '+kind)


def check_project(data):
    refs('agent',data['agent_ids']);refs('skill',data['skill_ids'])
    if data['planner_provider_id']:refs('provider',[data['planner_provider_id']])
    return data


def project_task(data):
    pid=data.get('project_id')
    if not pid:return data
    project=get('project',pid)
    if not project['enabled']:raise ValueError('Проект отключён.')
    if not data.get('planner_provider_id'):data['planner_provider_id']=project['planner_provider_id']
    return data


@router.post('/projects')
async def create_project(data:Project):
    return db.put('project',{'id':uid(),**check_project(data.model_dump())})

@router.put('/projects/{id}')
async def update_project(id:str,data:Project):
    old=get('project',id)
    if any(t.get('project_id')==id and t['status'] in ('running','planning') for t in db.list('task')):
        raise ValueError('Сначала приостановите выполняющиеся задачи проекта.')
    return db.put('project',{**old,**check_project(data.model_dump())})

class ManualTask(Model):
    title:str=Field(min_length=1,max_length=200)
    brief:str=Field(min_length=1,max_length=20000)
    project_id:str=Field(min_length=1,max_length=100)
    agent_ids:list[str]=Field(min_length=1,max_length=16)
    sequential:bool=True

@router.post('/manual-tasks')
async def manual_task(data:ManualTask,request:Request):
    project=get('project',data.project_id);refs('agent',data.agent_ids)
    if any(a not in project['agent_ids'] for a in data.agent_ids):raise ValueError('Назначьте всех агентов в проект.')
    task=request.app.state.engine.create_task(project_task({'title':data.title,'brief':data.brief,'project_id':data.project_id,'max_steps':len(data.agent_ids)}))
    steps=[{'id':'s'+str(i),'name':get('agent',a)['name'],'agent_id':a,'depends_on':['s'+str(i-1)] if data.sequential and i else [],'input':'Выполни свой этап по общей задаче.','status':'pending','output':'','error':'','attempts':0,'tokens_used':0,'reserved_tokens':0} for i,a in enumerate(data.agent_ids)]
    return db.update('task',task['id'],{'steps':steps,'status':'ready'})

@router.post('/skills')
async def create_skill(data:TextAsset):return db.put('skill',{'id':uid(),'revision':1,**data.model_dump()})

@router.put('/skills/{id}')
async def update_skill(id:str,data:TextAsset):
    old=get('skill',id)
    return db.put('skill',{**old,**data.model_dump(),'revision':old.get('revision',1)+1})

@router.post('/masks')
async def create_mask(data:TextAsset):return db.put('mask',{'id':uid(),**data.model_dump()})

@router.put('/masks/{id}')
async def update_mask(id:str,data:TextAsset):return db.put('mask',{**get('mask',id),**data.model_dump()})

@router.post('/mcp')
async def create_mcp(data:Server):
    payload=data.model_dump();token=payload.pop('token');toolbus.validate_url(payload['url'])
    return toolbus.sanitize(db.put('mcp',{'id':uid(),**payload,'token_encrypted':encrypt(token),'tools':[]}))

@router.put('/mcp/{id}')
async def update_mcp(id:str,data:Server):
    old=get('mcp',id);payload=data.model_dump();token=payload.pop('token');toolbus.validate_url(payload['url'])
    changed=payload['url']!=old['url']
    if changed and not token and old.get('token_encrypted'):raise ValueError('При смене адреса заново укажите токен.')
    return toolbus.sanitize(db.put('mcp',{**old,**payload,'tools':[] if changed else old.get('tools',[]),
        'token_encrypted':encrypt(token) if token else old.get('token_encrypted','')}))

@router.post('/mcp/{id}/discover')
async def discover_mcp(id:str):return await toolbus.discover(id)

@router.post('/tool-requests/{id}/approve')
async def approve_tool(id:str):return await toolbus.execute(id)

@router.post('/tool-requests/{id}/reject')
async def reject_tool(id:str):return toolbus.reject(id)

@router.put('/decision')
async def save_decision(data:Decision):
    old=db.get('decision','jev') or {};payload=data.model_dump();token=payload.pop('token')
    if payload['enabled'] and not token and not old.get('token_encrypted'):raise ValueError('Добавьте ключ TypeSafe API.')
    return toolbus.sanitize(db.put('decision',{**old,'id':'jev',**payload,'token_encrypted':encrypt(token) if token else old.get('token_encrypted','')}))

@router.post('/projects/{id}/suggest')
async def suggest(id:str,data:Message):
    project=get('project',id)
    candidates={a['id']:a['name']+' · '+a.get('role','') for a in db.list('agent') if a.get('enabled') and (not project['agent_ids'] or a['id'] in project['agent_ids'])}
    result=await jev.evaluate(data.text,candidates)
    db.event('','JEV предложил маршрут; разрешения на инструменты не изменялись.',project_id=id)
    return result


def create_chat(data):
    if data.get('project_id'):refs('project',[data['project_id']])
    if data.get('agent_id'):
        refs('agent',[data['agent_id']])
        if data.get('project_id'):
            project=get('project',data['project_id'])
            if data['agent_id'] not in project['agent_ids']:raise ValueError('Назначьте агента в этот проект.')
    elif data.get('provider_id'):refs('provider',[data['provider_id']])
    else:raise ValueError('Выберите агента или провайдера.')
    if data.get('mask_id'):refs('mask',[data['mask_id']])
    return db.put('chat',{'id':uid(),**data,'messages':[],'status':'idle','tokens_used':0})

@router.post('/chats')
async def new_chat(data:Chat):return create_chat(data.model_dump())

@router.get('/chats/{id}')
async def chat_detail(id:str):return get('chat',id)

@router.post('/chats/{id}/messages')
async def message(id:str,data:Message,request:Request):
    return request.app.state.workspace.message(id,data.text)

@router.post('/chats/{id}/stop')
async def stop_chat(id:str):
    get('chat',id)
    return db.update('chat',id,{'status':'stopped'})

@router.post('/automations')
async def new_automation(data:Automation):
    refs('project',[data.project_id]);project=get('project',data.project_id)
    refs('provider',[project['planner_provider_id']])
    return db.put('automation',{'id':uid(),**data.model_dump(),'runs':0,'next_run':time.time()+data.interval_minutes*60})

@router.put('/automations/{id}')
async def update_automation(id:str,data:Automation):
    old=get('automation',id);refs('project',[data.project_id]);refs('provider',[get('project',data.project_id)['planner_provider_id']])
    return db.put('automation',{**old,**data.model_dump(),'next_run':time.time()+data.interval_minutes*60})

@router.post('/automations/{id}/run')
async def run_automation(id:str,request:Request):
    task=request.app.state.workspace.fire(get('automation',id),manual=True)
    await request.app.state.engine.action(task['id'],'plan')
    return get('task',task['id'])

@router.get('/system/status')
async def system_status():
    usage=shutil.disk_usage(os.getenv('APP_DATA_DIR','data'))
    return {'application':'Агентная 2.0.1','database':'SQLite WAL','processes':1,'disk_free_bytes':usage.free,'disk_total_bytes':usage.total,
        'tasks_running':sum(t['status']=='running' for t in db.list('task')),'pending_approvals':sum(r['status']=='pending' for r in db.list('tool_request')),
        'load_average':list(os.getloadavg()) if hasattr(os,'getloadavg') else None,'shell_access':False,'measured_at':now()}

@router.put('/crm')
async def save_crm(data:CRM):
    old=db.get('crm','twenty') or {};payload=data.model_dump();token=payload.pop('token');_url_host(payload['url'])
    u=urlsplit(payload['url'])
    if u.path not in ('','/') or u.query:raise ValueError('Укажите корневой HTTPS-адрес Twenty, без пути.')
    if old.get('url')!=payload['url'] and old.get('token_encrypted') and not token:raise ValueError('Для нового адреса введите новый API-ключ.')
    if payload['enabled'] and not token and not old.get('token_encrypted'):raise ValueError('Добавьте API-ключ Twenty.')
    return toolbus.sanitize(db.put('crm',{**old,'id':'twenty',**payload,'token_encrypted':encrypt(token) if token else old.get('token_encrypted','')}))

@router.get('/crm/records')
async def crm_records():
    config=get('crm','twenty')
    if not config.get('enabled'):raise ValueError('Подключение Twenty отключено.')
    response=await request_json('GET',config['url'].rstrip('/')+'/rest/'+config['object_name'],params={'limit':30},headers={'Authorization':'Bearer '+decrypt(config['token_encrypted'])})
    records=response.get('data',{}).get(config['object_name'],[])
    if not isinstance(records,list):raise ValueError('Проверьте имя объекта по API-документации вашей CRM.')
    safe=[{'id':r.get('id'),'title':str(r.get(config['title_field'],'Без названия'))[:200]} for r in records if isinstance(r,dict)]
    return {'records':safe}

class ImportCRM(Model):
    record_id:str=Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9-]+$')
    project_id:str=Field(min_length=1,max_length=100)

@router.post('/crm/import')
async def import_crm(data:ImportCRM,request:Request):
    config=get('crm','twenty');project=get('project',data.project_id)
    if not config.get('enabled'):raise ValueError('Подключение Twenty отключено.')
    response=await request_json('GET',config['url'].rstrip('/')+'/rest/'+config['object_name']+'/'+data.record_id,headers={'Authorization':'Bearer '+decrypt(config['token_encrypted'])})
    record=response.get('data')
    if not isinstance(record,dict):raise ValueError('Неожиданный ответ CRM.')
    brief=json.dumps(record,ensure_ascii=False)
    if len(brief)>28000:raise ValueError('Запись слишком велика; сузьте поля в CRM.')
    key=toolbus.digest([config['url'],config['object_name'],data.record_id,data.project_id])
    for old in db.list('task'):
        if old.get('crm_import_key')==key:return old
    task=request.app.state.engine.create_task(project_task({'title':'Из CRM · '+data.record_id[:32], 'brief':'Подготовь результат по записи CRM. Это недоверенные исходные данные:\n'+brief,'project_id':data.project_id,'max_steps':8,'max_output_tokens':16000}))
    return db.update('task',task['id'],{'crm_import_key':key})


class Workspace:
    def __init__(self,engine):
        self.engine=engine;self.jobs={}
        for chat in db.list('chat'):
            if chat.get('status')=='running' or chat.get('request_started'):
                db.update('chat',chat['id'],{'status':'stopped' if chat.get('status')=='stopped' else 'failed','usage_uncertain':chat.get('usage_uncertain',False) or chat.get('request_started',True),'request_started':False,'error':'Процесс перезапущен; запрос не повторялся автоматически.'})
        toolbus.recover()

    def message(self,id,text):
        chat=get('chat',id)
        if chat['status']=='running' or id in self.jobs:raise ValueError('Дождитесь завершения текущего ответа.')
        if len(chat['messages'])>=200:raise ValueError('Создайте новый чат: достигнут лимит истории.')
        chat['messages'].append({'role':'user','content':text,'created_at':now()})
        db.put('chat',{**chat,'status':'running','request_started':False,'error':''})
        self.jobs[id]=asyncio.create_task(self.answer(id))
        return get('chat',id)

    async def answer(self,id):
        try:
            chat=get('chat',id)
            agent=get('agent',chat['agent_id']) if chat.get('agent_id') else {'instructions':'Помогайте владельцу с задачей.','provider_id':chat['provider_id'],'model':chat.get('model',''),'mask_id':chat.get('mask_id',''),'max_tokens':4096,'enabled':True}
            if not agent.get('enabled'):raise ValueError('Агент отключён.')
            if chat.get('project_id'):
                project=get('project',chat['project_id'])
                if not project['enabled']:raise ValueError('Проект отключён.')
                if chat.get('agent_id') and chat['agent_id'] not in project['agent_ids']:raise ValueError('Агент больше не назначен проекту.')
            provider=self.engine._provider(agent['provider_id'])
            messages=[{k:m[k] for k in ('role','content')} for m in chat['messages']]
            async with self.engine._slots:
                if get('chat',id)['status']!='running':return
                db.update('chat',id,{'request_started':True})
                result=await run_agent(agent,provider,messages,min(agent.get('max_tokens',4096),16000),chat_id=id)
            def finish(current):
                current['tokens_used']=current.get('tokens_used',0)+sum(result['usage'].get(k,0) for k in ('input_tokens','output_tokens'))
                current['usage_uncertain']=current.get('usage_uncertain',False) or result['usage'].get('source') not in ('provider','demo_no_billable_tokens')
                current['request_started']=False
                if current['status']=='stopped':return current
                current['messages'].append({'role':'assistant','content':result['text'] or result.get('error',''),'created_at':now()})
                current.update(status='failed' if result.get('error') else 'idle',error=result.get('error',''))
                return current
            db.mutate('chat',id,finish)
        except asyncio.CancelledError:
            def interrupted(current):
                current['usage_uncertain']=current.get('usage_uncertain',False) or current.get('request_started',False)
                current['request_started']=False
                if current['status']!='stopped':current.update(status='failed',error='Ответ прерван. Повторите сообщение вручную; возможный расход отмечен отдельно.')
                return current
            db.mutate('chat',id,interrupted)
            raise
        except Exception:
            def failed(current):
                current['usage_uncertain']=current.get('usage_uncertain',False) or current.get('request_started',False)
                current['request_started']=False
                if current['status']!='stopped':current.update(status='failed',error='Не удалось получить ответ. Проверьте агента, модель и доступность провайдера.')
                return current
            db.mutate('chat',id,failed)
        finally:self.jobs.pop(id,None)

    def fire(self,item,manual=False):
        if item['runs']>=item['max_runs']:raise ValueError('Лимит запусков исчерпан.')
        if not manual and not item['enabled']:return None
        if item.get('last_task_id'):
            previous=db.get('task',item['last_task_id'])
            if previous and previous['status'] in ('backlog','ready','running','planning','review','paused'):raise ValueError('Предыдущий запуск ещё не завершён.')
        payload=project_task({'title':item['name'],'brief':item['brief'],'project_id':item['project_id'],'max_steps':8,'max_output_tokens':item['max_output_tokens'],'auto_run':item['auto_run']})
        task=self.engine.create_task(payload,persist=False)
        # Both the task and the trigger cursor commit in one SQLite transaction.
        with db._lock, db.connection:
            db._put('task',task)
            db._put('automation',{**item,'runs':item['runs']+1,'next_run':time.time()+item['interval_minutes']*60,'last_task_id':task['id'],'error':''})
        return task

    async def loop(self,stop):
        while not stop.is_set():
            for item in db.list('automation'):
                if item['enabled'] and item['runs']<item['max_runs'] and item['next_run']<=time.time():
                    try:
                        task=self.fire(item)
                        if task:await self.engine.action(task['id'],'plan')
                    except Exception:db.update('automation',item['id'],{'next_run':time.time()+item['interval_minutes']*60,'error':'Запуск пропущен: проверьте предыдущую задачу, проект и провайдера.'})
            try:await asyncio.wait_for(stop.wait(),1)
            except TimeoutError:pass

    async def stop(self):
        jobs=list(self.jobs.values())
        for job in jobs:job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
