"""The same owner approval boundary for web and Telegram publishing."""
import asyncio
import hashlib
import json
from urllib.parse import urlparse

from app.db import db, now, uid

_active = False
FIELDS = {'task_id': 100, 'connector_id': 100, 'text': 40000, 'media_url': 2048, 'title': 200}


def require(kind, identifier):
    value = db.get(kind, identifier)
    if value is None:
        raise ValueError('Не найдено: ' + kind)
    return value


def connector_fingerprint(connector):
    fields = {k: connector.get(k) for k in ('id', 'kind', 'config', 'secret_encrypted', 'enabled')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def content_fingerprint(publication):
    return hashlib.sha256(json.dumps({k: publication.get(k, '') for k in FIELDS}, sort_keys=True).encode()).hexdigest()


def _version(publication, expected_updated_at):
    if expected_updated_at is not None and publication.get('updated_at') != expected_updated_at:
        raise ValueError('Черновик изменился. Откройте актуальный текст и подтвердите его заново.')


def _validate(data):
    if not isinstance(data, dict) or set(data) - set(FIELDS):
        raise ValueError('Недопустимые поля публикации')
    clean = {k: data.get(k, '') for k in FIELDS}
    if any(not isinstance(v, str) or len(v) > FIELDS[k] for k, v in clean.items()):
        raise ValueError('Некорректный текст или размер поля публикации')
    if not clean['connector_id']:
        raise ValueError('Выберите канал публикации')
    connector = require('connector', clean['connector_id'])
    if connector['kind'] == 'telegram' and not connector.get('config', {}).get('chat_id'):
        raise ValueError('Для публикаций в Telegram задайте chat_id канала в настройках подключения')
    if clean['task_id']:
        require('task', clean['task_id'])
    if clean['media_url']:
        parsed = urlparse(clean['media_url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Медиа: нужен публичный HTTPS URL без логина и пароля')
    return clean


def create_publication(data):
    return db.put('publication', {'id': uid(), **_validate(data), 'status': 'draft', 'external_id': '', 'error': ''})


def patch_publication(identifier, data, expected_updated_at=None):
    if not isinstance(data, dict) or set(data) - set(FIELDS):
        raise ValueError('Недопустимые поля публикации')
    def change(current):
        _version(current, expected_updated_at)
        if current['status'] not in ('draft', 'failed', 'approved'):
            raise ValueError('Публикация уже отправляется, отправлена или отклонена')
        clean = _validate({**{k: current.get(k, '') for k in FIELDS}, **data})
        return {**current, **clean, 'status': 'draft', 'approved_at': None,
                'connector_fingerprint': None, 'content_fingerprint': None, 'error': ''}
    result = db.mutate('publication', identifier, change)
    if result is None:
        raise ValueError('Публикация не найдена')
    return result


def approve_publication(identifier, expected_updated_at=None):
    def change(p):
        _version(p, expected_updated_at)
        if p['status'] != 'draft':
            raise ValueError('Подтвердить можно только черновик')
        connector = require('connector', p['connector_id'])
        if not connector.get('enabled', True):
            raise ValueError('Интеграция отключена')
        if not p.get('text', '').strip() and not p.get('media_url'):
            raise ValueError('Добавьте текст или медиа в черновик')
        if connector['kind'] in ('instagram', 'youtube') and not p.get('media_url'):
            raise ValueError('Для этой площадки добавьте HTTPS URL готового изображения или видео')
        if connector['kind'] == 'telegram':
            if not connector.get('config', {}).get('chat_id'):
                raise ValueError('Укажите chat_id канала Telegram')
            if p.get('media_url') or len(p.get('text', '').encode('utf-16-le')) // 2 > 4096:
                raise ValueError('Telegram: текст до 4096 символов UTF-16; медиа пока не поддерживаются')
        return {**p, 'status': 'approved', 'approved_at': now(),
                'connector_fingerprint': connector_fingerprint(connector),
                'content_fingerprint': content_fingerprint(p)}
    result = db.mutate('publication', identifier, change)
    if result is None:
        raise ValueError('Публикация не найдена')
    db.event(result.get('task_id', ''), 'Владелец подтвердил публикацию', publication_id=identifier)
    return result


def reject_publication(identifier, expected_updated_at=None):
    def change(p):
        _version(p, expected_updated_at)
        if p['status'] not in ('draft', 'approved', 'failed'):
            raise ValueError('Отправленную или уже отправляющуюся публикацию отклонить нельзя')
        return {**p, 'status': 'rejected', 'approved_at': None, 'connector_fingerprint': None,
                'content_fingerprint': None}
    result = db.mutate('publication', identifier, change)
    if result is None:
        raise ValueError('Публикация не найдена')
    db.event(result.get('task_id', ''), 'Владелец отклонил публикацию', publication_id=identifier)
    return result


async def publish_publication(identifier, expected_updated_at=None):
    global _active
    from app.integrations import publish, IntegrationError
    if _active:
        raise ValueError('Другая публикация уже отправляется. Дождитесь результата.')
    def claim(p):
        _version(p, expected_updated_at)
        if p['status'] != 'approved' or not p.get('approved_at'):
            raise ValueError('Сначала подтвердите публикацию. Повторная отправка заблокирована.')
        if p.get('connector_fingerprint') != connector_fingerprint(require('connector', p['connector_id'])):
            raise ValueError('Интеграция изменилась после подтверждения. Отредактируйте и подтвердите черновик заново.')
        if p.get('content_fingerprint') != content_fingerprint(p):
            raise ValueError('Содержимое не подтверждено в этой версии. Отредактируйте и подтвердите черновик заново.')
        return {**p, 'status': 'publishing', 'error': ''}
    p = db.mutate('publication', identifier, claim)
    if p is None:
        raise ValueError('Публикация не найдена')
    connector = require('connector', p['connector_id'])
    _active = True
    try:
        result = await publish(p, connector)
        result = {k: v for k, v in result.items() if k in ('external_id', 'url')}
        p = db.update('publication', identifier, {**result, 'status': 'published', 'published_at': now()})
        db.event(p.get('task_id', ''), 'Публикация отправлена', publication_id=identifier)
        return p
    except asyncio.CancelledError:
        db.update('publication', identifier, {'status': 'uncertain',
            'error': 'Отправка прервана. Проверьте внешний сервис; автоматического повтора нет.'})
        db.event(p.get('task_id', ''), 'Отправка прервана; результат не подтверждён', level='warning', publication_id=identifier)
        raise
    except Exception as exc:
        status = ('uncertain' if exc.uncertain else 'failed') if isinstance(exc, IntegrationError) else 'uncertain'
        message = str(exc) if isinstance(exc, ValueError) else 'Ошибка отправки. Проверьте соцсеть перед повторной публикацией.'
        db.update('publication', identifier, {'status': status, 'error': message})
        db.event(p.get('task_id', ''), 'Отправка не подтверждена', level='warning', publication_id=identifier)
        raise ValueError(message) from None
    finally:
        _active = False
