#!/usr/bin/env python3
"""Reset owner access from a trusted, offline server console; preserve MASTER_KEY."""
import argparse
from contextlib import closing
import getpass
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import sys
import warnings

from argon2 import PasswordHasher

from backup import ROOT, validate_database
sys.path.insert(0, str(ROOT))
from app.process_lock import lock_file, check_private_file, sync_directory


def main():
    parser = argparse.ArgumentParser(description='Смена пароля с консоли остановленного сервера; ключ сохраняется')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--data-dir', type=Path, default=Path(os.getenv('APP_DATA_DIR', ROOT / 'data')))
    parser.add_argument('--reset-2fa', action='store_true', help='Также отключить потерянный TOTP; после входа настройте заново')
    args = parser.parse_args()
    lock = None
    temporary = None
    try:
        if args.env_file.is_symlink():
            raise ValueError('.env не должен быть символической ссылкой.')
        env_file = args.env_file.resolve(strict=True)
        info = env_file.stat()
        check_private_file(env_file)
        contents = env_file.read_text(encoding='utf-8')
        if len(re.findall(r"^ADMIN_PASSWORD_HASH='[^'\n]+'$", contents, re.MULTILINE)) != 1:
            raise ValueError('В .env должен быть ровно один ADMIN_PASSWORD_HASH в одинарных кавычках.')
        data_dir = args.data_dir.resolve(strict=True)
        database = (data_dir / 'agentos.sqlite3').resolve(strict=True)
        lock = os.open(data_dir / 'application.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_file(lock)
        except BlockingIOError:
            raise ValueError('Приложение работает. Сначала остановите его; изменения не внесены.') from None
        with closing(sqlite3.connect(database, timeout=30)) as connection:
            validate_database(connection)
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('Новый пароль владельца (минимум 16 символов): ')
                if not 16 <= len(password) <= 1024 or password != password.strip():
                    raise ValueError('Пароль: 16–1024 символа, без пробелов по краям.')
                if password != getpass.getpass('Повторите новый пароль: '):
                    raise ValueError('Пароли не совпадают.')
            encoded = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(password)
            del password
            replacement = re.sub(r"^ADMIN_PASSWORD_HASH='[^'\n]+'$",
                                 lambda _: f"ADMIN_PASSWORD_HASH='{encoded}'", contents, flags=re.MULTILINE)
            fd, name = tempfile.mkstemp(prefix='.env-reset-', dir=env_file.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                if os.name != 'nt':
                    os.fchmod(stream.fileno(), 0o600)
                    if (os.geteuid(), os.getegid()) != (info.st_uid, info.st_gid):
                        os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            with connection:
                connection.execute("DELETE FROM documents WHERE kind='session'")
                connection.execute("DELETE FROM documents WHERE kind='setting' AND id IN ('totp_pending','login_global')")
                connection.execute("DELETE FROM documents WHERE kind='setting' AND id LIKE 'login_%'")
                if args.reset_2fa:
                    connection.execute("DELETE FROM documents WHERE kind='setting' AND id IN ('totp_secret','totp_last_counter')")
                os.replace(temporary, env_file)
            sync_directory(env_file.parent)
        print('Пароль обновлён; веб-сессии сброшены. MASTER_KEY и остальные настройки сохранены.')
        if args.reset_2fa:
            print('2FA отключена по вашему флагу. Включите её заново после входа.')
        else:
            print('Действующая 2FA сохранена.')
        print('Теперь запустите приложение.')
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        parser.error('Нужен интерактивный терминал со скрытым вводом пароля.')
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    main()
