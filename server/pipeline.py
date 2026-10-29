"""Оркестратор задач, чат с Claude, обработчики MCP-инструментов и импорт."""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import db
from runner import CLAUDE, NO_WINDOW, ClaudeRun, short_tool


# ---------------- события для браузера (SSE) ----------------
class Hub:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def subscribe(self):
        qu = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.append(qu)
        return qu

    def unsubscribe(self, qu):
        with self.lock:
            if qu in self.clients:
                self.clients.remove(qu)

    def send(self, type_, **data):
        msg = json.dumps({"type": type_, **data}, ensure_ascii=False)
        with self.lock:
            for qu in list(self.clients):
                try:
                    qu.put_nowait(msg)
                except queue.Full:
                    pass


hub = Hub()


def changed(project_id=None):
    hub.send("changed", project_id=project_id)


def task_event(task_id, kind, text):
    eid = db.add_event(task_id, kind, text)
    hub.send("task_event", task_id=task_id, event={"id": eid, "ts": time.time(), "kind": kind, "text": text})


# ---------------- промпты ----------------
TASK_SYSTEM = """Ты — исполнитель задач в личном пайплайне ClaudePipeline. Задачи приходят из канбан-трекера, пользователь рядом не сидит и чат не читает.

Правила:
- Работай автономно. Мелкие решения принимай сам и перечисляй их в отчёте.
- Если без решения пользователя дальше никак (выбор варианта, нехватка данных, рискованное действие) — вызови mcp__tracker__ask_user и СРАЗУ заверши ход одной короткой фразой. Ответ придёт следующим сообщением в этой же сессии.
- Крупные подзадачи и исследования отдавай подагентам (инструмент Agent), чтобы не перегружать свой контекст.
- По ходу работы можешь отмечать прогресс через mcp__tracker__report_progress.
- Если по ходу нашлась отдельная работа вне рамок задачи — создай её через mcp__tracker__create_task (в бэклог), а не делай молча.
- В конце ОБЯЗАТЕЛЬНО вызови mcp__tracker__finish_task: summary — 1-3 предложения, report — подробный markdown (что сделано и как, изменённые файлы, как проверить, принятые решения, открытые вопросы).
- Пиши по-русски."""

CHAT_SYSTEM = """Ты — ассистент, встроенный в личный канбан-трекер ClaudePipeline. Пользователь пишет тебе в чат на странице трекера.
Текущий проект: «{name}» (id {id}), папка: {path}.

- Доской управляешь через инструменты mcp__tracker__* (список/создание/правка/перенос задач, вопросы агентов, состояние пайплайна).
- Задачи из колонки «В работу» выполняют отдельные агенты пайплайна, у каждой своя сессия. Сам задачи по коду не выполняй, если пользователь прямо об этом не попросил, — оформляй их задачами.
- Код проекта читать можно, чтобы точнее формулировать задачи.
- Хорошая задача: короткий заголовок + описание с контекстом и критериями готовности.
- Если пользователь вставил текст из другого трекера — разбери его на задачи и создай их.
- Отвечай кратко, по-русски."""


def task_prompt(t, project):
    p = f"# Задача #{t['id']}: {t['title']}\nПроект: {project['name']} ({project['path']})\n\n"
    p += (t["description"] or "(описания нет)").strip()
    comments = db.q("SELECT text FROM events WHERE task_id=? AND kind='comment' ORDER BY id", (t["id"],))
    if comments:
        p += "\n\n## Комментарии пользователя\n" + "\n".join(f"- {c['text']}" for c in comments)
    return p


def rerun_prompt(t):
    p = "Пользователь снова поставил эту задачу в работу.\n\n"
    p += f"Актуальный заголовок: {t['title']}\nАктуальное описание:\n{(t['description'] or '').strip()}\n"
    new = db.q("SELECT text FROM events WHERE task_id=? AND kind='comment' AND ts>? ORDER BY id",
               (t["id"], t["last_run_at"] or 0))
    if new:
        p += "\nНовые комментарии пользователя:\n" + "\n".join(f"- {c['text']}" for c in new)
    p += "\nПродолжи работу с учётом этого и в конце снова вызови finish_task."
    return p


QUIET_TOOLS = {"ToolSearch", "TodoWrite", "TaskCreate", "TaskUpdate"}  # служебные, в ленте только шумят


def transcript_path(cwd, session_id):
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / enc / f"{session_id}.jsonl"


# ---------------- оркестратор ----------------
class Orchestrator:
    def __init__(self):
        self.runs = {}  # task_id -> ClaudeRun
        self.manual_stops = set()
        self.lock = threading.RLock()

    def recover(self):
        """После перезапуска сервера: незавершённые запуски продолжить."""
        for t in db.q("SELECT * FROM tasks WHERE status='running'"):
            if t["session_id"]:
                db.move_task(t["id"], "progress", status="resume",
                             pending_prompt="Сервер пайплайна перезапускался, работа прервалась. Продолжи задачу с того места, где остановился.")
            else:
                db.move_task(t["id"], "todo", status="queued")
        db.ex("UPDATE questions SET status='cancelled' WHERE status='open' AND kind='permission'")

    def state(self):
        s = db.get_settings()
        return {
            "enabled": s["pipeline_enabled"],
            "running": list(self.runs.keys()),
            "pause_until": float(s["pause_until"] or 0),
        }

    def set_enabled(self, on):
        if on:
            db.set_setting("pipeline_enabled", "1")
        else:
            if db.get_setting("stop_mode") == "immediate":
                with self.lock:
                    for run in list(self.runs.values()):
                        run.kill()
                db.set_setting("pipeline_enabled", "0")
            else:
                db.set_setting("pipeline_enabled", "stopping" if self.runs else "0")
        hub.send("state", state=self.state())

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"[orchestrator] {e}")
            time.sleep(1.5)

    def tick(self):
        enabled = db.get_setting("pipeline_enabled")
        if enabled == "stopping" and not self.runs:
            db.set_setting("pipeline_enabled", "0")
            hub.send("state", state=self.state())
            return
        if enabled != "1":
            return
        pause = float(db.get_setting("pause_until") or 0)
        if pause > time.time():
            return
        if pause:
            db.set_setting("pause_until", "0")
            hub.send("state", state=self.state())
        with self.lock:
            free = int(db.get_setting("max_parallel") or 1) - len(self.runs)
            if free <= 0:
                return
            busy = {r.project_id for r in self.runs.values()}
            cands = db.q("""SELECT t.* FROM tasks t JOIN projects p ON p.id=t.project_id
                            WHERE p.active=1 AND t.status='resume' ORDER BY t.updated_at""")
            cands += db.q("""SELECT t.* FROM tasks t JOIN columns c ON c.id=t.column_id JOIN projects p ON p.id=t.project_id
                             WHERE p.active=1 AND c.kind='todo' AND t.status NOT IN ('running','waiting','resume')
                             ORDER BY t.position""")
            for t in cands:
                if free <= 0:
                    break
                if t["project_id"] in busy or t["id"] in self.runs:
                    continue
                self.start_task(t)
                busy.add(t["project_id"])
                free -= 1

    def start_task(self, t):
        project = db.one("SELECT * FROM projects WHERE id=?", (t["project_id"],))
        if not Path(project["path"]).is_dir():
            db.update_task(t["id"], status="error", error=f"Папка проекта не найдена: {project['path']}")
            db.move_task(t["id"], "progress")
            changed(t["project_id"])
            return
        resume = bool(t["session_id"])
        if t["status"] == "resume" and t["pending_prompt"]:
            prompt = t["pending_prompt"]
        elif resume:
            prompt = rerun_prompt(t)
        else:
            prompt = task_prompt(t, project)
        session_id = t["session_id"] or str(uuid.uuid4())
        s = db.get_settings()
        system = TASK_SYSTEM + (f"\n\nДополнительные указания пользователя:\n{s['extra_prompt']}" if s["extra_prompt"].strip() else "")
        db.move_task(t["id"], "progress", status="running", session_id=session_id, last_run_at=time.time(),
                     pending_prompt=None, error=None)
        task_event(t["id"], "system", "Запуск продолжения сессии" if resume else "Старт новой сессии")
        tid = t["id"]
        run = ClaudeRun(
            key=f"task-{tid}", cwd=project["path"], prompt=prompt, session_id=session_id, resume=resume,
            role="task", system_prompt=system, task_id=tid, project_id=t["project_id"],
            model=t["model"] or s["model"] or None, effort=t["effort"] or s["effort"] or None,
            permission_mode=s["permission_mode"], log_path=db.DATA / "runs" / f"task-{tid}.jsonl",
            on_text=lambda text: task_event(tid, "text", text),
            on_tool=lambda n, i: None if n.startswith("mcp__tracker__") or n in QUIET_TOOLS else task_event(tid, "tool", short_tool(n, i)),
            on_rate_limit=lambda info: self._rate_limited(info),
            on_done=lambda r: self._done(tid, r),
        )
        with self.lock:
            self.runs[tid] = run
        try:
            run.start()
        except Exception as e:
            with self.lock:
                self.runs.pop(tid, None)
            db.update_task(tid, status="error", error=f"Не удалось запустить claude: {e}")
        changed(t["project_id"])
        hub.send("state", state=self.state())

    def _rate_limited(self, info):
        resets = float(info.get("resetsAt") or 0) or time.time() + 15 * 60
        db.set_setting("pause_until", str(resets))
        hub.send("state", state=self.state())

    def _done(self, tid, r):
        with self.lock:
            self.runs.pop(tid, None)
        t = db.get_task(tid)
        if not t:
            return
        db.update_task(tid, cost_usd=(t["cost_usd"] or 0) + float(r.get("total_cost_usd") or 0))
        # процесс умер, ожидая разрешения — запрос больше никто не ждёт
        db.ex("UPDATE questions SET status='cancelled' WHERE task_id=? AND status='open' AND kind='permission'", (tid,))
        if t["status"] == "waiting" and not db.q("SELECT id FROM questions WHERE task_id=? AND status='open'", (tid,)):
            db.move_task(tid, "progress", status="running")
            t = db.get_task(tid)
            hub.send("questions")
        if t["status"] in ("waiting", "resume", "done"):
            pass  # ask_user / ответ пришёл раньше конца хода / finish_task
        elif r.get("killed"):
            if tid in self.manual_stops:
                self.manual_stops.discard(tid)
                db.move_task(tid, "backlog", status="interrupted")
                task_event(tid, "system", "Остановлено вручную (сессия сохранена: верни в «В работу», чтобы продолжить)")
            else:
                db.update_task(tid, status="resume",
                               pending_prompt="Работу прервали (пайплайн выключали). Продолжи задачу с того места, где остановился.")
                task_event(tid, "system", "Пайплайн выключен — задача продолжится при следующем включении")
        elif r.get("rate_limited") or (r.get("is_error") and re.search(r"usage limit|rate limit|limit reached", str(r.get("result")), re.I)):
            if not r.get("rate_limited"):
                self._rate_limited({})
            db.update_task(tid, status="resume",
                           pending_prompt="Работа прервалась из-за лимита подписки. Продолжи задачу с того места, где остановился.")
            task_event(tid, "system", "Упёрлись в лимит, продолжу после сброса")
        elif r.get("is_error"):
            db.update_task(tid, status="error", error=str(r.get("result"))[:2000])
            task_event(tid, "error", str(r.get("result"))[:2000])
        else:
            # Агент не вызвал finish_task — берём финальный ответ как отчёт
            text = str(r.get("result") or "").strip() or "(агент не оставил отчёта)"
            finish_task(tid, text[:400], text)
        changed(t["project_id"])
        hub.send("state", state=self.state())

    def stop_task(self, tid):
        with self.lock:
            run = self.runs.get(tid)
        if run:
            self.manual_stops.add(tid)
            run.kill()
        else:
            t = db.get_task(tid)
            if t and t["status"] in ("resume", "running"):
                db.move_task(tid, "backlog", status="interrupted")
                changed(t["project_id"])


orch = Orchestrator()


def finish_task(tid, summary, report):
    t = db.get_task(tid)
    path = db.DATA / "reports" / f"task-{tid}.md"
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    md = f"# Задача #{tid}: {t['title']}\n\n"
    md += f"**Проект:** {t['project_name']} · **Завершено:** {stamp}\n\n"
    md += f"## Итог\n\n{summary}\n\n## Отчёт\n\n{report}\n\n"
    md += "---\n\n### Служебное\n\n"
    md += f"- Сессия Claude: `{t['session_id']}`\n"
    md += f"- Продолжить вручную: `cd \"{t['project_path']}\"` и `claude --resume {t['session_id']}`\n"
    md += f"- Транскрипт: `{transcript_path(t['project_path'], t['session_id'])}`\n"
    md += f"- Сырой лог пайплайна: `{db.DATA / 'runs' / f'task-{tid}.jsonl'}`\n"
    if path.exists():
        prev = path.read_text(encoding="utf-8")
        md += "\n\n---\n\n<details><summary>Предыдущий отчёт</summary>\n\n" + prev + "\n\n</details>\n"
    path.write_text(md, encoding="utf-8")
    db.move_task(tid, "done", status="done", summary=summary, report_path=str(path))
    task_event(tid, "finish", summary)
    changed(t["project_id"])


def answer_question(qid, answer):
    qu = db.get_question(qid)
    if not qu or qu["status"] != "open":
        return False
    db.ex("UPDATE questions SET status='answered', answer=?, answered_at=? WHERE id=?", (answer, time.time(), qid))
    t = db.get_task(qu["task_id"])
    task_event(t["id"], "answer", answer)
    if qu["kind"] == "permission":
        if t["status"] == "waiting":
            db.move_task(t["id"], "progress", status="running")
    else:
        still_open = db.q("SELECT id FROM questions WHERE task_id=? AND status='open'", (t["id"],))
        if not still_open:
            answered = db.q("""SELECT text, answer FROM questions WHERE task_id=? AND kind='question'
                               AND answered_at>=? ORDER BY id""", (t["id"], t["last_run_at"] or 0))
            prompt = "Ответы пользователя на твои вопросы:\n\n" + "\n\n".join(
                f"Вопрос: {a['text']}\nОтвет: {a['answer']}" for a in answered)
            prompt += "\n\nПродолжай работу над задачей."
            db.move_task(t["id"], "progress", status="resume", pending_prompt=prompt)
    changed(t["project_id"])
    hub.send("questions")
    return True


def on_manual_move(tid, kind):
    """Реакция на ручной перенос карточки."""
    t = db.get_task(tid)
    if kind == "todo":
        db.update_task(tid, status="queued")
    elif kind == "done":
        db.update_task(tid, status="done")
    elif kind == "backlog" and t["status"] not in ("running",):
        db.update_task(tid, status="idle")
    if kind in ("todo", "done", "backlog"):
        db.ex("UPDATE questions SET status='cancelled' WHERE task_id=? AND status='open' AND kind='question'", (tid,))
        hub.send("questions")


# ---------------- чат ----------------
class Chat:
    def __init__(self):
        self.runs = {}  # project_id -> ClaudeRun

    def running(self, pid):
        return pid in self.runs

    def send(self, pid, text, attachments=None):
        """attachments — уже сохранённые файлы: [{name, file, size, type}] в db.UPLOADS/<pid>/."""
        if pid in self.runs:
            return False
        p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        cwd = p["path"] if Path(p["path"]).is_dir() else str(db.ROOT)
        self._msg(pid, "user", text, attachments)
        prompt = text
        if attachments:
            folder = db.UPLOADS / str(pid)
            prompt = (text or "Пользователь прислал вложения без текста — посмотри их.")
            prompt += "\n\nВложения (открой через Read, картинки ты видишь):\n"
            prompt += "\n".join(f"- {folder / a['file']}" for a in attachments)
        resume = bool(p["chat_session_id"])
        sid = p["chat_session_id"] or str(uuid.uuid4())
        if not resume:
            db.ex("UPDATE projects SET chat_session_id=? WHERE id=?", (sid, pid))
        s = db.get_settings()
        run = ClaudeRun(
            key=f"chat-{pid}", cwd=cwd, prompt=prompt, session_id=sid, resume=resume, role="chat",
            system_prompt=CHAT_SYSTEM.format(name=p["name"], id=pid, path=p["path"]), project_id=pid,
            model=s["chat_model"] or s["model"] or None, permission_mode=s["permission_mode"],
            log_path=db.DATA / "runs" / f"chat-{pid}.jsonl", add_dirs=[str(db.UPLOADS / str(pid))] if attachments else None,
            on_text=lambda t: self._msg(pid, "assistant", t),
            on_tool=lambda n, i: None if n in QUIET_TOOLS else self._msg(pid, "tool", short_tool(n.replace("mcp__tracker__", "трекер."), i)),
            on_done=lambda r: self._done(pid, r),
        )
        self.runs[pid] = run
        hub.send("chat_state", project_id=pid, running=True)
        try:
            run.start()
        except Exception as e:
            self.runs.pop(pid, None)
            self._msg(pid, "error", f"Не удалось запустить claude: {e}")
            hub.send("chat_state", project_id=pid, running=False)
        return True

    def _msg(self, pid, role, text, attachments=None):
        mid = db.add_chat(pid, role, text, attachments)
        hub.send("chat", project_id=pid, message={"id": mid, "role": role, "text": text, "ts": time.time(),
                                                  "attachments": attachments or []})

    def _done(self, pid, r):
        self.runs.pop(pid, None)
        if r.get("is_error") and not r.get("killed"):
            err = str(r.get("result"))
            if "No conversation found" in err:
                db.ex("UPDATE projects SET chat_session_id=NULL WHERE id=?", (pid,))
                err += "\n(сессия чата сброшена, отправь сообщение ещё раз)"
            self._msg(pid, "error", err[:2000])
        hub.send("chat_state", project_id=pid, running=False)

    def stop(self, pid):
        run = self.runs.get(pid)
        if run:
            run.kill()

    def reset(self, pid):
        self.stop(pid)
        db.ex("UPDATE projects SET chat_session_id=NULL WHERE id=?", (pid,))
        db.ex("DELETE FROM chat_messages WHERE project_id=?", (pid,))
        drop_uploads(pid)
        hub.send("chat_reset", project_id=pid)


def drop_uploads(pid):
    """Вложения чата проекта больше не нужны: история и сессия удалены."""
    shutil.rmtree(db.UPLOADS / str(pid), ignore_errors=True)


chat = Chat()


# ---------------- обработчики MCP-инструментов ----------------
def _task_brief(t):
    return {"id": t["id"], "title": t["title"], "column": t["column_name"], "status": t["status"],
            "summary": t["summary"]}


def _col(pid, name):
    c = db.find_column(pid, name or "backlog")
    if not c:
        raise ValueError(f"Колонка «{name}» не найдена. Есть: " +
                         ", ".join(f"{x['name']} ({x['kind'] or 'своя'})" for x in
                                   db.q("SELECT * FROM columns WHERE project_id=? ORDER BY position", (pid,))))
    return c


def mcp_call(body):
    tool, a = body["tool"], body.get("args") or {}
    tid = int(body["task_id"]) if body.get("task_id") else None
    pid = int(body["project_id"]) if body.get("project_id") else None
    if tid and not pid:
        pid = db.get_task(tid)["project_id"]
    try:
        return {"text": _mcp(tool, a, tid, pid)}
    except Exception as e:
        return {"text": f"Ошибка: {e}", "isError": True}


def _mcp(tool, a, tid, pid):
    J = lambda x: json.dumps(x, ensure_ascii=False, indent=1)
    if tool == "ask_user":
        opts = [o for o in (a.get("options") or []) if str(o).strip()]
        qid = db.add_question(tid, a["question"], opts)
        db.move_task(tid, "waiting", status="waiting")
        task_event(tid, "question", a["question"] + (("\nВарианты: " + " | ".join(opts)) if opts else ""))
        changed(pid)
        hub.send("questions")
        return (f"Вопрос #{qid} отправлен пользователю. Сейчас же заверши ход одной короткой фразой, "
                "ничего больше не делай — ответ придёт следующим сообщением.")
    if tool == "report_progress":
        task_event(tid, "progress", a["text"])
        return "Ок"
    if tool == "finish_task":
        finish_task(tid, a["summary"], a["report"])
        return "Задача закрыта, отчёт сохранён. Заверши ход."
    if tool == "approve":
        if not tid:  # чат: разрешаем, пользователь сам на странице
            return J({"behavior": "allow", "updatedInput": a.get("input") or {}})
        text = short_tool(a.get("tool_name", "?"), a.get("input"))
        qid = db.add_question(tid, f"Разрешить действие?\n{text}", ["Разрешить", "Запретить"],
                              kind="permission", payload=a)
        db.move_task(tid, "waiting", status="waiting")
        task_event(tid, "question", f"Запрос разрешения: {text}")
        changed(pid)
        hub.send("questions")
        return J({"question_id": qid})  # mcp_tracker сам ждёт ответа
    if tool == "create_task":
        target = int(a.get("project_id") or pid)
        c = _col(target, a.get("column"))
        new = db.create_task(target, a["title"], a.get("description", ""), c["id"])
        if c["kind"] == "todo":
            db.update_task(new, status="queued")
        if tid:
            task_event(tid, "progress", f"Создана задача #{new}: {a['title']}")
        changed(target)
        return f"Создана задача #{new} в колонке «{c['name']}»"
    if tool == "list_projects":
        return J(db.q("SELECT id, name, path, active FROM projects ORDER BY id"))
    if tool == "list_tasks":
        target = int(a.get("project_id") or pid)
        rows = [db.get_task(r["id"]) for r in db.q("SELECT id FROM tasks WHERE project_id=? ORDER BY column_id, position", (target,))]
        if a.get("column"):
            c = _col(target, a["column"])
            rows = [r for r in rows if r["column_id"] == c["id"]]
        return J([_task_brief(r) for r in rows]) if rows else "Задач нет"
    if tool == "get_task":
        t = db.get_task(int(a["task_id"]))
        if not t:
            return "Нет такой задачи"
        out = {k: t[k] for k in ("id", "title", "description", "column_name", "status", "summary", "error", "project_name")}
        if t["report_path"] and Path(t["report_path"]).exists():
            out["report"] = Path(t["report_path"]).read_text(encoding="utf-8")[:6000]
        out["events"] = [f"{e['kind']}: {e['text'][:300]}" for e in db.task_events(t["id"], limit=30)]
        out["questions"] = db.q("SELECT id, text, answer, status FROM questions WHERE task_id=?", (t["id"],))
        return J(out)
    if tool == "update_task":
        fields = {k: a[k] for k in ("title", "description") if a.get(k) is not None}
        db.update_task(int(a["task_id"]), **fields)
        changed(db.get_task(int(a["task_id"]))["project_id"])
        return "Обновлено"
    if tool == "move_task":
        t = db.get_task(int(a["task_id"]))
        if t["id"] in orch.runs:
            return "Задача сейчас выполняется, переносить нельзя"
        c = _col(t["project_id"], a["column"])
        db.move_task(t["id"], column_id=c["id"])
        on_manual_move(t["id"], c["kind"])
        changed(t["project_id"])
        return f"Перенесено в «{c['name']}»"
    if tool == "list_questions":
        rows = db.list_questions("open")
        return J([{"question_id": r["id"], "task": f"#{r['task_id']} {r['task_title']}", "project": r["project_name"],
                   "text": r["text"], "options": r["options"]} for r in rows]) if rows else "Открытых вопросов нет"
    if tool == "answer_question":
        return "Ответ отправлен" if answer_question(int(a["question_id"]), a["answer"]) else "Вопрос уже закрыт"
    if tool == "pipeline_status":
        st = orch.state()
        st["running"] = [_task_brief(db.get_task(i)) for i in st["running"]]
        st["queue"] = [_task_brief(db.get_task(r["id"])) for r in db.q(
            "SELECT t.id FROM tasks t JOIN columns c ON c.id=t.column_id WHERE c.kind='todo' AND t.status!='running'")]
        return J(st)
    if tool == "set_pipeline":
        orch.set_enabled(bool(a["enabled"]))
        return "Пайплайн " + ("включён" if a["enabled"] else "выключается")
    if tool == "approve_status":
        qu = db.get_question(int(a["question_id"]))
        if qu["status"] == "open":
            return J({"done": False})
        payload = qu["payload"] or {}
        if qu["status"] == "answered" and qu["answer"].strip().lower().startswith("разреш"):
            res = {"behavior": "allow", "updatedInput": payload.get("input") or {}}
        else:
            res = {"behavior": "deny", "message": f"Пользователь не разрешил. Ответ: {qu['answer'] or 'отменено'}"}
        return J({"done": True, **res})
    raise ValueError(f"Неизвестный инструмент {tool}")


# ---------------- импорт ----------------
IMPORT_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
        "required": ["title", "description"]}}},
    "required": ["tasks"],
}

IMPORT_PROMPT = """Ниже текст, скопированный из другого трекера, заметок или переписки. Разбери его на отдельные задачи для канбан-доски.
Для каждой задачи: короткий понятный заголовок (до 80 символов) и описание, где сохранено всё полезное из исходника: детали, шаги воспроизведения, критерии, ссылки, номера тикетов. Ничего не выдумывай. Если задача в тексте одна — верни одну. Файлы и инструменты не используй, просто разбери текст. Пиши по-русски (термины и цитаты оставляй как есть).

---
{text}"""


def import_parse(pid, text):
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    cwd = p["path"] if Path(p["path"]).is_dir() else str(db.ROOT)
    s = db.get_settings()
    cmd = [CLAUDE, "-p", "--output-format", "json", "--strict-mcp-config", "--no-session-persistence",
           "--json-schema", json.dumps(IMPORT_SCHEMA)]
    if s["chat_model"] or s["model"]:
        cmd += ["--model", s["chat_model"] or s["model"]]
    r = subprocess.run(cmd, input=IMPORT_PROMPT.format(text=text).encode("utf-8"), capture_output=True,
                       cwd=cwd, timeout=600, creationflags=NO_WINDOW)
    out = json.loads(r.stdout.decode("utf-8", errors="replace") or "{}")
    data = out.get("structured_output")
    if not data:
        m = re.search(r"\{.*\}", out.get("result") or "", re.S)
        data = json.loads(m.group(0)) if m else {"tasks": []}
    return data.get("tasks", [])
