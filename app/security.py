"""Owner authentication, encrypted credentials and session helpers."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.fernet import Fernet

from app.db import db, uid

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def encrypt(value):
    return Fernet(os.environ['MASTER_KEY'].encode()).encrypt(value.encode()).decode()


def decrypt(value):
    return Fernet(os.environ['MASTER_KEY'].encode()).decrypt(value.encode()).decode() if value else ''


def check_password(password):
    try:
        return password_hasher.verify(os.environ['ADMIN_PASSWORD_HASH'], password)
    except (VerificationError, InvalidHashError):
        return False


def secure_cookies():
    return os.getenv('SECURE_COOKIES', 'true').lower() == 'true'


def demo_mode():
    return os.getenv('DEMO_MODE', 'false').lower() == 'true'


def public_origin():
    return os.environ['PUBLIC_URL'].rstrip('/')


def validate_environment():
    for key in ('MASTER_KEY', 'ADMIN_PASSWORD_HASH', 'PUBLIC_URL'):
        if not os.getenv(key):
            raise RuntimeError(f'{key} is required. Run scripts/configure.py first.')
    Fernet(os.environ['MASTER_KEY'].encode())
    origin = urlparse(public_origin())
    if origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('', '/'):
        raise RuntimeError('PUBLIC_URL must contain only scheme and host, optionally a port.')
    if origin.scheme not in ('https', 'http') or not origin.hostname:
        raise RuntimeError('Invalid PUBLIC_URL')
    if origin.scheme != 'https' and origin.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('Public access requires HTTPS. For local use set PUBLIC_URL=http://localhost:8000.')
    if not secure_cookies() and origin.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('SECURE_COOKIES=false is allowed only on localhost.')
    if not os.environ['ADMIN_PASSWORD_HASH'].startswith('$argon2id$'):
        raise RuntimeError('ADMIN_PASSWORD_HASH must be an Argon2id hash.')


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_session():
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    db.put('session', {'id': token_hash(token), 'csrf_token': csrf, 'expires_at': time.time() + 12 * 3600,
                       'user': os.getenv('ADMIN_USERNAME', 'sergey')})
    return token, csrf


def get_session(token):
    if not token or len(token) > 200:
        return None
    session = db.get('session', token_hash(token))
    if session and session['expires_at'] > time.time():
        return session
    if session:
        db.delete('session', session['id'])
    return None


def totp(secret, counter=None):
    counter = int(time.time() // 30) if counter is None else counter
    key = base64.b32decode(secret.upper() + '=' * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff
    return f'{number % 1000000:06d}'


def verify_totp(secret, code, *, prevent_replay=True):
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    current = int(time.time() // 30)
    last = db.setting('totp_last_counter', -1) if prevent_replay else -1
    for counter in (current, current - 1, current + 1):
        if counter > last and hmac.compare_digest(totp(secret, counter), code):
            if prevent_replay:
                db.set_setting('totp_last_counter', counter)
            return True
    return False


def login_allowed(ip):
    key = 'login_' + token_hash(ip)
    failures = db.setting(key, [])
    failures = [t for t in failures if time.time() - t < 900]
    total = [t for t in db.setting('login_global', []) if time.time() - t < 60]
    return len(failures) < 8 and len(total) < 40


def record_login_failure(ip):
    for key, period in [('login_' + token_hash(ip), 900), ('login_global', 60)]:
        timestamps = [t for t in db.setting(key, []) if time.time() - t < period]
        db.set_setting(key, (timestamps + [time.time()])[-50:])


def sanitize_provider(doc):
    return {**{k: v for k, v in doc.items() if k != 'api_key_encrypted'}, 'has_api_key': bool(doc.get('api_key_encrypted'))}


def sanitize_connector(doc):
    return {**{k: v for k, v in doc.items() if k != 'secret_encrypted'}, 'has_secrets': bool(doc.get('secret_encrypted'))}
