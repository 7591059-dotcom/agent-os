#!/usr/bin/env python3
"""Generate a new private configuration; never overwrite an existing master key."""
import argparse
import getpass
import ipaddress
import os
from pathlib import Path
import re
import sys
import warnings

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def domain_name(value):
    value = value.strip().lower()
    if len(value) > 253 or '.' not in value or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
        for part in value.split('.')
    ):
        raise ValueError('Введите домен без https://, порта и пути: agents.example.com')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError('Для этого HTTPS-развёртывания нужен домен, а не IP-адрес.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Первичная настройка Агентной. Пароль вводится скрыто.')
    parser.add_argument('--local', action='store_true', help='HTTP только на localhost')
    parser.add_argument('--demo', action='store_true', help='Локальный макет провайдера без внешних API')
    parser.add_argument('--domain', help='Домен сервера без схемы и пути')
    parser.add_argument('--username', default='sergey', help='Логин владельца (по умолчанию sergey)')
    parser.add_argument('--port', type=int, default=8000, help='Порт для --local/--demo')
    parser.add_argument('--output', type=Path, default=ROOT / '.env', help='Новый файл .env; существующий не заменяется')
    args = parser.parse_args(argv)
    local = args.local or args.demo
    if args.domain and local:
        parser.error('--domain нельзя совмещать с --local/--demo')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', args.username):
        parser.error('Логин: от 1 до 64 латинских букв, цифр и символов _ . -')
    if not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535')
    target = args.output.absolute()
    if target.exists() or target.is_symlink():
        parser.error(f'{target} уже существует. Скрипт не заменяет ключ шифрования и настройки.')
    if not target.parent.is_dir():
        parser.error('Каталог для конфигурации не существует')
    try:
        domain = 'localhost' if local else domain_name(args.domain or input('Домен сервера: '))
        with warnings.catch_warnings():
            # Fail instead of getpass falling back to a terminal with echoed input.
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('Придумайте пароль владельца (минимум 16 символов): ')
            if not 16 <= len(password) <= 1024 or password != password.strip():
                raise ValueError('Пароль: 16–1024 символа, без пробелов по краям.')
            if password != getpass.getpass('Повторите пароль: '):
                raise ValueError('Пароли не совпадают.')
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        parser.error('Нужен интерактивный терминал со скрытым вводом пароля. Запустите команду вручную.')
    except ValueError as exc:
        parser.error(str(exc))
    password_hash = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(password)
    del password
    public_url = f'http://localhost:{args.port}' if local else f'https://{domain}'
    values = {
        'APP_DOMAIN': domain,
        'PUBLIC_URL': public_url,
        'ADMIN_USERNAME': args.username,
        'ADMIN_PASSWORD_HASH': password_hash,
        'MASTER_KEY': Fernet.generate_key().decode(),
        'SECURE_COOKIES': 'false' if local else 'true',
        'DEMO_MODE': 'true' if args.demo else 'false',
        'APP_DATA_DIR': 'data',
    }
    text = '# Private configuration. Back up separately; never commit or share.\n'
    text += ''.join(f"{key}='{value}'\n" for key, value in values.items())
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    print(f'Настройки созданы: {target}\nЛогин: {args.username}\nАдрес: {public_url}')
    print('Пароль и ключ не выводятся. Сохраните .env отдельно от копий базы в защищённом месте.')
    if args.demo:
        print('ДЕМО: используется локальный макет ответов. Реальные ИИ и соцсети не вызываются автоматически.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f'Не удалось записать настройки: {exc.strerror}', file=sys.stderr)
        raise SystemExit(1)
