"""SQLite-хранилище трекера. Один connection на процесс, доступ под общим замком."""
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
(DATA / "reports").mkdir(exist_ok=True)
(DATA / "runs").mkdir(exist_ok=True)
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(exist_ok=True)

_lock = threading.RLock()
_conn = sqlite3.connect(DATA / "tracker.db", check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, color TEXT DEFAULT '#6c8cff',
    active INTEGER DEFAULT 1, chat_session_id TEXT, created_at REAL  -- chat_session_id устарел: сессии в chats
);
CREATE TABLE IF NOT EXISTS columns (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL, kind TEXT, position INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    column_id INTEGER NOT NULL REFERENCES columns(id), title TEXT NOT NULL, description TEXT DEFAULT '',
    position REAL DEFAULT 0, status TEXT DEFAULT 'idle', session_id TEXT, model TEXT, effort TEXT,
    summary TEXT, report_path TEXT, error TEXT, cost_usd REAL DEFAULT 0, last_run_at REAL, pending_prompt TEXT,
    created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts REAL, kind TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT DEFAULT 'question', text TEXT, options TEXT, payload TEXT, answer TEXT,
    status TEXT DEFAULT 'open', created_at REAL, answered_at REAL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT, session_id TEXT, created_at REAL, updated_at REAL,
    title_kind TEXT  -- NULL временное (из первого сообщения) | auto от модели | manual | legacy (до автоназваний)
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT, text TEXT, ts REAL, attachments TEXT, chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE
);
"""

DEFAULT_SETTINGS = {
    "pipeline_enabled": "0",
    "stop_mode": "graceful",          # graceful | immediate
    "max_parallel": "2",
    "permission_mode": "bypassPermissions",
    "model": "",
    "effort": "",
    "chat_model": "",
    "extra_prompt": "",
    "pause_until": "0",
    "update_check_minutes": "60",     # проверка обновлений на GitHub, 0 — только вручную
    "git_mode": "commit_push",        # off | commit | commit_push — что агент делает с git в конце задачи
}

# Системные колонки: kind -> имя по умолчанию
SYSTEM_COLUMNS = [
    ("backlog", "Бэклог"),
    ("todo", "В работу"),
    ("progress", "В процессе"),
    ("waiting", "Ждёт ответа"),
    ("done", "Готово"),
]

with _lock:
    _conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    # миграции старых баз
    if "attachments" not in {r["name"] for r in _conn.execute("PRAGMA table_info(chat_messages)")}:
        _conn.execute("ALTER TABLE chat_messages ADD COLUMN attachments TEXT")
    if "chat_id" not in {r["name"] for r in _conn.execute("PRAGMA table_info(chat_messages)")}:
        _conn.execute("ALTER TABLE chat_messages ADD COLUMN chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE")
    if "title_kind" not in {r["name"] for r in _conn.execute("PRAGMA table_info(chats)")}:
        _conn.execute("ALTER TABLE chats ADD COLUMN title_kind TEXT")
        _conn.execute("UPDATE chats SET title_kind='legacy' WHERE title IS NOT NULL")  # разберёт pipeline.Titler
    _conn.execute("CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages(chat_id, id)")


def _migrate_chats():
    """Один чат на проект (projects.chat_session_id + сообщения без chat_id) -> первый чат в списке chats."""
    moved = []
    with _lock:
        rows = _conn.execute("""SELECT p.id, p.chat_session_id, p.created_at, MIN(m.ts) AS first, MAX(m.ts) AS last
                                FROM projects p LEFT JOIN chat_messages m ON m.project_id=p.id AND m.chat_id IS NULL
                                GROUP BY p.id HAVING p.chat_session_id IS NOT NULL OR COUNT(m.id) > 0""").fetchall()
        if not rows:
            return
        _conn.execute("BEGIN")
        try:
            for r in rows:
                first = r["first"] or r["created_at"] or time.time()
                cid = _conn.execute("INSERT INTO chats(project_id, title, session_id, created_at, updated_at) VALUES(?,?,?,?,?)",
                                    (r["id"], f"Чат от {datetime.fromtimestamp(first):%d.%m.%Y}", r["chat_session_id"],
                                     first, r["last"] or first)).lastrowid
                _conn.execute("UPDATE chat_messages SET chat_id=? WHERE project_id=? AND chat_id IS NULL", (cid, r["id"]))
                _conn.execute("UPDATE projects SET chat_session_id=NULL WHERE id=?", (r["id"],))
                moved.append((r["id"], cid))
        except BaseException:
            _conn.execute("ROLLBACK")
            raise
        _conn.execute("COMMIT")
    for pid, cid in moved:  # вложения: data/uploads/<pid>/<файл> -> data/uploads/<pid>/<cid>/<файл>
        files = [f for f in (UPLOADS / str(pid)).glob("*") if f.is_file()]
        if files:
            (UPLOADS / str(pid) / str(cid)).mkdir(parents=True, exist_ok=True)
        for f in files:
            f.replace(UPLOADS / str(pid) / str(cid) / f.name)


_migrate_chats()


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid


def now():
    return time.time()


# ---------- settings ----------
def get_settings():
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


def get_setting(key):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else DEFAULT_SETTINGS.get(key)


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


# ---------- projects / columns ----------
def create_project(name, path, color="#6c8cff"):
    with _lock:
        pid = ex("INSERT INTO projects(name, path, color, created_at) VALUES(?,?,?,?)", (name, path, color, now()))
        for i, (kind, cname) in enumerate(SYSTEM_COLUMNS):
            ex("INSERT INTO columns(project_id, name, kind, position) VALUES(?,?,?,?)", (pid, cname, kind, i))
        return pid


def column_by_kind(project_id, kind):
    return one("SELECT * FROM columns WHERE project_id=? AND kind=?", (project_id, kind))


def find_column(project_id, name_or_kind):
    """Колонка по kind или по имени (без учёта регистра)."""
    c = column_by_kind(project_id, name_or_kind)
    if c:
        return c
    for c in q("SELECT * FROM columns WHERE project_id=?", (project_id,)):
        if c["name"].strip().lower() == str(name_or_kind).strip().lower():
            return c
    return None


# ---------- tasks ----------
def get_task(tid):
    return one("""SELECT t.*, c.kind AS column_kind, c.name AS column_name, p.name AS project_name, p.path AS project_path
                  FROM tasks t JOIN columns c ON c.id=t.column_id JOIN projects p ON p.id=t.project_id
                  WHERE t.id=?""", (tid,))


def create_task(project_id, title, description="", column_id=None, model=None, effort=None):
    if column_id is None:
        column_id = column_by_kind(project_id, "backlog")["id"]
    pos = one("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM tasks WHERE column_id=?", (column_id,))["p"]
    t = now()
    return ex("""INSERT INTO tasks(project_id, column_id, title, description, position, model, effort, created_at, updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?)""", (project_id, column_id, title, description, pos, model or None,
                                                effort or None, t, t))


def update_task(tid, **fields):
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))


def move_task(tid, kind=None, column_id=None, **fields):
    t = get_task(tid)
    if column_id is None:
        column_id = column_by_kind(t["project_id"], kind)["id"]
    pos = one("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM tasks WHERE column_id=?", (column_id,))["p"]
    update_task(tid, column_id=column_id, position=pos, **fields)


def add_event(task_id, kind, text):
    return ex("INSERT INTO events(task_id, ts, kind, text) VALUES(?,?,?,?)", (task_id, now(), kind, text))


def task_events(task_id, after=0, limit=500):
    return q("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id DESC LIMIT ?", (task_id, after, limit))[::-1]


# ---------- questions ----------
def add_question(task_id, text, options=None, kind="question", payload=None):
    return ex("INSERT INTO questions(task_id, kind, text, options, payload, created_at) VALUES(?,?,?,?,?,?)",
              (task_id, kind, text, json.dumps(options or [], ensure_ascii=False),
               json.dumps(payload, ensure_ascii=False) if payload is not None else None, now()))


def get_question(qid):
    r = one("SELECT * FROM questions WHERE id=?", (qid,))
    if r:
        r["options"] = json.loads(r["options"] or "[]")
        r["payload"] = json.loads(r["payload"]) if r["payload"] else None
    return r


def list_questions(status="open"):
    rows = q("""SELECT qu.*, t.title AS task_title, t.project_id, p.name AS project_name, p.color AS project_color
                FROM questions qu JOIN tasks t ON t.id=qu.task_id JOIN projects p ON p.id=t.project_id
                WHERE (?='all' OR qu.status=?) ORDER BY qu.id DESC LIMIT 200""", (status, status))
    for r in rows:
        r["options"] = json.loads(r["options"] or "[]")
        r["payload"] = json.loads(r["payload"]) if r["payload"] else None
    return rows


# ---------- chat ----------
def chat_dir(project_id, chat_id):
    """Папка вложений чата."""
    return UPLOADS / str(project_id) / str(chat_id)


def get_chat(chat_id):
    return one("SELECT * FROM chats WHERE id=?", (chat_id,))


def list_chats(project_id):
    return q("""SELECT c.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id=c.id) AS messages
                FROM chats c WHERE c.project_id=? ORDER BY c.updated_at DESC, c.id DESC""", (project_id,))


def create_chat(project_id, title=None):
    t = now()
    return ex("INSERT INTO chats(project_id, title, created_at, updated_at) VALUES(?,?,?,?)", (project_id, title, t, t))


def add_chat(chat_id, role, text, attachments=None):
    """attachments: [{name, file, size, type}], file — имя в data/uploads/<project_id>/<chat_id>/.
    None — чата уже нет (удалили, пока шёл ответ)."""
    with _lock:
        c = get_chat(chat_id)
        if not c:
            return None
        t = now()
        ex("UPDATE chats SET updated_at=? WHERE id=?", (t, chat_id))
        return ex("INSERT INTO chat_messages(project_id, chat_id, role, text, ts, attachments) VALUES(?,?,?,?,?,?)",
                  (c["project_id"], chat_id, role, text, t,
                   json.dumps(attachments, ensure_ascii=False) if attachments else None))


def chat_messages(chat_id, limit=300):
    rows = q("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))[::-1]
    for r in rows:
        r["attachments"] = json.loads(r["attachments"]) if r["attachments"] else []
    return rows
