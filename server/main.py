"""AgentPipeline: локальный сервер трекера. Запуск: python server/main.py"""
import base64
import binascii
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

import runtime
if __name__ == "__main__":
    runtime.acquire()

import db
import pipeline as pl
from runner import PORT, NO_WINDOW
from updater import updater
from git_readiness import check as check_git_readiness
from telegram import telegram, delivery as telegram_delivery, ROOT as TELEGRAM_ROOT

WEB = db.ROOT / "web"
ROUTES = []
MAX_FILE = 20 * 1024 * 1024        # на одно вложение
MAX_FILES = 10                     # вложений в сообщении
MAX_TOTAL = 100 * 1024 * 1024      # всех вложений сообщения
MAX_BODY = MAX_TOTAL * 4 // 3 + 1024 * 1024  # base64 + текст


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def need(cond, msg="Не найдено", code=404):
    if not cond:
        raise HttpError(code, msg)


# ---------------- состояние ----------------
@route("GET", "/api/state")
def api_state(h, body, query):
    return {
        "boot_id": pl.BOOT_ID,
        "agent": runtime.AGENT,
        "agent_name": runtime.NAME,
        "agent_info": pl.agent_info(),
        "settings": db.get_settings(),
        "pipeline": pl.orch.state(),
        "update": updater.state(),
        "telegram": telegram.state(),
        "projects": db.q("SELECT * FROM projects ORDER BY id"),
        "open_questions": db.one("SELECT COUNT(*) AS n FROM questions WHERE status='open'")["n"],
    }


@route("POST", "/api/pipeline")
def api_pipeline(h, body, query):
    pl.orch.set_enabled(bool(body.get("enabled")))
    return pl.orch.state()


@route("POST", "/api/restart")
def api_restart(h, body, query):
    pl.restart.request("кнопка в настройках")
    return pl.orch.state()


@route("GET", "/api/update")
def api_update_state(h, body, query):
    return updater.state()


@route("POST", "/api/update/check")
def api_update_check(h, body, query):
    return updater.check()


@route("POST", "/api/update")
def api_update(h, body, query):
    ok, msg = updater.update()
    need(ok, msg, 409)
    return {"message": msg, "update": updater.state(), "pipeline": pl.orch.state()}


@route("POST", "/api/settings")
def api_settings(h, body, query):
    if "onboarding_status" in body:
        need(body["onboarding_status"] in ("completed", "skipped"), "Мастер можно завершить или пропустить", 400)
    for k, v in body.items():
        if k in db.DEFAULT_SETTINGS and k not in ("pipeline_enabled", "pause_until", "telegram_auth", "telegram_error", "telegram_test_at"):
            db.set_setting(k, v)
    if body.get("clear_pause"):
        db.set_setting("pause_until", "0")
    if "update_check_minutes" in body:
        updater.wake.set()
    pl.hub.send("state", state=pl.orch.state())
    return db.get_settings()


# ---------------- проекты и колонки ----------------
@route("GET", r"/api/projects/(\d+)/git-readiness")
def api_git_readiness(h, body, query, pid):
    p = db.one("SELECT path FROM projects WHERE id=?", (pid,))
    need(p)
    mode = query.get("mode", [db.get_setting("git_mode")])[0]
    auth = query.get("auth", ["existing"])[0]
    need(mode in pl.GIT_MODES and auth in ("existing", "gh"), "Неизвестный режим проверки Git", 400)
    return check_git_readiness(p["path"], mode, auth)


@route("POST", "/api/projects")
def api_project_create(h, body, query):
    name, path = (body.get("name") or "").strip(), (body.get("path") or "").strip().strip('"')
    need(name and path, "Нужны название и папка", 400)
    need(Path(path).is_dir(), f"Папка не найдена: {path}", 400)
    pid = db.create_project(name, path, body.get("color") or "#6c8cff")
    pl.changed()
    return db.one("SELECT * FROM projects WHERE id=?", (pid,))


@route("PATCH", r"/api/projects/(\d+)")
def api_project_update(h, body, query, pid):
    fields = {k: body[k] for k in ("name", "path", "color", "active") if k in body}
    if "path" in fields:
        need(Path(fields["path"]).is_dir(), f"Папка не найдена: {fields['path']}", 400)
    if fields:
        db.ex(f"UPDATE projects SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), pid))
    pl.changed()
    return db.one("SELECT * FROM projects WHERE id=?", (pid,))


@route("DELETE", r"/api/projects/(\d+)")
def api_project_delete(h, body, query, pid):
    for t in db.q("SELECT id FROM tasks WHERE project_id=?", (pid,)):
        pl.orch.stop_task(t["id"])
    pl.chat.stop_project(pid)
    db.ex("DELETE FROM projects WHERE id=?", (pid,))
    pl.drop_uploads(pid)
    pl.changed()
    return {"ok": True}


@route("GET", r"/api/projects/(\d+)/board")
def api_board(h, body, query, pid):
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    need(p)
    tasks = db.q("""SELECT t.*, (SELECT COUNT(*) FROM questions qu WHERE qu.task_id=t.id AND qu.status='open') AS open_q,
                           (SELECT COUNT(*) FROM task_links l WHERE l.task_id=t.id OR l.linked_id=t.id) AS links
                    FROM tasks t WHERE project_id=? ORDER BY position""", (pid,))
    return {"project": p, "columns": db.q("SELECT * FROM columns WHERE project_id=? ORDER BY position", (pid,)),
            "tasks": [db.agent_row(t) for t in tasks]}


@route("POST", r"/api/projects/(\d+)/columns")
def api_column_create(h, body, query, pid):
    name = (body.get("name") or "").strip()
    need(name, "Нужно имя", 400)
    pos = db.one("SELECT COALESCE(MAX(position),0)+1 AS p FROM columns WHERE project_id=?", (pid,))["p"]
    db.ex("INSERT INTO columns(project_id, name, position) VALUES(?,?,?)", (pid, name, pos))
    pl.changed(pid)
    return {"ok": True}


@route("PATCH", r"/api/columns/(\d+)")
def api_column_update(h, body, query, cid):
    c = db.one("SELECT * FROM columns WHERE id=?", (cid,))
    need(c)
    if body.get("name"):
        db.ex("UPDATE columns SET name=? WHERE id=?", (body["name"].strip(), cid))
    if body.get("order"):  # полный порядок колонок проекта
        for i, col_id in enumerate(body["order"]):
            db.ex("UPDATE columns SET position=? WHERE id=? AND project_id=?", (i, col_id, c["project_id"]))
    pl.changed(c["project_id"])
    return {"ok": True}


@route("DELETE", r"/api/columns/(\d+)")
def api_column_delete(h, body, query, cid):
    c = db.one("SELECT * FROM columns WHERE id=?", (cid,))
    need(c)
    need(not c["kind"], "Системную колонку удалить нельзя", 400)
    backlog = db.column_by_kind(c["project_id"], "backlog")
    db.ex("UPDATE tasks SET column_id=? WHERE column_id=?", (backlog["id"], cid))
    db.ex("DELETE FROM columns WHERE id=?", (cid,))
    pl.changed(c["project_id"])
    return {"ok": True}


# ---------------- задачи ----------------
@route("POST", r"/api/projects/(\d+)/tasks")
def api_task_create(h, body, query, pid):
    title = (body.get("title") or "").strip()
    need(title, "Нужен заголовок", 400)
    cid = int(body.get("column_id") or db.column_by_kind(pid, "backlog")["id"])
    notify = body.get("telegram_notify", False)
    need(isinstance(notify, bool), "telegram_notify должен быть логическим значением", 400)
    tid = db.create_task(pid, title, body.get("description", ""), cid, body.get("model"), body.get("effort"), notify)
    kind = db.one("SELECT kind FROM columns WHERE id=?", (cid,))["kind"]
    if kind == "todo":
        db.update_task(tid, status="queued")
    pl.changed(pid)
    return db.get_task(tid)


@route("GET", r"/api/tasks/(\d+)")
def api_task(h, body, query, tid):
    t = db.get_task(tid)
    need(t)
    t["events"] = db.task_events(tid)
    t["questions"] = [db.get_question(r["id"]) for r in db.q("SELECT id FROM questions WHERE task_id=? ORDER BY id", (tid,))]
    t["running"] = tid in pl.orch.runs
    t["telegram_delivery"] = telegram_delivery(tid)
    transcript = pl.transcript_path(t["project_path"], t["session_id"])
    t["transcript"] = str(transcript) if transcript else None
    t["links"] = [{k: l[k] for k in ("id", "task_id", "task_number", "title", "status", "column_name", "column_kind", "project_id", "project_name")}
                  for l in db.task_links(tid)]
    return t


@route("GET", "/api/tasks")
def api_task_search(h, body, query):
    """Локальный номер или заголовок; project_id ограничивает поиск одним проектом."""
    s = (query.get("q") or [""])[0].strip().lstrip("#").casefold()
    project_id = (query.get("project_id") or [""])[0]
    need(not project_id or project_id.isdigit(), "project_id должен быть числом", 400)
    skip = {int(x) for x in (query.get("exclude") or [""])[0].split(",") if x.isdigit()}
    if not s:
        return []
    rows = db.q("""SELECT t.id, t.id AS task_id, t.task_number, t.title, t.status, t.project_id, c.name AS column_name, p.name AS project_name
                   FROM tasks t JOIN columns c ON c.id=t.column_id JOIN projects p ON p.id=t.project_id
                   WHERE (?='' OR t.project_id=?) ORDER BY t.id DESC""", (project_id, project_id))
    hit = [r for r in rows if r["id"] not in skip and
           (str(r["task_number"]) == s if s.isdigit() else s in r["title"].casefold())]
    return hit[:12]


@route("POST", r"/api/tasks/(\d+)/links")
def api_task_link(h, body, query, tid):
    t = db.get_task(tid)
    need(t)
    other = db.get_task(int(body.get("task_id") or 0))
    need(other, "Нет такой задачи", 400)
    try:
        db.link_tasks(tid, other["id"])
    except ValueError as e:
        raise HttpError(400, str(e))
    pl.changed_links(t["project_id"], [other["id"]])
    return {"ok": True}


@route("DELETE", r"/api/tasks/(\d+)/links/(\d+)")
def api_task_unlink(h, body, query, tid, other):
    t = db.get_task(tid)
    need(t)
    db.unlink_tasks(tid, other)
    pl.changed_links(t["project_id"], [other])
    return {"ok": True}


@route("PATCH", r"/api/tasks/(\d+)")
def api_task_update(h, body, query, tid):
    t = db.get_task(tid)
    need(t)
    fields = {k: (body[k] or None) if k in ("model", "effort") else body[k]
              for k in ("title", "description", "model", "effort", "telegram_notify") if k in body}
    if "telegram_notify" in fields:
        need(isinstance(fields["telegram_notify"], bool), "telegram_notify должен быть логическим значением", 400)
    db.update_task(tid, **fields)
    updated = db.get_task(tid)
    if updated["telegram_notify"] and updated["status"] == "done" and updated["summary"] and not telegram_delivery(tid):
        pl.prepare_telegram(updated, updated["summary"])
    pl.changed(t["project_id"])
    return db.get_task(tid)


@route("POST", r"/api/tasks/(\d+)/move")
def api_task_move(h, body, query, tid):
    t = db.get_task(tid)
    need(t)
    cid = int(body["column_id"])
    col = db.one("SELECT * FROM columns WHERE id=?", (cid,))
    need(col and col["project_id"] == t["project_id"], "Колонка из другого проекта", 400)
    if tid in pl.orch.runs and cid != t["column_id"]:
        raise HttpError(409, "Задача выполняется — сначала останови её")
    db.update_task(tid, column_id=cid)
    for i, other in enumerate(body.get("order") or [tid]):
        db.ex("UPDATE tasks SET position=? WHERE id=? AND column_id=?", (i, other, cid))
    if cid != t["column_id"]:
        pl.on_manual_move(tid, col["kind"])
    pl.changed(t["project_id"])
    return {"ok": True}


@route("POST", r"/api/tasks/(\d+)/comment")
def api_task_comment(h, body, query, tid):
    t = db.get_task(tid)
    need(t)
    text = (body.get("text") or "").strip()
    need(text, "Пустой комментарий", 400)
    pl.task_event(tid, "comment", text)
    return {"ok": True}


@route("POST", r"/api/tasks/(\d+)/stop")
def api_task_stop(h, body, query, tid):
    pl.orch.stop_task(tid)
    return {"ok": True}


@route("POST", r"/api/tasks/(\d+)/reset")
def api_task_reset(h, body, query, tid):
    """Забыть сессию: следующий запуск начнётся с чистого листа."""
    t = db.get_task(tid)
    need(t)
    need(tid not in pl.orch.runs, "Задача выполняется", 409)
    db.update_task(tid, session_id=None, pending_prompt=None, error=None)
    pl.task_event(tid, "system", "Сессия сброшена, следующий запуск — с нуля")
    pl.changed(t["project_id"])
    return {"ok": True}


@route("DELETE", r"/api/tasks/(\d+)")
def api_task_delete(h, body, query, tid):
    t = db.get_task(tid)
    need(t)
    pl.orch.stop_task(tid)
    linked = [l["id"] for l in db.task_links(tid)]
    db.ex("DELETE FROM tasks WHERE id=?", (tid,))  # связи удалит ON DELETE CASCADE
    pl.changed_links(t["project_id"], linked)
    pl.hub.send("questions")
    return {"ok": True}


@route("GET", r"/api/tasks/(\d+)/report")
def api_task_report(h, body, query, tid):
    t = db.get_task(tid)
    need(t and t["report_path"] and Path(t["report_path"]).exists(), "Отчёта пока нет")
    return {**{k: t[k] for k in ("task_id", "task_number", "project_id", "project_name", "title")},
            "markdown": Path(t["report_path"]).read_text(encoding="utf-8")}


@route("GET", r"/api/tasks/(\d+)/rawlog")
def api_task_rawlog(h, body, query, tid):
    p = db.DATA / "runs" / f"task-{tid}.jsonl"
    need(p.exists(), "Лога нет")
    return h.send_text(p.read_text(encoding="utf-8"), "text/plain; charset=utf-8")


# ---------------- вопросы ----------------
@route("GET", "/api/questions")
def api_questions(h, body, query):
    return db.list_questions(query.get("status", ["open"])[0])


@route("POST", r"/api/questions/(\d+)/answer")
def api_answer(h, body, query, qid):
    answer = (body.get("answer") or "").strip()
    need(answer, "Пустой ответ", 400)
    need(pl.answer_question(qid, answer), "Вопрос уже закрыт", 409)
    return {"ok": True}


# ---------------- чат ----------------
def get_chat(cid):
    c = db.get_chat(cid)
    need(c, "Чат не найден")
    return c


@route("GET", r"/api/projects/(\d+)/chats")
def api_chats(h, body, query, pid):
    return {"chats": [pl.chat.info(c) for c in db.list_chats(pid)]}


@route("POST", r"/api/projects/(\d+)/chats")
def api_chat_create(h, body, query, pid):
    need(db.one("SELECT id FROM projects WHERE id=?", (pid,)), "Проект не найден")
    return pl.chat.create(pid)


@route("PATCH", r"/api/chats/(\d+)")
def api_chat_rename(h, body, query, cid):
    get_chat(cid)
    title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:200]
    need(title, "Пустое название", 400)
    pl.chat.rename(cid, title)
    return pl.chat.info(db.get_chat(cid))


@route("POST", r"/api/chats/(\d+)/title")
def api_chat_retitle(h, body, query, cid):
    get_chat(cid)
    need(pl.chat.retitle(cid), "Агент в этом чате ещё не ответил — называть пока нечего", 400)
    return pl.chat.info(db.get_chat(cid))


@route("DELETE", r"/api/chats/(\d+)")
def api_chat_delete(h, body, query, cid):
    get_chat(cid)
    pl.chat.delete(cid)
    return {"ok": True}


@route("GET", r"/api/chats/(\d+)/messages")
def api_chat_messages(h, body, query, cid):
    return {"chat": pl.chat.info(get_chat(cid)), "messages": db.chat_messages(cid), "running": pl.chat.running(cid)}


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name):
    """Имя файла без путей и запрещённых в Windows символов."""
    name = re.split(r"[\\/]", str(name or ""))[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem, ext = stem[:80], ext[:15]
    if stem.split(".")[0].upper() in WIN_RESERVED:
        stem = "_" + stem
    return (f"{stem}.{ext}" if ext else stem) or "file"


def mb(n):
    return f"{n / 1024 / 1024:.0f} МБ"


def save_uploads(folder, items):
    """Проверить и сохранить вложения: сначала всё декодируем, потом пишем — без полусохранённых сообщений."""
    need(isinstance(items, list), "Вложения должны быть списком", 400)
    need(len(items) <= MAX_FILES, f"Не больше {MAX_FILES} вложений в одном сообщении", 400)
    decoded, total = [], 0
    for it in items:
        need(isinstance(it, dict), "Неверный формат вложения", 400)
        name = safe_name(it.get("name"))
        try:
            raw = base64.b64decode(it.get("data") or "", validate=True)
        except (binascii.Error, ValueError):
            raise HttpError(400, f"Файл «{name}» пришёл повреждённым")
        need(len(raw) <= MAX_FILE, f"Файл «{name}» больше {mb(MAX_FILE)} ({len(raw) / 1024 / 1024:.1f} МБ)", 413)
        total += len(raw)
        decoded.append((name, raw, str(it.get("type") or "")[:100]))
    need(total <= MAX_TOTAL, f"Вложения вместе больше {mb(MAX_TOTAL)}", 413)
    folder.mkdir(parents=True, exist_ok=True)
    stamp, saved = time.strftime("%Y%m%d-%H%M%S"), []
    for name, raw, ctype in decoded:
        f = folder / f"{stamp}_{name}"
        n = 1
        while f.exists():
            n += 1
            f = folder / f"{stamp}-{n}_{name}"
        f.write_bytes(raw)
        saved.append({"name": name, "file": f.name, "size": len(raw),
                      "type": ctype or mimetypes.guess_type(name)[0] or "application/octet-stream"})
    return saved


@route("POST", r"/api/chats/(\d+)/messages")
def api_chat_send(h, body, query, cid):
    c = get_chat(cid)
    text = (body.get("text") or "").strip()
    items = body.get("attachments") or []
    need(text or items, "Пустое сообщение", 400)
    need(not pl.chat.running(cid), "Я ещё отвечаю на прошлое сообщение в этом чате", 409)
    need(not pl.restart.since, "Трекер перезапускается — отправь сообщение через минуту", 409)
    attachments = save_uploads(db.chat_dir(c["project_id"], cid), items) if items else None
    notify = body.get("telegram_notify", False)
    need(isinstance(notify, bool), "telegram_notify должен быть логическим значением", 400)
    need(pl.chat.send(cid, text, attachments, notify), "Я ещё отвечаю на прошлое сообщение в этом чате", 409)
    return {"ok": True}


INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "application/pdf"}
TEXT_TYPES = ("text/", "application/json", "application/javascript", "application/xml", "image/svg+xml")


@route("GET", r"/api/chat/(\d+)/files/(\d+)")
def api_chat_file(h, body, query, mid, idx):
    m = db.one("SELECT project_id, chat_id, attachments FROM chat_messages WHERE id=?", (mid,))
    atts = json.loads(m["attachments"]) if m and m["attachments"] else []
    need(idx < len(atts), "Вложение не найдено")
    a = atts[idx]
    f = db.chat_dir(m["project_id"], m["chat_id"]) / a["file"]
    need(f.is_file(), "Файл вложения удалён")
    ctype = mimetypes.guess_type(a["name"])[0] or ""
    # активное содержимое (html, svg, js) не исполняем: текст и неизвестное — как text/plain, прочее — скачиванием
    if ctype in INLINE_TYPES:
        disp = "inline"
    elif not ctype or ctype.startswith(TEXT_TYPES):
        ctype, disp = "text/plain; charset=utf-8", "inline"
    else:
        ctype, disp = "application/octet-stream", "attachment"
    if query.get("download"):
        disp = "attachment"
    return h.send_bytes(f.read_bytes(), ctype, {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(a['name'])}"})


@route("POST", r"/api/chats/(\d+)/stop")
def api_chat_stop(h, body, query, cid):
    pl.chat.stop(cid)
    return {"ok": True}


# ---------------- импорт ----------------
@route("POST", r"/api/projects/(\d+)/import")
def api_import(h, body, query, pid):
    text = (body.get("text") or "").strip()
    need(text, "Пустой текст", 400)
    try:
        return {"tasks": pl.import_parse(pid, text)}
    except Exception as e:
        raise HttpError(500, f"Не удалось разобрать: {e}")


@route("POST", r"/api/projects/(\d+)/import/commit")
def api_import_commit(h, body, query, pid):
    cid = int(body["column_id"])
    kind = db.one("SELECT kind FROM columns WHERE id=?", (cid,))["kind"]
    for t in body.get("tasks") or []:
        if (t.get("title") or "").strip():
            notify = t.get("telegram_notify", body.get("telegram_notify", False))
            need(isinstance(notify, bool), "telegram_notify должен быть логическим значением", 400)
            tid = db.create_task(pid, t["title"].strip(), t.get("description", ""), cid, telegram_notify=notify)
            if kind == "todo":
                db.update_task(tid, status="queued")
    pl.changed(pid)
    return {"ok": True}


# ---------------- Telegram (опциональные компоненты запускаются только по запросу) ----------------
@route("GET", "/api/telegram")
def api_telegram(h, body, query):
    return telegram.state()


@route("GET", "/api/telegram/instructions")
def api_telegram_instructions(h, body, query):
    return h.send_bytes((db.ROOT / "docs/telegram-results.md").read_bytes(), "text/plain; charset=utf-8")


@route("POST", "/api/telegram/(?:check|login|test)")
def api_telegram_action(h, body, query):
    operation = urlparse(h.path).path.rsplit("/", 1)[-1]
    try:
        telegram.start(operation)
    except ValueError as e:
        raise HttpError(409, str(e))
    return telegram.state()


@route("POST", "/api/telegram/cancel")
def api_telegram_cancel(h, body, query):
    need(telegram.operation == "login", "Сейчас нет входа в Telegram", 409)
    telegram.kill()
    return {"ok": True}


@route("GET", "/api/telegram/qr")
def api_telegram_qr(h, body, query):
    path = TELEGRAM_ROOT / "qr.png"
    need(telegram.operation == "login" and path.is_file(), "QR-код ещё не готов")
    return h.send_bytes(path.read_bytes(), "image/png")


@route("POST", r"/api/tasks/(\d+)/telegram/retry")
def api_telegram_retry(h, body, query, tid):
    need(db.get_task(tid))
    try:
        telegram.retry(tid, body.get("resolution"))
    except ValueError as e:
        raise HttpError(409, str(e))
    return telegram_delivery(tid)


# ---------------- служебное ----------------
@route("POST", "/api/pick-folder")
def api_pick_folder(h, body, query):
    code = ("import tkinter as t, tkinter.filedialog as f; r=t.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "print(f.askdirectory(title='Папка проекта') or '')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8")
    return {"path": str(Path(out.stdout.strip())) if out.stdout.strip() else ""}


@route("POST", "/api/mcp/call")
def api_mcp(h, body, query):
    return pl.mcp_call(body)


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return None

    def send_text(self, text, ctype):
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return None

    def send_bytes(self, raw, ctype, headers=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)
        return None

    def _static(self, path):
        if path in ("/", "") or path.startswith("/report/"):
            path = "/index.html" if not path.startswith("/report/") else "/report.html"
        f = (WEB / path.lstrip("/")).resolve()
        if not str(f).startswith(str(WEB.resolve())) or not f.is_file():
            return self.send_json({"error": "not found"}, 404)
        raw = f.read_bytes()
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        qu = pl.hub.subscribe()
        try:
            hello = json.dumps({"type": "hello", "boot_id": pl.BOOT_ID})
            self.wfile.write(f"retry: 2000\ndata: {hello}\n\n".encode("utf-8"))
            self.wfile.flush()
            while not getattr(qu, "lost", False):
                try:
                    msg = qu.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            pl.hub.unsubscribe(qu)
            self.close_connection = True

    def _dispatch(self, method):
        url = urlparse(self.path)
        if method == "GET" and url.path == "/api/events":
            return self._events()
        if method == "GET" and not url.path.startswith("/api/"):
            return self._static(url.path)
        body = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.close_connection = True
            return self.send_json({"error": f"Слишком большой запрос: вложения вместе — не больше {mb(MAX_TOTAL)}"}, 413)
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return self.send_json({"error": "bad json"}, 400)
        for m, rx, fn in ROUTES:
            if m == method and (mt := rx.match(url.path)):
                try:
                    res = fn(self, body, parse_qs(url.query), *[int(g) for g in mt.groups()])
                    if res is not None:
                        self.send_json(res)
                except HttpError as e:
                    self.send_json({"error": e.msg}, e.code)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self.send_json({"error": str(e)}, 500)
                return
        self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR в Windows позволяет второму процессу занять уже слушаемый порт — там он не нужен
    allow_reuse_address = os.name != "nt"


def spawn_successor():
    """Новый экземпляр с тем же исполнителем. Он дождётся выхода этого процесса."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--no-browser", "--after", str(os.getpid()), "--agent", runtime.AGENT]
    if os.name != "nt":
        return subprocess.Popen(cmd, cwd=db.ROOT, start_new_session=True)
    si = subprocess.STARTUPINFO()
    si.dwFlags, si.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 7  # SW_SHOWMINNOACTIVE: свёрнуто, фокус не отнимает
    flags = subprocess.CREATE_NEW_CONSOLE
    try:  # выйти из job-объекта родителя, если он разрешает: иначе новый экземпляр могут погасить вместе со старым
        return subprocess.Popen(cmd, cwd=db.ROOT, startupinfo=si, creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(cmd, cwd=db.ROOT, startupinfo=si, creationflags=flags)


def wait_exit(pid, timeout):
    """Ждать завершения процесса pid (старого экземпляра при перезапуске)."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if h:
            k.WaitForSingleObject(h, int(timeout * 1000))
            k.CloseHandle(h)
        return
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.3)


def bind(after):
    """При перезапуске сначала ждём выхода старого экземпляра, потом до 30 с — освобождения порта."""
    if after:
        wait_exit(after, 60)
    for i in range(60 if after else 1):
        try:
            return Server(("127.0.0.1", PORT), Handler)
        except OSError:
            if not after or i == 59:
                raise
            time.sleep(0.5)


def main():
    after = int(sys.argv[sys.argv.index("--after") + 1]) if "--after" in sys.argv else None
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("AgentPipeline — " + runtime.NAME)
    try:
        srv = bind(after)
    except OSError as e:
        if after:
            pl.log_restart(f"новый экземпляр не смог занять порт {PORT}: {e}")
        raise
    pl.preflight()
    pl.orch.recover()
    telegram.change = pl.changed
    telegram.stopping = lambda: bool(pl.restart.since)
    telegram.running_tasks = lambda: set(pl.orch.runs)
    telegram.recover()
    threading.Thread(target=pl.orch.loop, daemon=True).start()
    threading.Thread(target=updater.loop, daemon=True).start()
    threading.Thread(target=pl.titler.loop, daemon=True).start()
    threading.Thread(target=telegram.loop, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    if after:
        pl.log_restart(f"новый экземпляр запущен (pid {os.getpid()}) после pid {after}")
    print(f"AgentPipeline запущен: {url}  (Ctrl+C — остановить)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)

    def restart_exit():
        p = spawn_successor()
        pl.log_restart(f"запущен новый экземпляр (pid {p.pid}), этот завершается")
        srv.shutdown()  # serve_forever в главном потоке вернётся, процесс выйдет

    pl.restart.exit = restart_exit
    try:
        srv.serve_forever()
        srv.server_close()
        os._exit(0)  # не ждать потоков SSE и чтения
    except KeyboardInterrupt:
        print("Останавливаю...")
        runs = pl.active_runs()
        for run in runs:
            run.kill()
        for run in runs:
            if hasattr(run, "finished"):
                run.finished.wait(10)
        srv.server_close()


if __name__ == "__main__":
    main()
