"""Run DOM checks against an isolated disposable demo server; no user secrets."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import threading
import urllib.request

from argon2 import PasswordHasher
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def serve_demo(port):
    """A test-only process with graceful shutdown over its private stdin pipe."""
    if os.environ.get('DEMO_MODE') != 'true' or not os.environ.get('UI_TEST_AUTH_FILE'):
        raise RuntimeError('Для проверки нужен изолированный демонстрационный сервер.')
    sys.path.insert(0, str(ROOT))
    import uvicorn
    server = uvicorn.Server(uvicorn.Config('app.main:app', host='127.0.0.1', port=port,
                                          access_log=False, proxy_headers=False))
    def stop_on_input():
        sys.stdin.readline()
        server.should_exit = True
    threading.Thread(target=stop_on_input, daemon=True).start()
    server.run()


def main():
    with tempfile.TemporaryDirectory(prefix='agentos-ui-') as folder:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        password = secrets.token_urlsafe(32)
        auth = Path(folder) / 'test-auth.json'
        auth.write_text(json.dumps({'username': 'ui_test_owner', 'password': password}))
        auth.chmod(0o600)
        origin = f'http://localhost:{port}'
        env = {**os.environ, 'MASTER_KEY': Fernet.generate_key().decode(),
               'ADMIN_PASSWORD_HASH': PasswordHasher().hash(password), 'ADMIN_USERNAME': 'ui_test_owner',
               'PUBLIC_URL': origin, 'SECURE_COOKIES': 'false', 'DEMO_MODE': 'true',
               'APP_DATA_DIR': str(Path(folder) / 'data'), 'UI_TEST_URL': origin,
               'UI_TEST_AUTH_FILE': str(auth)}
        server = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve-demo', str(port)],
                                  cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ready = False
            for _ in range(100):
                try:
                    with urllib.request.urlopen(origin + '/api/health', timeout=1) as response:
                        ready = response.status == 200
                    if ready:
                        break
                except Exception:
                    time.sleep(.05)
            if not ready:
                raise RuntimeError('Disposable demo server did not start')
            return subprocess.run(['node', 'tests/ui-smoke.cjs'], cwd=ROOT, env=env).returncode
        finally:
            if server.poll() is None:
                try:
                    server.stdin.write(b'\n')
                    server.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                else:
                    server.kill()
                server.wait()
            finally:
                server.stdin.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--serve-demo':
        serve_demo(int(sys.argv[2]))
    else:
        raise SystemExit(main())
