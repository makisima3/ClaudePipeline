"""Общая история завершений и неизменяемые снимки, независимо от транспорта Telegram."""
import json
import mimetypes
import shutil
import uuid
from pathlib import Path

import db

ROOT = db.DATA / "results"


def installation_id():
    with db._lock:
        db.ex("INSERT OR IGNORE INTO result_meta VALUES('installation_id',?)", (uuid.uuid4().hex,))
        return db.one("SELECT value FROM result_meta WHERE key='installation_id'")["value"]


def run_key(t):
    return str(t["last_run_at"] or t["created_at"])


def existing(t):
    return db.one("SELECT * FROM task_results WHERE source_task_id=? AND run_key=?", (t["id"], run_key(t)))


def prepare(t, summary, report, text=None, files=None):
    """Сначала копируем файлы; запись публикуется вместе с done в транзакции finish."""
    saved = existing(t)
    if saved:
        return saved
    folder = ROOT / uuid.uuid4().hex
    materials, error = [], None
    try:
        if not isinstance(files or [], list) or len(files or []) > 10:
            raise ValueError()
        total = 0
        for i, name in enumerate(files or []):
            source = Path(name)
            if not source.is_absolute():
                source = Path(t["project_path"]) / source
            size = source.stat().st_size
            total += size
            if not source.is_file() or size > 100 * 1024**2 or total > 300 * 1024**2:
                raise ValueError()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{i + 1}-{source.name}"
            shutil.copyfile(source, target)
            materials.append({"name": source.name, "size": size,
                              "type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                              "path": target.relative_to(ROOT).as_posix()})
    except (OSError, ValueError, TypeError):
        # Даже недоступный материал не должен уничтожить итог и уже скопированные файлы.
        error = "Часть материалов не сохранена: исходный файл недоступен или превышен лимит."
    return {"project_id": t["project_id"], "task_id": t["id"], "source_task_id": t["id"],
            "task_number": t["task_number"], "project_name": t["project_name"], "title": t["title"],
            "run_key": run_key(t), "summary": summary, "text": (text or summary).strip(), "report": report,
            "files": json.dumps(materials, ensure_ascii=False), "error": error, "created_at": db.now(),
            "description": t.get("description", ""), "status": "done"}


def save(row):
    if "id" in row:
        return row["id"]
    keys = list(row)
    db.ex(f"INSERT OR IGNORE INTO task_results({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
          tuple(row[k] for k in keys))
    return db.one("SELECT id FROM task_results WHERE source_task_id=? AND run_key=?",
                  (row["source_task_id"], row["run_key"]))["id"]


def public(row, detail=False):
    out = {k: row[k] for k in ("id", "project_id", "task_id", "source_task_id", "run_key", "task_number", "project_name", "title",
                               "summary", "text", "error", "created_at")}
    out["files"] = [{k: v for k, v in f.items() if k != "path"} |
                    {"url": f"/api/results/{row['id']}/files/{i}"} for i, f in enumerate(json.loads(row["files"]))]
    out["url"] = f"/?result_id={row['id']}"
    if detail:
        out["report"] = row["report"]
    return out


def recover():
    """Однократный перенос доступных старых итогов. Новые записи создаёт только finish."""
    rows = db.q("""SELECT t.id FROM tasks t WHERE t.status='done' AND t.summary IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM task_results r WHERE r.source_task_id=t.id) ORDER BY t.updated_at,t.id""")
    for item in rows:
        t = db.get_task(item["id"])
        text, files, report = t["summary"], [], t["summary"]
        if t["report_path"]:
            try:
                report = Path(t["report_path"]).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                pass
        delivery = db.one("SELECT payload FROM telegram_deliveries WHERE task_id=? AND run_key=? AND state<>'prepared'",
                          (t["id"], run_key(t)))
        if delivery:
            import telegram
            parts = json.loads(delivery["payload"])
            if parts and not parts[0].get("error"):
                text = parts[0]["text"].rsplit("\n\n[AP:", 1)[0]
                header = f"{t['project_name']} #{t['task_number']} · {t['title']}\n\n"
                if text.startswith(header):
                    text = text[len(header):]
                files = [str(telegram.ROOT / f) for part in parts for f in part.get("files", [])]
        row = prepare(t, t["summary"], report, text, files)
        row["created_at"] = t["updated_at"] or t["created_at"] or db.now()
        save(row)


def page(project_id=None, after=0, before=0, limit=50):
    where, args = ["id> ?"], [after]
    if project_id is not None:
        where.append("project_id=?")
        args.append(project_id)
    if before:
        where.append("id<?")
        args.append(before)
    # Android читает строго по возрастанию курсора; история проекта — от новых к старым.
    direction = "ASC" if project_id is None else "DESC"
    rows = db.q(f"SELECT * FROM task_results WHERE {' AND '.join(where)} ORDER BY id {direction} LIMIT ?", (*args, limit + 1))
    return {"installation_id": installation_id(), "items": [public(r) for r in rows[:limit]],
            "has_more": len(rows) > limit, "latest_id": db.one("SELECT COALESCE(MAX(id),0) AS n FROM task_results")["n"]}
