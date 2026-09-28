"""Small transactional document repository backed by SQLite WAL."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


class Database:
    def __init__(self):
        self._lock = threading.RLock()
        self.connection = None

    def close(self):
        with self._lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None

    def init(self, path):
        with self._lock:
            if self.connection is not None:
                self.connection.close()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('PRAGMA busy_timeout=30000')
            self.connection.execute('CREATE TABLE IF NOT EXISTS documents (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(kind,id))')
            self.connection.execute('CREATE INDEX IF NOT EXISTS documents_kind_created ON documents(kind,created_at DESC)')
            self.connection.execute('PRAGMA user_version=1')
            self.connection.commit()

    def list(self, kind):
        with self._lock:
            rows = self.connection.execute('SELECT data FROM documents WHERE kind=? ORDER BY created_at DESC', (kind,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get(self, kind, id):
        with self._lock:
            row = self.connection.execute('SELECT data FROM documents WHERE kind=? AND id=?', (kind, str(id))).fetchone()
            return json.loads(row[0]) if row else None

    def _put(self, kind, doc):
        doc = dict(doc)
        doc.setdefault('id', uid())
        doc.setdefault('created_at', now())
        doc['updated_at'] = now()
        self.connection.execute('INSERT INTO documents VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at',
                                (kind, doc['id'], json.dumps(doc, ensure_ascii=False), doc['created_at'], doc['updated_at']))
        return doc

    def put(self, kind, doc):
        with self._lock, self.connection:
            return self._put(kind, doc)

    def mutate(self, kind, id, fn):
        with self._lock, self.connection:
            doc = self.get(kind, id)
            if doc is None:
                return None
            changed = fn(doc)
            return self._put(kind, changed) if changed is not None else doc

    def update(self, kind, id, changes):
        return self.mutate(kind, id, lambda doc: {**doc, **changes, 'id': doc['id']})

    def delete(self, kind, id):
        with self._lock, self.connection:
            return self.connection.execute('DELETE FROM documents WHERE kind=? AND id=?', (kind, id)).rowcount > 0

    def event(self, task_id, message, level='info', **metadata):
        return self.put('event', {'id': uid(), 'task_id': task_id, 'message': message, 'level': level, **metadata})

    def setting(self, key, default=None):
        row = self.get('setting', key)
        return row['value'] if row else default

    def set_setting(self, key, value):
        return self.put('setting', {'id': key, 'value': value})

    def backup(self, path):
        with self._lock:
            destination = sqlite3.connect(str(path))
            try:
                self.connection.backup(destination)
            finally:
                destination.close()


db = Database()
