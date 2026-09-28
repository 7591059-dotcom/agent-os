#!/usr/bin/env python3
"""Load generated .env literally and run one worker, bound to loopback only."""
import argparse
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.process_lock import check_private_file


def load_environment(path):
    check_private_file(path)
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError(f'Некорректная строка {number} в конфигурации')
        if len(value) < 2 or not (value.startswith("'") and value.endswith("'")) or "'" in value[1:-1]:
            raise ValueError(f'Строка {number}: значение должно быть в одинарных кавычках')
        # No variable expansion, shell sourcing or eval: Argon2id $ remains literal.
        os.environ[key] = value[1:-1]


def main():
    parser = argparse.ArgumentParser(description='Локальный запуск на localhost, один процесс')
    parser.add_argument('--env', type=Path, default=ROOT / '.env')
    args = parser.parse_args()
    try:
        load_environment(args.env)
        origin = urlparse(os.environ.get('PUBLIC_URL', ''))
        if origin.scheme != 'http' or origin.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('Для локального запуска создайте настройки через configure.py --local или --demo')
        port = origin.port or 8000
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    import uvicorn
    print(f'Откройте {os.environ["PUBLIC_URL"]}; остановка: Ctrl+C')
    uvicorn.run('app.main:app', host='127.0.0.1', port=port, workers=1,
                proxy_headers=False, access_log=False, timeout_graceful_shutdown=40)


if __name__ == '__main__':
    main()
