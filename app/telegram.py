"""Private Telegram conversation. Tokens and notification attempts survive restarts.

No model has access to this module. Every external send is owner-initiated or a
status notification for that owner's explicit idea, with no ambiguous retry.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
import secrets
import time
import weakref
from app.db import db, now
from app import integrations as social
from app.net import request, request_json
from app.security import decrypt

HELP = ('АГЕНТНАЯ · отправьте идею текстом или голосом. Агент предложит решение; '
        'выполнение начинается только после кнопки «Одобрить и выполнить».\n'
        '/tasks — мои задачи\n/task <id> — статус и актуальные кнопки\n/result <id> — полный результат\n'
        '/pause <id> · /resume <id> · /cancel <id>\n'
        '/new — выйти из режима правок и начать новую идею\n'
        'Публикация: результат → канал → черновик → одобрить → отдельно опубликовать.')


def fingerprint(connector):
    return hashlib.sha256(json.dumps({k: connector.get(k) for k in ('id','kind','config','secret_encrypted','enabled')}, sort_keys=True).encode()).hexdigest()


def current(connector_id, user_id=None):
    connector = db.get('connector', connector_id)
    if not connector or connector.get('kind') != 'telegram' or not connector.get('enabled', True):
        raise ValueError('Telegram-подключение отключено.')
    config = connector.get('config') or {}
    if not config.get('controller_enabled', True):
        raise ValueError('Управление через Telegram отключено.')
    if user_id is not None and (type(user_id) is not int or user_id <= 0 or user_id not in [x for x in config.get('allowed_user_ids', []) if type(x) is int]):
        raise ValueError('Доступ отозван.')
    return connector


def authorized_message(update, config):
    m = update.get('message')
    if not isinstance(m, dict): return None
    chat, sender = m.get('chat') or {}, m.get('from') or {}
    user = sender.get('id')
    if (type(user) is not int or user <= 0 or sender.get('is_bot') is not False
        or chat.get('type') != 'private' or type(chat.get('id')) is not int or chat['id'] != user
        or any(k in m for k in ('forward_origin','forward_from','forward_from_chat','sender_chat','via_bot'))
        or user not in [x for x in config.get('allowed_user_ids', []) if type(x) is int]
        or type(m.get('date')) is not int or not 0 <= time.time()-m['date'] < 300): return None
    return m


def owned_task(task_id, connector_id, user_id):
    task = db.get('task', task_id)
    if not task or task.get('telegram') != {'connector_id': connector_id, 'user_id': user_id}:
        raise ValueError('Задача не принадлежит этому диалогу.')
    return task


def resolve_task(prefix, connector_id, user_id):
    if not re.fullmatch('[a-f0-9]{8,32}', prefix): raise ValueError('Нужны первые 8 символов ID задачи.')
    matches = [t for t in db.list('task') if t['id'].startswith(prefix) and t.get('telegram') == {'connector_id':connector_id,'user_id':user_id}]
    if len(matches) != 1: raise ValueError('Задача не найдена или ID неоднозначен.')
    return matches[0]


def button(connector, user_id, label, action, task, **extra):
    key = secrets.token_urlsafe(24)
    db.put('telegram_callback', {'id':key, 'connector_id':connector['id'], 'fingerprint':fingerprint(connector),
        'user_id':user_id, 'action':action, 'task_id':task['id'], 'revision':(task.get('intake') or {}).get('revision'),
        'expires':time.time()+86400, 'active':False, 'used':False, **extra})
    return {'text':label, 'callback_data':'a:'+key}


# A lock covers the complete multipart preview, including its final buttons.
# Loop-local state avoids sharing asyncio primitives between test/server loops.
_send_states = weakref.WeakKeyDictionary()


def _send_state(bot_id, user_id):
    states = _send_states.setdefault(asyncio.get_running_loop(), {})
    return states.setdefault((bot_id, user_id), {'lock': asyncio.Lock(), 'last_attempt': None})


async def _pace(state):
    if state['last_attempt'] is not None:
        remaining = 1.1 - (time.monotonic() - state['last_attempt'])
        if remaining > 0:
            await asyncio.sleep(remaining)


async def send(connector, user_id, text, buttons=None, key=None):
    """At-most-once outbox, serialized and paced per bot/private chat.

    The durable attempt is claimed before waiting for the lock. Cancellation,
    including during pacing, leaves unknown delivery and inactive buttons.
    """
    if type(user_id) is not int or user_id <= 0: raise ValueError('Нужен ID владельца.')
    connector = current(connector['id'], user_id)
    key = key or secrets.token_hex(16)
    if db.get('telegram_outbox', key): return
    db.put('telegram_outbox', {'id':key,'connector_id':connector['id'],'user_id':user_id,'status':'sending'})
    try:
        token = social._telegram_token(social._secrets(connector))
        state = _send_state(token.split(':', 1)[0], user_id)
        parts = [str(text)[i:i+1900] for i in range(0, len(str(text)), 1900)] or ['—']
        async with state['lock']:
            result = None
            for index, part in enumerate(parts):
                await _pace(state)
                # Settings/allowlist may change while a previous message sends or
                # while this one waits; check after every wait, before the effect.
                fresh = current(connector['id'], user_id)
                if fingerprint(fresh) != fingerprint(connector): raise ValueError('Настройки подключения изменились.')
                payload = {'chat_id':user_id, 'text':part,'protect_content':True,'link_preview_options':{'is_disabled':True}}
                if buttons and index == len(parts)-1: payload['reply_markup']={'inline_keyboard':buttons}
                state['last_attempt'] = time.monotonic()
                result = await social._telegram(token,'sendMessage',payload,effect=True)
                if not isinstance(result,dict) or type(result.get('message_id')) is not int:
                    raise ValueError('Telegram не подтвердил доставку.')
            for row in buttons or []:
                for item in row:
                    db.update('telegram_callback',item['callback_data'][2:],{'active':True,'message_id':result['message_id']})
            db.update('telegram_outbox',key,{'status':'sent'})
    except BaseException:
        db.update('telegram_outbox',key,{'status':'unknown','error':'Доставка не подтверждена. Автоматический повтор отключён; запросите /task или /result.'})
        raise


async def transcribe(message, connector):
    config = connector.get('config') or {}
    if not config.get('voice_enabled',False): raise ValueError('Включите распознавание голоса в настройках Telegram и выберите OpenAI API. Пока отправьте текст.')
    voice = message.get('voice') or {}
    if (type(voice.get('duration')) is not int or not 0 < voice['duration'] <= 300
        or type(voice.get('file_size')) is not int or not 0 < voice['file_size'] <= 10*1024*1024
        or not isinstance(voice.get('file_id'),str) or len(voice['file_id']) > 512):
        raise ValueError('Голосовое сообщение: до 5 минут и 10 МБ.')
    provider = db.get('provider',config.get('transcription_provider_id'))
    if not provider or provider.get('kind') != 'openai' or not provider.get('enabled',True):
        raise ValueError('Для голоса выберите активный официальный OpenAI-провайдер.')
    key = decrypt(provider.get('api_key_encrypted',''))
    if not key: raise ValueError('У OpenAI-провайдера отсутствует API-ключ.')
    token = social._telegram_token(social._secrets(connector))
    info = await social._telegram(token,'getFile',{'file_id':voice['file_id']})
    path = info.get('file_path','') if isinstance(info,dict) else ''
    if (not re.fullmatch(r'(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:oga|ogg)',path)
        or type(info.get('file_size')) is not int or not 0 < info['file_size'] <= 10*1024*1024):
        raise ValueError('Telegram вернул недопустимый OGG-файл.')
    response = await request('GET',f'https://api.telegram.org/file/bot{token}/{path}',max_bytes=10*1024*1024,timeout=45)
    if not response.content.startswith(b'OggS') or len(response.content) != info['file_size']:
        raise ValueError('Голосовой файл повреждён или имеет неподдерживаемый формат.')
    fresh = current(connector['id'],message['from']['id'])
    if fingerprint(fresh) != fingerprint(connector): raise ValueError('Настройки изменились; отправьте голосовое ещё раз.')
    latest = db.get('provider', provider['id'])
    if not latest or not latest.get('enabled', True) or latest.get('kind') != 'openai' or latest.get('api_key_encrypted') != provider.get('api_key_encrypted'):
        raise ValueError('OpenAI-провайдер изменился или отключён.')
    data = await request_json('POST','https://api.openai.com/v1/audio/transcriptions',
        headers={'Authorization':f'Bearer {key}'}, files={'file':('voice.ogg',response.content,'audio/ogg')},
        data={'model':config.get('transcription_model') or 'gpt-4o-mini-transcribe','response_format':'json'},timeout=90,max_bytes=100000)
    text = data.get('text')
    if not isinstance(text,str) or not text.strip() or len(text) > 12000: raise ValueError('Не удалось получить текст до 12000 символов. Отправьте более короткое сообщение.')
    return text.strip()


def session_id(connector_id,user): return f'{connector_id}:{user}'


async def show_task(connector,user,task):
    intake = task.get('intake') or {}
    buttons = []
    text = f"{task.get('title','Задача')} · {task['id'][:8]}\nСтатус: {task.get('status')} / {intake.get('state','')}"
    if intake.get('state') == 'review':
        proposal = intake.get('proposal') or ''
        if not isinstance(proposal,str): proposal = json.dumps(proposal,ensure_ascii=False,indent=2)
        text += '\n\nПредложение:\n'+proposal+'\n\nВыполняем? Одобрение запускает планирование и агентов с расходом API.'
        buttons = [[button(connector,user,'Одобрить и выполнить','approve',task)],
                   [button(connector,user,'Внести правки','revise',task),button(connector,user,'Отклонить','reject',task)]]
    elif task.get('status') == 'done':
        text += '\n\n'+str(task.get('result') or 'Нет текстового результата.')
        buttons = [[button(connector,user,'Подготовить публикацию','targets',task)]]
    elif intake.get('state') == 'failed':
        text += '\n'+str(intake.get('error') or 'Разбор остановился; проверьте провайдера в веб-панели.')
        buttons = [[button(connector,user,'Уточнить и повторить','revise',task)]]
    for publication in db.list('publication'):
        if publication.get('task_id') == task['id']:
            target = db.get('connector', publication.get('connector_id'))
            if target:
                buttons.append([button(connector,user,f"Черновик {publication['id'][:8]} · {publication['status']}",'pub_view',task,publication_id=publication['id'],publication_version=publication['updated_at'],target_fingerprint=fingerprint(target))])
            if len(buttons) >= 30: break
    await send(connector,user,text,buttons)


async def show_publication(connector,user,task,publication):
    target = db.get('connector',publication['connector_id'])
    text = (f"Черновик {publication['id'][:8]} · {publication['status']}\nКанал: {target.get('name','') if target else 'удалён'} ({target.get('kind','') if target else ''})\n"
            f"Заголовок: {publication.get('title','')}\nМедиа: {publication.get('media_url') or 'нет'}\n\n{publication.get('text','')}")
    destination={k:v for k,v in (target or {}).get('config',{}).items() if k in {'chat_id','owner_id','account_id','privacy_status','url'}}
    if destination: text += '\n\nАдресат / настройки: '+json.dumps(destination,ensure_ascii=False)
    if publication.get('error'): text += '\n\nОшибка: '+str(publication['error'])
    if publication.get('url'): text += '\nСсылка: '+str(publication['url'])
    if publication['status']=='uncertain': text += '\nРезультат отправки неизвестен. Проверьте канал вручную; повторная отправка заблокирована.'
    extra = {'publication_id':publication['id'],'publication_version':publication['updated_at'],
             'target_fingerprint':fingerprint(target) if target else ''}
    buttons=[]
    if publication['status'] in {'draft','approved','failed'}:
        buttons = [[button(connector,user,'Изменить текст','edit_text',task,**extra),button(connector,user,'Изменить медиа URL','edit_media',task,**extra)],
                   [button(connector,user,'Изменить заголовок','edit_title',task,**extra)]]
        if publication['status']=='draft': buttons.append([button(connector,user,'Одобрить содержание','pub_approve',task,**extra)])
        elif publication['status']=='approved': buttons.append([button(connector,user,'Опубликовать сейчас','pub_publish',task,**extra)])
        buttons.append([button(connector,user,'Отклонить публикацию','pub_reject',task,**extra)])
    marker=publication_notice_id(connector['id'],user,publication)
    db.put('telegram_notice',{'id':marker,'status':'sending'})
    try:
        await send(connector,user,text,buttons)
        db.update('telegram_notice',marker,{'status':'sent'})
    except BaseException:
        db.update('telegram_notice',marker,{'status':'unknown'})
        raise


def publication_notice_id(connector_id,user,publication):
    return f"pubnotice:{connector_id}:{user}:{publication['id']}:{publication['updated_at']}"


async def handle_callback(update,connector,engine,workflow):
    q=update.get('callback_query') or {}; sender=q.get('from') or {}; message=q.get('message') or {}; chat=message.get('chat') or {}
    user=sender.get('id')
    if (type(user) is not int or sender.get('is_bot') is not False or chat.get('type')!='private'
        or type(chat.get('id')) is not int or chat['id']!=user or 'forward_origin' in message): return
    try: connector=current(connector['id'],user)
    except ValueError: return
    value=q.get('data','')
    if not isinstance(value,str) or not re.fullmatch(r'a:[A-Za-z0-9_-]{32}',value): return
    record=db.get('telegram_callback',value[2:])
    token=social._telegram_token(social._secrets(connector))
    bot_sender=message.get('from') or {}
    if (not record or record['connector_id']!=connector['id'] or record['user_id']!=user
        or record.get('fingerprint')!=fingerprint(connector) or not record.get('active') or record.get('used')
        or record['expires']<time.time() or record.get('message_id')!=message.get('message_id')
        or bot_sender.get('is_bot') is not True or bot_sender.get('id')!=int(token.split(':')[0])):
        await send(connector,user,'Кнопка устарела или уже использована. Запросите /task <id>.'); return
    task=owned_task(record['task_id'],connector['id'],user)
    if (task.get('intake') or {}).get('revision') != record.get('revision'):
        raise ValueError('Предложение изменилось. Запросите /task <id>.')
    action=record['action']; publication=None
    if record.get('publication_id'):
        publication=db.get('publication',record['publication_id'])
        target=db.get('connector',publication['connector_id']) if publication else None
        if (not publication or publication.get('task_id')!=task['id'] or publication['updated_at']!=record['publication_version']
            or not target or fingerprint(target)!=record.get('target_fingerprint')):
            raise ValueError('Черновик или подключение изменились. Запросите /task <id> и подготовьте актуальный черновик.')
    # Claim before any await, including answerCallbackQuery; never repeat an action.
    db.update('telegram_callback',record['id'],{'used':True})
    try: await social._telegram(token,'answerCallbackQuery',{'callback_query_id':q['id'],'text':'Принято'})
    except (ValueError,KeyError): pass
    connector=current(connector['id'],user)
    if fingerprint(connector)!=record['fingerprint']: raise ValueError('Настройки изменились.')
    if publication:
        latest=db.get('publication',publication['id'])
        target=db.get('connector',publication['connector_id'])
        if not latest or latest['updated_at']!=record['publication_version'] or not target or fingerprint(target)!=record['target_fingerprint']:
            raise ValueError('Черновик или подключение изменились; откройте их заново.')
    if action=='pub_view':
        await show_publication(connector,user,task,publication)
    elif action=='approve':
        await workflow.approve(task['id'],record['revision'])
        await send(connector,user,'Одобрено. Оркестратор готовит план и запускает агентов. Результат пришлю сюда.')
    elif action=='reject':
        workflow.reject(task['id'],record['revision']); await send(connector,user,'Идея отклонена. Агентов не запускаю.')
    elif action in {'revise','edit_text','edit_media','edit_title'}:
        db.put('telegram_session',{'id':session_id(connector['id'],user),'action':action,'record':record,'expires':time.time()+3600})
        await send(connector,user,{'revise':'Пришлите правки к идее текстом или голосом.', 'edit_text':'Пришлите полный новый текст публикации.', 'edit_media':'Пришлите публичный HTTPS URL готового изображения/видео; «-» удалит медиа.', 'edit_title':'Пришлите новый заголовок.'}[action]+'\n/new — отменить ввод правок.')
    elif action=='targets':
        if task.get('status')!='done': raise ValueError('Результат ещё не готов.')
        targets=[c for c in db.list('connector') if c.get('enabled',True) and c.get('kind') in {'telegram','vk','instagram','youtube','webhook'} and (c.get('kind')!='telegram' or c.get('config',{}).get('chat_id'))]
        buttons=[[button(connector,user,str(c.get('name') or c['kind'])[:60],'draft',task,target_id=c['id'],target_fingerprint=fingerprint(c))] for c in targets[:30]]
        await send(connector,user,'Выберите канал для черновика. Публикации ещё не будет.' if buttons else 'Сначала добавьте канал в разделе «Подключения» веб-панели.',buttons)
    elif action=='draft':
        from app.publications import create_publication
        target=db.get('connector',record['target_id'])
        if not target or fingerprint(target)!=record['target_fingerprint'] or not target.get('enabled',True): raise ValueError('Подключение изменилось.')
        publication=create_publication({'task_id':task['id'],'connector_id':target['id'],'title':str(task.get('title',''))[:100],'text':str(task.get('result') or ''),'media_url':''})
        await show_publication(connector,user,task,publication)
    elif action in {'pub_approve','pub_publish','pub_reject'}:
        from app.publications import approve_publication,publish_publication,reject_publication
        if action=='pub_approve': publication=approve_publication(publication['id'],expected_updated_at=record['publication_version'])
        elif action=='pub_reject': publication=reject_publication(publication['id'],expected_updated_at=record['publication_version'])
        else: publication=await publish_publication(publication['id'],expected_updated_at=record['publication_version'])
        await show_publication(connector,user,task,publication)


async def handle_update(update,connector,engine,workflow):
    connector=current(connector['id'])
    if 'callback_query' in update: return await handle_callback(update,connector,engine,workflow)
    message=authorized_message(update,connector.get('config') or {})
    if not message: return
    user=message['from']['id']; text=message.get('text')
    if message.get('voice'):
        rate_key='telegram_voice_rate_'+session_id(connector['id'],user)
        if time.time()-db.setting(rate_key,0)<15: raise ValueError('Подождите 15 секунд между голосовыми сообщениями.')
        db.set_setting(rate_key,time.time())
        text=await transcribe(message,connector)
        await send(connector,user,'Распознано (проверьте перед одобрением):\n'+text)
    if not isinstance(text,str) or not text.strip():
        return await send(connector,user,'Отправьте идею текстом или голосовым сообщением.')
    text=text.strip()
    if len(text)>12000: raise ValueError('До 12000 символов в сообщении.')
    connector=current(connector['id'],user)
    if text.startswith('/') and not message.get('voice'):
        parts=text.split(maxsplit=1); command=parts[0].split('@')[0].lower(); argument=parts[1] if len(parts)>1 else ''
        if command in {'/start','/help'}: return await send(connector,user,HELP+'\n/status — состояние ОС; /newchat — новый диалог\n/approvals — запросы инструментов; /tool ID — параметры; /approve ID — выполнить; /reject ID — отказать')
        if command=='/newchat':
            db.delete('setting','bot-chat:'+connector['id']+':'+str(user))
            return await send(connector,user,'Новый диалог готов. Пришлите сообщение.')
        if command=='/status':
            from app.workspace import system_status
            status=await system_status()
            return await send(connector,user,'ОС на связи. Активных задач: '+str(status['tasks_running'])+'; ожидают согласования: '+str(status['pending_approvals']))
        if command=='/new':
            db.delete('telegram_session',session_id(connector['id'],user)); return await send(connector,user,'Пришлите новую идею.')
        if command in {'/approvals','/tool','/approve','/reject'}:
            return await tool_command(connector,user,command,argument)
        if command=='/tasks':
            tasks=[t for t in db.list('task') if t.get('telegram')=={'connector_id':connector['id'],'user_id':user}][:15]
            return await send(connector,user,'\n'.join(f"{t['id'][:8]} · {t.get('status')} · {t.get('title','')}" for t in tasks) or 'Пока нет задач. Пришлите идею.')
        if command in {'/task','/result','/pause','/resume','/cancel'}:
            task=resolve_task(argument,connector['id'],user)
            if command in {'/task','/result'}: return await show_task(connector,user,task)
            if command=='/resume' and (task.get('intake') or {}).get('state')!='approved': raise ValueError('Сначала одобрите предложение.')
            changed=await engine.action(task['id'],command[1:]); return await send(connector,user,f"{task['id'][:8]} · {changed['status']}")
        return await send(connector,user,HELP)
    session=db.get('telegram_session',session_id(connector['id'],user))
    if session and session['expires']>time.time():
        record=session['record']; task=owned_task(record['task_id'],connector['id'],user)
        if record.get('fingerprint')!=fingerprint(connector): raise ValueError('Настройки изменились; /new отменит ввод правок.')
        if session['action']=='revise':
            workflow.revise(task['id'],text,record['revision'])
            db.delete('telegram_session',session['id']); return await send(connector,user,'Правки приняты. Агент подготовит новое предложение для одобрения.')
        from app.publications import patch_publication
        publication=db.get('publication',record['publication_id'])
        if not publication or publication['updated_at']!=record['publication_version']: raise ValueError('Черновик изменился; /new отменит ввод правок.')
        field={'edit_text':'text','edit_media':'media_url','edit_title':'title'}[session['action']]
        publication=patch_publication(publication['id'],{field:'' if field=='media_url' and text=='-' else text},expected_updated_at=record['publication_version'])
        db.delete('telegram_session',session['id']); return await show_publication(connector,user,task,publication)
    if connector.get('config',{}).get('mode')=='chat':
        from app.main import app
        from app.workspace import create_chat
        key='bot-chat:'+connector['id']+':'+str(user)
        chat_id=db.setting(key)
        chat=db.get('chat',chat_id) if chat_id else None
        config=connector.get('config',{})
        if not chat or chat.get('agent_id')!=config.get('agent_id') or chat.get('project_id','')!=config.get('project_id',''):
            chat=create_chat({'title':connector['name']+' · Telegram','agent_id':config['agent_id'],'project_id':config.get('project_id','')})
            db.set_setting(key,chat['id'])
        app.state.workspace.message(chat['id'],text)
        db.put('telegram_chat_reply',{'id':chat['id'],'connector_id':connector['id'],'user_id':user,'status':'waiting'})
        return await send(connector,user,'Сообщение передано агенту. Если понадобятся инструменты, запрос появится в разделе «Согласования» веб-панели и в /approvals.')
    pending=[t for t in db.list('task') if t.get('telegram')=={'connector_id':connector['id'],'user_id':user} and t.get('intake',{}).get('state') in {'queued','analyzing'}]
    if len(pending)>=5: raise ValueError('В обработке уже 5 идей. Дождитесь предложений.')
    task=workflow.create_idea(text,config=connector.get('config') or {},source='telegram',connector_id=connector['id'],user_id=user)
    await send(connector,user,f"Идея {task['id'][:8]} появилась на дашборде. Агент разбирает задачу; пришлю предложение и спрошу, выполнять ли его.")


async def notify_once():
    for reply in db.list('telegram_chat_reply'):
        if reply.get('status')!='waiting':continue
        chat=db.get('chat',reply['id'])
        if not chat or chat.get('status')=='running':continue
        db.update('telegram_chat_reply',reply['id'],{'status':'sending'})
        try:
            connector=current(reply['connector_id'],reply['user_id'])
            answer=chat.get('error') or (chat['messages'][-1]['content'] if chat.get('messages') else 'Нет ответа.')
            await send(connector,reply['user_id'],answer)
            db.update('telegram_chat_reply',reply['id'],{'status':'sent'})
        except Exception:
            db.update('telegram_chat_reply',reply['id'],{'status':'uncertain'})

    for task in db.list('task'):
        route=task.get('telegram') or {}; user=route.get('user_id')
        if not route.get('connector_id'): continue
        try: connector=current(route['connector_id'],user)
        except ValueError: continue
        if not connector.get('config',{}).get('notifications_enabled',True): continue
        intake=task.get('intake') or {}; state=intake.get('state')
        status=task.get('status')
        if state not in {'review','failed'} and status not in {'done','failed','cancelled','paused'}: continue
        marker=f"notice:{connector['id']}:{user}:{task['id']}:{intake.get('revision')}:{state}:{status}"
        if db.get('telegram_notice',marker): continue
        # Durable claim before building/sending; uncertain deliveries require /task.
        db.put('telegram_notice',{'id':marker,'status':'sending'})
        try:
            await show_task(connector,user,task)
            db.update('telegram_notice',marker,{'status':'sent'})
        except Exception:
            db.update('telegram_notice',marker,{'status':'unknown'})
            db.update('connector',connector['id'],{'last_error':'Уведомление не подтверждено. Запросите /task в боте; повтор автоматически отключён.'})

    for publication in db.list('publication'):
        task=db.get('task',publication.get('task_id'))
        route=(task or {}).get('telegram') or {}; user=route.get('user_id')
        if not route.get('connector_id') or publication.get('status')=='publishing': continue
        try: connector=current(route['connector_id'],user)
        except ValueError: continue
        if not connector.get('config',{}).get('notifications_enabled',True): continue
        marker=publication_notice_id(connector['id'],user,publication)
        if db.get('telegram_notice',marker): continue
        try: await show_publication(connector,user,task,publication)
        except Exception:
            db.update('connector',connector['id'],{'last_error':'Доставка черновика не подтверждена. Запросите /task в боте.'})


async def delay(stop,seconds):
    try: await asyncio.wait_for(stop.wait(),seconds)
    except TimeoutError: pass


async def worker(connector,stop,engine,workflow):
    token=social._telegram_token(social._secrets(connector))
    offset_key='telegram_offset_'+connector['id']+'_'+hashlib.sha256(token.encode()).hexdigest()[:12]
    offset=db.setting(offset_key)
    if offset is None:
        old=await social._telegram(token,'getUpdates',{'offset':-1,'limit':1,'timeout':0,'allowed_updates':['message','callback_query']})
        if not isinstance(old,list): raise ValueError('Некорректная очередь Telegram.')
        offset=max([x['update_id']+1 for x in old if isinstance(x,dict) and type(x.get('update_id')) is int],default=0)
        db.set_setting(offset_key,offset)
    while not stop.is_set():
        updates=await social._telegram(token,'getUpdates',{'offset':offset,'limit':30,'timeout':25,'allowed_updates':['message','callback_query']},timeout=35)
        if not isinstance(updates,list): raise ValueError('Некорректная очередь Telegram.')
        db.update('connector',connector['id'],{'last_poll_at':now(),'last_error':''})
        for update in updates:
            if not isinstance(update,dict) or type(update.get('update_id')) is not int or update['update_id']<offset: continue
            offset=update['update_id']+1; db.set_setting(offset_key,offset)
            try: await handle_update(update,connector,engine,workflow)
            except Exception as exc:
                fresh=current(connector['id']); message=authorized_message(update,fresh.get('config') or {})
                user=message['from']['id'] if message else (update.get('callback_query') or {}).get('from',{}).get('id')
                try:
                    current(connector['id'],user)
                    await send(fresh,user,str(exc)[:1000] if isinstance(exc,ValueError) else 'Действие не завершилось. Проверьте задачу в веб-панели; неизвестный результат не повторяется автоматически.')
                except Exception: pass


async def telegram_loop(stop_event,engine,workflow=None):
    if workflow is None:
        from app.workflow import Workflow
        workflow=Workflow(engine)
    workers={}
    async def notifications():
        while not stop_event.is_set():
            await notify_once(); await delay(stop_event,2)
    notifier=asyncio.create_task(notifications())
    try:
        while not stop_event.is_set():
            connectors={c['id']:c for c in db.list('connector') if c.get('kind')=='telegram' and c.get('enabled',True)
                        and c.get('config',{}).get('controller_enabled',True) and c.get('config',{}).get('allowed_user_ids')}
            for id in list(workers):
                fp,job=workers[id]
                if id not in connectors or fp!=fingerprint(connectors[id]) or job.done():
                    if job.done() and not job.cancelled() and job.exception() and id in connectors:
                        db.update('connector',id,{'last_error':'Telegram-контроллер остановлен: проверьте токен, сеть и отсутствие второго экземпляра бота.'})
                    job.cancel(); await asyncio.gather(job,return_exceptions=True); del workers[id]
            for id,c in connectors.items():
                if id not in workers: workers[id]=(fingerprint(c),asyncio.create_task(worker(c,stop_event,engine,workflow)))
            await delay(stop_event,3)
    finally:
        notifier.cancel()
        for _,job in workers.values(): job.cancel()
        await asyncio.gather(notifier,*(job for _,job in workers.values()),return_exceptions=True)


async def tool_command(connector,user,command,argument):
    from app import toolbus
    bot_id=connector['id']
    own_chat=db.setting('bot-chat:'+bot_id+':'+str(user))
    def owned(r):
        if r.get('chat_id') and r['chat_id']==own_chat:return True
        task=db.get('task',r.get('task_id',''))
        return bool(task and task.get('telegram')=={'connector_id':bot_id,'user_id':user})
    pending=[r for r in db.list('tool_request') if r['status']=='pending' and owned(r)]
    if command=='/approvals':
        return await send(connector,user,'\n'.join(r['id'][:12]+' · '+r['tool']+' · /tool '+r['id'][:12] for r in pending) or 'Запросов инструментов нет.')
    if not re.fullmatch(r'[a-f0-9]{8,32}',argument):raise ValueError('Укажите ID запроса из /approvals.')
    matches=[r for r in pending if r['id'].startswith(argument)]
    if len(matches)!=1:raise ValueError('Запрос не найден или ID неоднозначен.')
    record=matches[0];key='tool-preview:'+bot_id+':'+str(user)+':'+record['id']
    if command=='/tool':
        server=db.get('mcp',record['server_id'])
        await send(connector,user,'Инструмент: '+record['tool']+'\nСервер: '+server['url']+'\nАргументы:\n'+json.dumps(record['arguments'],ensure_ascii=False,indent=2)+'\n\nДля выполнения: /approve '+record['id'][:12]+'\nДля отказа: /reject '+record['id'][:12])
        db.set_setting(key,{'binding':record['binding'],'connector':fingerprint(connector),'expires':time.time()+300})
        return
    if command=='/reject':
        toolbus.reject(record['id']);db.delete('setting',key)
        return await send(connector,user,'Действие отклонено.')
    preview=db.setting(key)
    if not preview or preview['expires']<time.time() or preview['binding']!=record['binding'] or preview['connector']!=fingerprint(connector):
        raise ValueError('Сначала просмотрите актуальные аргументы: /tool '+record['id'][:12])
    db.delete('setting',key)
    result=await toolbus.execute(record['id'])
    labels={'done':'выполнено','failed':'ошибка','uncertain':'исход неизвестен, проверьте сервис'}
    return await send(connector,user,'Действие: '+labels.get(result['status'],result['status'])+'\n'+result.get('error',''))
