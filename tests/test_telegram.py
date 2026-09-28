import asyncio
import json
import time
from unittest.mock import AsyncMock
import httpx
import pytest
from cryptography.fernet import Fernet
from app.db import db
from app.security import encrypt
from app import telegram as tg

@pytest.fixture(autouse=True)
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('MASTER_KEY',Fernet.generate_key().decode())
    db.init(tmp_path/'test.sqlite3')
    monkeypatch.setattr(tg, '_pace', AsyncMock())

@pytest.fixture
def controller(monkeypatch):
    c=db.put('connector',{'id':'bot','kind':'telegram','enabled':True,'name':'Control','config':{'allowed_user_ids':[42],'controller_enabled':True},'secret_encrypted':encrypt(json.dumps({'bot_token':'123456:abcdefghijklmnopqrstuvwxyz123456'}))})
    messages=[]
    async def api(token,method,payload=None,**kw):
        if method=='sendMessage':
            messages.append(payload);return {'message_id':len(messages)}
        return True
    monkeypatch.setattr(tg.social,'_telegram',api)
    return c,messages

def message(text='Идея',**extra):
    return {'message':{'from':{'id':42,'is_bot':False},'chat':{'id':42,'type':'private'},'date':int(time.time()),'text':text,**extra}}

def task(state='review',status='review',**extra):
    return db.put('task',{'id':'a'*32,'title':'Idea','status':status,'intake':{'state':state,'revision':1,'proposal':'FULL PROPOSAL'},'telegram':{'connector_id':'bot','user_id':42},**extra})

def callback(data,message_id=1,user=42):
    return {'callback_query':{'id':'query','from':{'id':user,'is_bot':False},'data':data,'message':{'message_id':message_id,'from':{'id':123456,'is_bot':True},'chat':{'id':user,'type':'private'}}}}

class Workflow:
    def __init__(self): self.approved=[];self.ideas=[];self.revised=[]
    def create_idea(self,text,**kwargs): self.ideas.append((text,kwargs));return task(state='queued')
    async def approve(self,id,revision): self.approved.append((id,revision));db.update('task',id,{'intake':{'state':'approved','revision':revision},'status':'backlog'})
    def revise(self,id,text,revision): self.revised.append((id,text,revision))

@pytest.mark.parametrize('changes',[{'chat':{'id':42,'type':'group'}},{'from':{'id':99,'is_bot':False}},{'date':0},{'forward_origin':{}},{'via_bot':{}},{'from':{'id':True,'is_bot':False}}])
def test_reject_untrusted_messages(controller,changes):
    c,_=controller
    assert tg.authorized_message(message(**changes),c['config']) is None

def test_idea_creates_durable_attribution_without_execute(controller):
    c,msgs=controller;w=Workflow()
    asyncio.run(tg.handle_update(message(),c,None,w))
    assert w.ideas[0][1]['user_id']==42 and not w.approved
    assert db.get('task','a'*32)['intake']['state']=='queued'
    assert len(msgs)==1

def test_callback_full_preview_scope_and_replay(controller):
    c,msgs=controller;t=task();w=Workflow()
    asyncio.run(tg.show_task(c,42,t))
    data=msgs[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    assert 'FULL PROPOSAL' in msgs[0]['text']
    asyncio.run(tg.handle_callback(callback(data,user=99),c,None,w));assert not w.approved
    asyncio.run(tg.handle_callback(callback(data),c,None,w));assert len(w.approved)==1
    asyncio.run(tg.handle_callback(callback(data),c,None,w));assert len(w.approved)==1

def test_callback_stale_revision_rejected(controller):
    c,msgs=controller;t=task();w=Workflow();asyncio.run(tg.show_task(c,42,t))
    data=msgs[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    db.update('task',t['id'],{'intake':{**t['intake'],'revision':2}})
    with pytest.raises(ValueError): asyncio.run(tg.handle_callback(callback(data),c,None,w))
    assert not w.approved

def test_callback_current_allowlist_rechecked(controller):
    c,msgs=controller;t=task();w=Workflow();asyncio.run(tg.show_task(c,42,t))
    data=msgs[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    db.update('connector','bot',{'config':{'allowed_user_ids':[99]}})
    asyncio.run(tg.handle_callback(callback(data),c,None,w));assert not w.approved

def test_partial_preview_does_not_activate_buttons_or_retry(controller,monkeypatch):
    c,msgs=controller;t=task();b=tg.button(c,42,'Approve','approve',t);calls=[]
    async def fail(token,method,payload,**kw):
        calls.append(payload)
        if len(calls)==2: raise ValueError('unknown')
        return {'message_id':1}
    monkeypatch.setattr(tg.social,'_telegram',fail)
    with pytest.raises(ValueError):asyncio.run(tg.send(c,42,'x'*4000,[[b]],key='one'))
    assert db.get('telegram_callback',b['callback_data'][2:])['active'] is False
    assert db.get('telegram_outbox','one')['status']=='unknown'
    asyncio.run(tg.send(c,42,'x'*4000,[[b]],key='one'));assert len(calls)==2

def test_notification_recovery_at_most_once(controller):
    c,msgs=controller;task()
    asyncio.run(tg.notify_once());asyncio.run(tg.notify_once())
    assert len(msgs)==1

def voice_provider(c):
    db.put('provider',{'id':'openai','kind':'openai','enabled':True,'api_key_encrypted':encrypt('secret')})
    return db.update('connector','bot',{'config':{**c['config'],'voice_enabled':True,'transcription_provider_id':'openai'}})

def voice(): return message(voice={'duration':3,'file_size':8,'file_id':'safe'})['message']

def test_voice_secure_fixed_endpoint_and_no_shell(controller,monkeypatch):
    c,_=controller;c=voice_provider(c);calls=[]
    async def api(*args,**kw):return {'file_path':'voice/file_1.oga','file_size':8}
    async def download(method,url,**kw):
        calls.append((url,kw));return httpx.Response(200,content=b'OggS1234')
    async def speech(method,url,**kw):
        calls.append((url,kw));return {'text':'Создай статью'}
    monkeypatch.setattr(tg.social,'_telegram',api);monkeypatch.setattr(tg,'request',download);monkeypatch.setattr(tg,'request_json',speech)
    assert asyncio.run(tg.transcribe(voice(),c))=='Создай статью'
    assert calls[0][0].startswith('https://api.telegram.org/file/bot')
    assert calls[1][0]=='https://api.openai.com/v1/audio/transcriptions'
    assert calls[1][1]['files']['file'][0]=='voice.ogg'

@pytest.mark.parametrize('path',['../secret.ogg','https://evil.example/a.ogg','voice/a.ogg?x=1','voice/a.mp3','voice/a%2f.ogg'])
def test_voice_reject_path_injection(controller,monkeypatch,path):
    c,_=controller;c=voice_provider(c)
    monkeypatch.setattr(tg.social,'_telegram',AsyncMock(return_value={'file_path':path,'file_size':8}))
    raw=AsyncMock();monkeypatch.setattr(tg,'request',raw)
    with pytest.raises(ValueError): asyncio.run(tg.transcribe(voice(),c))
    raw.assert_not_awaited()

def test_voice_rechecks_provider_after_download(controller,monkeypatch):
    c,_=controller;c=voice_provider(c)
    monkeypatch.setattr(tg.social,'_telegram',AsyncMock(return_value={'file_path':'voice/a.ogg','file_size':8}))
    async def download(*a,**kw):
        db.update('provider','openai',{'enabled':False});return httpx.Response(200,content=b'OggS1234')
    monkeypatch.setattr(tg,'request',download);speech=AsyncMock();monkeypatch.setattr(tg,'request_json',speech)
    with pytest.raises(ValueError):asyncio.run(tg.transcribe(voice(),c))
    speech.assert_not_awaited()

def test_publication_approval_and_send_are_separate(controller,monkeypatch):
    from app.publications import create_publication
    c,msgs=controller;t=task(state='approved',status='done',result='Content')
    db.put('connector',{'id':'target','kind':'telegram','config':{'chat_id':'@testchannel'},'enabled':True})
    p=create_publication({'task_id':t['id'],'connector_id':'target','text':'Reviewed content'})
    asyncio.run(tg.show_publication(c,42,t,p))
    data=msgs[-1]['reply_markup']['inline_keyboard'][2][0]['callback_data']
    publish=AsyncMock(return_value={'external_id':'post'});monkeypatch.setattr(tg.social,'publish',publish)
    asyncio.run(tg.handle_callback(callback(data),c,None,Workflow()))
    assert db.get('publication',p['id'])['status']=='approved';publish.assert_not_awaited()
    data=msgs[-1]['reply_markup']['inline_keyboard'][2][0]['callback_data']
    asyncio.run(tg.handle_callback(callback(data,message_id=len(msgs)),c,None,Workflow()))
    publish.assert_awaited_once();assert db.get('publication',p['id'])['status']=='published'

def test_publication_target_change_during_ack_rejected(controller,monkeypatch):
    from app.publications import create_publication
    c,msgs=controller;t=task(state='approved',status='done')
    db.put('connector',{'id':'target','kind':'telegram','config':{'chat_id':'@oldchannel'},'enabled':True})
    p=create_publication({'task_id':t['id'],'connector_id':'target','text':'Content'})
    asyncio.run(tg.show_publication(c,42,t,p));data=msgs[-1]['reply_markup']['inline_keyboard'][2][0]['callback_data']
    async def changed(*a,**kw): db.update('connector','target',{'config':{'chat_id':'@newchannel'}});return True
    monkeypatch.setattr(tg.social,'_telegram',changed)
    with pytest.raises(ValueError):asyncio.run(tg.handle_callback(callback(data),c,None,Workflow()))
    assert db.get('publication',p['id'])['status']=='draft'

def test_poll_discards_backlog_then_saves_offset_before_action(controller,monkeypatch):
    c,_=controller;stop=asyncio.Event();calls=[];w=Workflow()
    async def poll(token,method,payload=None,**kwargs):
        assert method=='getUpdates';calls.append(payload)
        if len(calls)==1:return [{'update_id':7,'message':message()['message']}]
        stop.set();return [{'update_id':8,'message':message()['message']},{'update_id':8,'message':message()['message']}]
    async def handle(update,connector,engine,workflow):
        offsets=[s['value'] for s in db.list('setting') if s['id'].startswith('telegram_offset_')]
        assert offsets==[9];workflow.ideas.append(update)
    monkeypatch.setattr(tg.social,'_telegram',poll);monkeypatch.setattr(tg,'handle_update',handle)
    asyncio.run(tg.worker(c,stop,None,w))
    assert len(w.ideas)==1 and calls[0]['offset']==-1 and calls[1]['offset']==8

def test_web_publication_notifies_owner_once(controller):
    from app.publications import create_publication
    c,msgs=controller;t=task(state='approved',status='done')
    db.put('connector',{'id':'target','kind':'telegram','config':{'chat_id':'@channel'},'enabled':True})
    p=create_publication({'task_id':t['id'],'connector_id':'target','text':'Web draft'})
    asyncio.run(tg.notify_once());count=len(msgs);asyncio.run(tg.notify_once())
    assert len(msgs)==count and any('Web draft' in m['text'] for m in msgs)

def test_reopen_existing_publication(controller):
    from app.publications import create_publication
    c,msgs=controller;t=task(state='approved',status='done')
    db.put('connector',{'id':'target','kind':'telegram','config':{'chat_id':'@channel'},'enabled':True})
    p=create_publication({'task_id':t['id'],'connector_id':'target','text':'Draft'})
    asyncio.run(tg.show_task(c,42,t))
    buttons=msgs[-1]['reply_markup']['inline_keyboard'];data=buttons[-1][0]['callback_data']
    asyncio.run(tg.handle_callback(callback(data),c,None,Workflow()))
    assert 'Draft' in msgs[-1]['text'] and len(db.list('publication'))==1

@pytest.mark.parametrize('changes',[{'duration':301},{'file_size':10485761},{'duration':True},{'file_size':0}])
def test_voice_limits_before_network(controller,monkeypatch,changes):
    c,_=controller;c=voice_provider(c);m=voice();m['voice'].update(changes)
    api=AsyncMock();monkeypatch.setattr(tg.social,'_telegram',api)
    with pytest.raises(ValueError):asyncio.run(tg.transcribe(m,c))
    api.assert_not_awaited()

def test_voice_disabled_no_network(controller,monkeypatch):
    c,_=controller;api=AsyncMock();monkeypatch.setattr(tg.social,'_telegram',api)
    with pytest.raises(ValueError):asyncio.run(tg.transcribe(voice(),c))
    api.assert_not_awaited()

def test_concurrent_previews_send_contiguously(controller,monkeypatch):
    c,_=controller;calls=[]
    async def api(token,method,payload,**kw):
        calls.append(payload['text'][0]);await asyncio.sleep(0)
        return {'message_id':len(calls)}
    monkeypatch.setattr(tg.social,'_telegram',api)
    async def exercise():
        await asyncio.gather(tg.send(c,42,'A'*4000,key='a'),tg.send(c,42,'B'*4000,key='b'))
    asyncio.run(exercise())
    assert calls in [['A']*3+['B']*3,['B']*3+['A']*3]
    assert db.get('telegram_outbox','a')['status']=='sent'
    assert db.get('telegram_outbox','b')['status']=='sent'


def test_cancel_waiting_send_claims_outbox_and_keeps_buttons_inactive(controller,monkeypatch):
    c,_=controller;t=task();b=tg.button(c,42,'Approve','approve',t)
    async def exercise():
        started=asyncio.Event();release=asyncio.Event()
        async def api(token,method,payload,**kw):
            started.set();await release.wait();return {'message_id':1}
        monkeypatch.setattr(tg.social,'_telegram',api)
        first=asyncio.create_task(tg.send(c,42,'First',key='first'))
        await started.wait()
        second=asyncio.create_task(tg.send(c,42,'Second',[[b]],key='second'))
        await asyncio.sleep(0)
        assert db.get('telegram_outbox','second')['status']=='sending'
        second.cancel()
        with pytest.raises(asyncio.CancelledError):await second
        release.set();await first
    asyncio.run(exercise())
    assert db.get('telegram_outbox','second')['status']=='unknown'
    assert db.get('telegram_callback',b['callback_data'][2:])['active'] is False


def test_revoke_owner_during_pacing_prevents_next_send(controller,monkeypatch):
    c,msgs=controller;waits=[]
    async def pace(state):
        waits.append(1)
        if len(waits)==2:db.update('connector','bot',{'config':{'allowed_user_ids':[]}})
    monkeypatch.setattr(tg,'_pace',pace)
    with pytest.raises(ValueError):asyncio.run(tg.send(c,42,'x'*4000,key='revoked'))
    assert len(msgs)==1
    assert db.get('telegram_outbox','revoked')['status']=='unknown'


def test_full_telegram_workflow_real_engine_and_publication_boundary(controller, monkeypatch):
    """Real persisted workflow+engine+publication service, only external transport mocked."""
    from app.engine import Engine
    from app.workflow import Workflow as RealWorkflow
    c, messages = controller
    monkeypatch.setenv('DEMO_MODE', 'true')
    db.put('provider', {'id': 'demo', 'kind': 'mock', 'enabled': True, 'model': 'demo', 'name': 'Демо'})
    c = db.update('connector', c['id'], {'config': {**c['config'], 'planner_provider_id': 'demo'}})
    db.put('connector', {'id': 'target', 'kind': 'webhook', 'name': 'Test target', 'enabled': True,
                         'config': {'url': 'https://example.com/hook'}})
    effects = []
    async def publish(p, target):
        effects.append(p['id'])
        return {'external_id': 'test-only'}
    monkeypatch.setattr(tg.social, 'publish', publish)
    async def run():
        engine = Engine(); await engine.start()
        workflow = RealWorkflow(engine); stop = asyncio.Event()
        job = asyncio.create_task(workflow.loop(stop))
        async def wait_for(tid, predicate):
            for _ in range(150):
                t = db.get('task', tid)
                if predicate(t): return t
                await asyncio.sleep(.03)
            raise AssertionError(t)
        async def click(action):
            record = next(r for r in db.list('telegram_callback') if r['action'] == action and r['active'] and not r['used'])
            update = callback('a:' + record['id'], message_id=record['message_id'])
            await tg.handle_update(update, c, engine, workflow)
            return update
        try:
            await tg.handle_update(message('Подготовить текст о новом продукте'), c, engine, workflow)
            t = db.list('task')[0]; tid = t['id']
            assert t['status'] == 'review' and t['steps'] == []
            await wait_for(tid, lambda t: t['intake']['state'] == 'review')
            await tg.notify_once()
            await click('revise')
            await tg.handle_update(message('Не обещай неподтверждённых характеристик'), c, engine, workflow)
            t = await wait_for(tid, lambda t: t['intake']['state'] == 'review' and t['intake']['revision'] == 2)
            assert t['steps'] == [] and not effects
            await tg.notify_once()
            duplicate = await click('approve')
            await tg.handle_update(duplicate, c, engine, workflow)
            t = await wait_for(tid, lambda t: t['status'] == 'done')
            assert t['plan_generation'] == 1 and t['result'] and not effects
            await tg.notify_once()
            await click('targets'); await click('draft')
            p = db.list('publication')[0]
            assert p['status'] == 'draft' and not effects
            await click('pub_approve')
            assert db.get('publication', p['id'])['status'] == 'approved' and not effects
            duplicate = await click('pub_publish')
            await tg.handle_update(duplicate, c, engine, workflow)
            assert effects == [p['id']]
            assert db.get('publication', p['id'])['status'] == 'published'
        finally:
            stop.set(); job.cancel(); await asyncio.gather(job, return_exceptions=True)
            await engine.stop()
    asyncio.run(run())
