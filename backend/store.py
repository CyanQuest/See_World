import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from cryptography.fernet import Fernet
from .models import Settings

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('TRAVEL_DATA_DIR', ROOT / 'data'))
SECRETS = Path(os.environ.get('TRAVEL_SECRET_DIR', ROOT / '.secrets'))

def now(): return time.time()
def uid(): return uuid.uuid4().hex
def dumps(value): return json.dumps(value, ensure_ascii=False)

@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DATA / 'travel.sqlite3', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally: conn.close()

def init():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    SECRETS.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, idem TEXT UNIQUE, status TEXT, stage TEXT, prefs TEXT, limits TEXT, error TEXT DEFAULT '', created REAL, updated REAL, lease REAL DEFAULT 0, owner TEXT DEFAULT '', action TEXT DEFAULT 'new');
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, canonical TEXT, meta TEXT, selected INTEGER DEFAULT 0, status TEXT DEFAULT 'candidate', reason TEXT DEFAULT '', error TEXT DEFAULT '', complete INTEGER DEFAULT 0, transcript TEXT, facts TEXT, UNIQUE(task_id,canonical));
        CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id) ON DELETE CASCADE, start REAL, end REAL, hash TEXT, status TEXT, text TEXT, raw TEXT, error TEXT DEFAULT '', path TEXT);
        CREATE TABLE IF NOT EXISTS guides(task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, version INTEGER, content TEXT, markdown TEXT, prefs TEXT, model TEXT, prompt_version TEXT, created REAL, PRIMARY KEY(task_id,version));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, source_id TEXT, stage TEXT, level TEXT, message TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, provider TEXT, operation TEXT, status TEXT, seconds REAL DEFAULT 0, tokens INTEGER, reserved_tokens INTEGER DEFAULT 0, cost REAL, request_id TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT);
        CREATE TABLE IF NOT EXISTS provider_cache(task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, cache_key TEXT, response TEXT, PRIMARY KEY(task_id,cache_key));
        CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, task_id TEXT, kind TEXT);
        CREATE TABLE IF NOT EXISTS task_retries(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, attempts INTEGER, next_at REAL);
        CREATE TABLE IF NOT EXISTS quality_cycles(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, phase TEXT, version INTEGER DEFAULT 0, feedback TEXT DEFAULT '{}', batch TEXT DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS source_attempts(source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE, task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS search_cache(cache_key TEXT PRIMARY KEY, urls TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS search_pacing(scope TEXT PRIMARY KEY, next_at REAL);
        CREATE TABLE IF NOT EXISTS editorial_steps(task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, step_key TEXT, value TEXT, PRIMARY KEY(task_id,step_key));
        CREATE TABLE IF NOT EXISTS request_metrics(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, value TEXT);
        CREATE TABLE IF NOT EXISTS evidence_searches(task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, search_key TEXT, value TEXT, PRIMARY KEY(task_id,search_key));
        CREATE TABLE IF NOT EXISTS revision_issues(task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, version INTEGER, issue_id TEXT, value TEXT, PRIMARY KEY(task_id,version,issue_id));
        PRAGMA user_version=3;
        ''')
        columns={r['name'] for r in c.execute('PRAGMA table_info(quality_cycles)')}
        for name, definition in [('revision_round','INTEGER DEFAULT 0'),('triage',"TEXT DEFAULT '{}'"),('history',"TEXT DEFAULT '[]'"),('baseline_score','INTEGER DEFAULT -1')]:
            if name not in columns: c.execute(f'ALTER TABLE quality_cycles ADD COLUMN {name} {definition}')
    os.chmod(DATA / 'travel.sqlite3', 0o600)

def cipher():
    path = SECRETS / 'master.key'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError: pass
    else:
        with os.fdopen(fd, 'wb') as f: f.write(Fernet.generate_key())
    return Fernet(path.read_bytes())

def settings(private=True):
    with db() as c: row = c.execute('SELECT value FROM settings WHERE id=1').fetchone()
    value = Settings().model_dump()
    if row:
        value.update(json.loads(row['value']))
        for key in ('moss_key', 'llm_key', 'cookies'):
            if value[key]: value[key] = cipher().decrypt(value[key].encode()).decode()
    value['moss_key'] = os.environ.get('MOSS_API_KEY', value['moss_key'])
    value['llm_key'] = os.environ.get('LLM_API_KEY', value['llm_key'])
    value['llm_base'] = os.environ.get('LLM_BASE_URL', value['llm_base'])
    value['llm_model'] = os.environ.get('LLM_MODEL', value['llm_model'])
    if not private:
        for key in ('moss_key', 'llm_key', 'cookies'):
            value[key + '_configured'] = bool(value.pop(key))
    return value

def save_settings(patch):
    value = settings()
    for key, val in patch.items():
        if key in ('moss_key', 'llm_key', 'cookies') and val == '': continue
        value[key] = val
    value = Settings(**value).model_dump()
    for key in ('moss_key', 'llm_key', 'cookies'):
        if value[key]: value[key] = cipher().encrypt(value[key].encode()).decode()
    with db() as c: c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)', (dumps(value),))

def event(task, stage, message, source=None, level='info'):
    with db() as c:
        c.execute('INSERT INTO events(task_id,source_id,stage,level,message,created) VALUES(?,?,?,?,?,?)', (task, source, stage, level, message, now()))
        c.execute('UPDATE tasks SET stage=?,updated=? WHERE id=?', (stage, now(), task))

def task(task_id):
    with db() as c: row = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row: raise KeyError('任务不存在')
    result = dict(row)
    for k in ('prefs', 'limits'): result[k] = json.loads(result[k])
    return result

def sources(task_id):
    with db() as c: rows = c.execute('SELECT * FROM sources WHERE task_id=? ORDER BY rowid', (task_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for k in ('meta', 'transcript', 'facts'): item[k] = json.loads(item[k]) if item[k] else None
        result.append(item)
    return result

def update_source(source_id, **values):
    for key in ('meta', 'transcript', 'facts'):
        if key in values: values[key] = dumps(values[key])
    with db() as c: c.execute('UPDATE sources SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), source_id))

def set_status(task_id, status, error=''):
    with db() as c: c.execute('UPDATE tasks SET status=?,error=?,updated=? WHERE id=?', (status, error, now(), task_id))
