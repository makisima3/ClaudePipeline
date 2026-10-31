"""Оркестратор задач, чат с Claude, обработчики MCP-инструментов и импорт."""
import json
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import db
import results
from runner import CLAUDE, NO_WINDOW, ClaudeRun, short_tool
from runtime import AGENT, NAME
from telegram import telegram, prepare as prepare_telegram, delivery as telegram_delivery


CODEX_SYSTEM = """Используй доступные инструменты Codex для файлов и команд, view_image для изображений.
Инструменты трекера доступны через MCP tracker; найди их, если они отложены.
Соблюдай AGENTS.md проекта. Не ищи инструмент Claude Agent или Read.
"""


def agent_info():
    if AGENT == "codex":
        from codex_runner import INFO
        return INFO
    return {"models": [{"id": m, "model": m, "displayName": m} for m in ("opus", "sonnet", "haiku", "fable")]}


def preflight():
    if AGENT == "codex":
        from codex_runner import preflight as check
        check()


def active_runs():
    runs = list(orch.runs.values()) + list(chat.runs.values())
    if telegram.busy:
        runs.append(telegram)
    if AGENT == "codex":
        from codex_runner import active_runs as codex_active
        runs += codex_active()
    return list(set(runs))


def agent_run(**kwargs):
    if AGENT == "codex":
        from codex_runner import CodexRun
        return CodexRun(**kwargs)
    for key in ("on_session", "images"):
        kwargs.pop(key, None)
    return ClaudeRun(**kwargs)


def task_context(t):
    parts = [task_prompt(t, {"name": t["project_name"], "path": t["project_path"]})]
    if t.get("summary"):
        parts.append("Предыдущий итог: " + t["summary"])
    if t.get("report_path") and Path(t["report_path"]).is_file():
        parts.append("Предыдущий отчёт: " + t["report_path"])
    parts += [e["kind"] + ": " + e["text"] for e in db.task_events(t["id"], limit=100)]
    return "Контекст работы другого исполнителя (его сессия сюда не переносится):\n" + "\n\n".join(parts)


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
                    qu.lost = True  # клиент не успевает читать: /api/events оборвёт поток, браузер переподключится и всё перечитает


hub = Hub()
BOOT_ID = uuid.uuid4().hex  # меняется при каждом старте сервера: по нему страница понимает, что трекер перезапустился


def changed(project_id=None):
    hub.send("changed", project_id=project_id)


def changed_links(project_id, task_ids):
    """Связь видна в обеих карточках: обновляем и проекты связанных задач."""
    for pid in {project_id} | {t["project_id"] for t in map(db.get_task, task_ids) if t}:
        changed(pid)


def task_event(task_id, kind, text):
    eid = db.add_event(task_id, kind, text)
    hub.send("task_event", task_id=task_id, event={"id": eid, "ts": time.time(), "kind": kind, "text": text})


# ---------------- промпты ----------------
TASK_SYSTEM = """Ты — исполнитель задач в личном пайплайне AgentPipeline. Задачи приходят из канбан-трекера, пользователь рядом не сидит и чат не читает.

Правила:
- Пользовательский #номер — task_number внутри проекта; технический task_id глобален и используется в MCP/API, links, PIPELINE_TASK_ID и путях файлов. Не подставляй #номер вместо task_id. В новых материалах пиши «Проект #номер», для межпроектных ссылок используй [Проект #номер](/?task_id=<глобальный id>). finish_task/ask_user адресуются текущей задаче автоматически.
- Работай автономно. Мелкие решения принимай сам и перечисляй их в отчёте.
- Если без решения пользователя дальше никак (выбор варианта, нехватка данных, рискованное действие) — вызови mcp__tracker__ask_user и СРАЗУ заверши ход одной короткой фразой. Ответ придёт следующим сообщением в этой же сессии.
- Крупные подзадачи и исследования отдавай подагентам (инструмент Agent), чтобы не перегружать свой контекст.
- По ходу работы можешь отмечать прогресс через mcp__tracker__report_progress.
- Фоновые процессы, которые ты запускал (серверы, watch, установщики), останавливай перед ask_user и finish_task: после конца хода пайплайн всё равно погасит их вместе с твоим процессом. Боевой сервер трекера не трогай.
- Если по ходу нашлась отдельная работа вне рамок задачи — создай её через mcp__tracker__create_task (в бэклог), а не делай молча.
- Git. Режим указан в промпте задачи строкой «Git: …». Если он не off и папка проекта — git-репозиторий: в начале посмотри `git status`, чтобы отличать чужие незакоммиченные изменения от своих; в конце, перед finish_task, закоммить ТОЛЬКО изменения своей задачи с сообщением `<проект> #<task_number> <заголовок задачи>` и 2–5 строками, что сделано; в описании коммита укажи технический task_id. Не связанные между собой изменения — отдельными коммитами. Файлы добавляй явно по путям (общий файл — по кускам: `git diff` → свой патч → `git apply --cached`), без `git add -A`. Чужие незакоммиченные изменения не трогай, не откатывай и не включай — упомяни их в отчёте. В режиме commit_push после коммита сделай `git push` без force; если push не прошёл (нет сети, конфликт, отказ), ничего не обходи (никаких force, rebase, reset) — напиши об этом в отчёте. Режим off или папка не git-репозиторий — git не трогай.
- Для Git используй существующую авторизацию и конфигурацию проекта. Интерактивный вход пользователь проходит сам; не перенастраивай доступ без его просьбы. Не выводи токены/ключи и URL с секретами в UI, логи или отчёт; ошибку push описывай понятной причиной без секретов. Вход gh или успешное чтение не гарантируют право записи в репозиторий/ветку. Если конфигурация push включает mirror или принудительные refspec, push пропусти и объясни причину в отчёте.
- В конце ОБЯЗАТЕЛЬНО вызови mcp__tracker__finish_task: summary — 1-3 предложения, report — подробный markdown (что сделано и как, изменённые файлы, как проверить, принятые решения, открытые вопросы).
- ВСЕГДА подготовь result_text и result_files для общей ленты «Результаты», независимо от telegram_notify и подключения Telegram. Это итог и реально подготовленные материалы (для визуальных изменений — скриншоты, видео — если полезно и доступно). Файлы должны существовать при finish_task; сервер сохраняет независимые снимки. Прежние telegram_text/telegram_files — совместимые имена тех же общих материалов; для Telegram используются они же. Готовность и флаг Telegram не отменяют подготовку результата. Общие функции и исправления трекера проверяй и на сайте, и в Android; различия платформ фиксируй явно.
- Если для задачи включён Telegram или пользователь явно просит прислать результат в ТГ, подготовь в finish_task telegram_text (кратко: что сделано и что действительно проверено) и telegram_files (пути к материалам). Для визуальных изменений, особенно игровых, приложи скриншоты; короткое видео — когда оно помогает показать движение/взаимодействие и его реально можно записать. О пройденных тестах пиши только если они запускались и прошли. Явную просьбу учти через telegram_notify=true. Файлы должны существовать при вызове finish_task: трекер скопирует их и сам доставит после выхода агента, напрямую ничего не отправляй. Инструкция: {telegram_guide}.
- Если задача меняла код самого трекера AgentPipeline (папка server/), после finish_task вызови mcp__tracker__restart_tracker: трекер перезапустится сам, когда твой процесс завершится. Сервер трекера руками не останавливай и не перезапускай.
- Пиши по-русски."""

CHAT_SYSTEM = """Ты — ассистент, встроенный в личный канбан-трекер AgentPipeline. Пользователь пишет тебе в чат на странице трекера.
Текущий проект: «{name}» (id {id}), папка: {path}.

- Пользовательский #номер — task_number внутри проекта. Технический task_id (также id) глобален: только его передавай в get_task/update_task/move_task, links/unlink. Сначала найди номер через list_tasks текущего проекта; для другого проекта укажи project_id. При неоднозначности уточни проект. В ответах используй «Проект #номер», для межпроектных ссылок — [Проект #номер](/?task_id=<глобальный id>).
- Доской управляешь через инструменты mcp__tracker__* (список/создание/правка/перенос задач, вопросы агентов, состояние пайплайна).
- Задачи из колонки «В работу» выполняют отдельные агенты пайплайна, у каждой своя сессия. Сам задачи по коду не выполняй, если пользователь прямо об этом не попросил, — оформляй их задачами.
- Код проекта читать можно, чтобы точнее формулировать задачи.
- Хорошая задача: короткий заголовок + описание с контекстом и критериями готовности.
- В create_task всегда передавай telegram_notify из настройки устройства в текущем сообщении. Явная просьба прислать результат в Telegram включает telegram_notify=true для конкретной задачи, даже при выключенной настройке; при редактировании передавай её в update_task. Общая настройка относится только к новым задачам, старые массово не меняй.
- Если пользователь вставил текст из другого трекера — разбери его на задачи и создай их.
- Отвечай кратко, по-русски."""


GIT_MODES = {
    "off": "off — git не трогай",
    "commit": "commit — в конце закоммить изменения задачи, push не делай",
    "commit_push": "commit_push — в конце закоммить изменения задачи и сделай push (без force)",
}


def git_line():
    """Строка с режимом git_mode для промпта задачи."""
    mode = db.get_setting("git_mode")
    return f"Git: {GIT_MODES.get(mode, GIT_MODES['commit_push'])}"


STATUS_NAMES = {"idle": "не запускалась", "queued": "в очереди", "running": "выполняется", "resume": "продолжит",
                "waiting": "ждёт ответа", "done": "готово", "error": "ошибка", "interrupted": "прервано"}


def task_label(t):
    return f"{t['project_name']} #{t['task_number']}"


def task_address(t):
    return (f"Задача: {task_label(t)}. Технический task_id={t['id']}, project_id={t['project_id']}. "
            "В MCP/API используй task_id; #номер служит только для показа внутри проекта.")


def link_info(l):
    """Связанная задача для промпта и get_task: путь к отчёту — только если файл есть."""
    rp = l["report_path"] if l["report_path"] and Path(l["report_path"]).exists() else None
    return {"id": l["id"], "task_id": l["id"], "task_number": l["task_number"], "project_name": l["project_name"],
            "title": l["title"], "column": l["column_name"], "status": l["status"],
            "project_id": l["project_id"], "project": l["project_name"], "summary": l["summary"], "report_path": rp}


def links_block(t, head="## Связанные задачи"):
    """Раздел о связанных задачах для промпта; пусто, если связей нет."""
    links = db.task_links(t["id"])
    if not links:
        return ""
    p = (f"\n\n{head}\nПользователь связал их с этой задачей для контекста. Подробности — в отчётах "
         "(прочитай через Read, если нужно) или через mcp__tracker__get_task.\n")
    for l in map(link_info, links):
        st = STATUS_NAMES.get(l["status"], l["status"])
        where = l["column"] + (f", {st}" if st.casefold() != l["column"].casefold() else "")
        if l["project_id"] != t["project_id"]:
            where += f", проект «{l['project']}»"
        summary = (l["summary"] or "пока нет").strip().rstrip(".")
        p += f"- {task_label(l)} «{l['title']}» (task_id={l['id']}, project_id={l['project_id']}) — {where}. Итог: {summary}. "
        p += f"Отчёт: {l['report_path']}\n" if l["report_path"] else "Отчёта нет.\n"
    return p.rstrip()


def task_prompt(t, project):
    p = f"# Задача {project['name']} #{t['task_number']}: {t['title']}\nПроект: {project['name']} ({project['path']})\n{task_address(t)}\n{git_line()}\n\n"
    p += (t["description"] or "(описания нет)").strip()
    p += f"\n\nРезультат в Telegram: {'да' if t.get('telegram_notify') else 'нет'} (флаг этой задачи)."
    comments = db.q("SELECT text FROM events WHERE task_id=? AND kind='comment' ORDER BY id", (t["id"],))
    if comments:
        p += "\n\n## Комментарии пользователя\n" + "\n".join(f"- {c['text']}" for c in comments)
    return p + links_block(t)


def rerun_prompt(t):
    p = "Пользователь снова поставил эту задачу в работу.\n" + task_address(t) + "\n\n"
    p += f"Актуальный заголовок: {t['title']}\nАктуальное описание:\n{(t['description'] or '').strip()}\n"
    new = db.q("SELECT text FROM events WHERE task_id=? AND kind='comment' AND ts>? ORDER BY id",
               (t["id"], t["last_run_at"] or 0))
    if new:
        p += "\nНовые комментарии пользователя:\n" + "\n".join(f"- {c['text']}" for c in new)
    links = links_block(t, "Связанные задачи (актуально):")
    if links:
        p += links + "\n"
    p += f"\nРезультат в Telegram: {'да' if t.get('telegram_notify') else 'нет'} (флаг этой задачи)."
    p += f"\n{git_line()}\nПродолжи работу с учётом этого и в конце снова вызови finish_task."
    return p


QUIET_TOOLS = {"ToolSearch", "TodoWrite", "TaskCreate", "TaskUpdate"}  # служебные, в ленте только шумят


def transcript_path(cwd, session_id):
    if not session_id:
        return None
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    if AGENT == "codex":
        import os
        folder = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
        return next(folder.glob(f"**/*{session_id}.jsonl"), None) if session_id else None
    return Path.home() / ".claude" / "projects" / enc / f"{session_id}.jsonl"


# ---------------- оркестратор ----------------
class Orchestrator:
    def __init__(self):
        self.runs = {}  # task_id -> ClaudeRun
        self.manual_stops = set()
        self.lock = threading.RLock()

    def recover(self):
        """После перезапуска сервера: незавершённые запуски продолжить."""
        for t in map(db.agent_row, db.q("SELECT * FROM tasks WHERE status='running'")):
            if t["session_id"]:
                db.move_task(t["id"], "progress", status="resume",
                             pending_prompt="Сервер пайплайна перезапускался, работа прервалась. Продолжи задачу с того места, где остановился.")
            else:
                db.move_task(t["id"], "todo", status="queued")
        db.ex("UPDATE questions SET status='cancelled' WHERE status='open' AND kind='permission'")
        for t in db.q("""SELECT t.id FROM tasks t WHERE t.status='waiting' AND NOT EXISTS
                         (SELECT 1 FROM questions q WHERE q.task_id=t.id AND q.status='open')"""):
            db.move_task(t["id"], "progress", status="resume",
                         pending_prompt="Сервер перезапустился во время ожидания разрешения. Продолжи задачу; при необходимости запроси разрешение заново.")

    def state(self):
        s = db.get_settings()
        return {
            "enabled": s["pipeline_enabled"],
            "running": list(self.runs.keys()),
            "pause_until": float(s["pause_until"] or 0),
            "pause_estimated": db.get_setting("pause_estimated") == "1",
            "restart": restart.info(),
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
                restart.check()
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
        if enabled != "1" or restart.since:  # перед перезапуском новые задачи не берём
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
        t = db.get_task(t["id"])
        project = db.one("SELECT * FROM projects WHERE id=?", (t["project_id"],))
        if not Path(project["path"]).is_dir():
            db.update_task(t["id"], status="error", error=f"Папка проекта не найдена: {project['path']}")
            db.move_task(t["id"], "progress")
            changed(t["project_id"])
            return
        resume = bool(t["session_id"])
        if t["status"] == "resume" and t["pending_prompt"]:
            prompt = t["pending_prompt"] + f"\nРезультат в Telegram: {'да' if t['telegram_notify'] else 'нет'} (актуальный флаг задачи)."
        elif resume:
            prompt = rerun_prompt(t)
        else:
            prompt = task_prompt(t, project)
        if t["status"] == "resume" and t["pending_prompt"]:
            prompt = task_address(t) + "\n\n" + prompt
        previous = t.get("last_provider") or ("claude" if db.one("SELECT session_id FROM tasks WHERE id=?", (t["id"],))["session_id"] else None)
        if previous and previous != AGENT:
            prompt = task_context(t) + "\n\n" + prompt
        session_id = t["session_id"] or (str(uuid.uuid4()) if AGENT == "claude" else None)
        s = db.get_settings()
        system = TASK_SYSTEM.replace("{telegram_guide}", str(db.ROOT / "docs/telegram-results.md"))
        if AGENT == "codex":
            system = system.replace("(инструмент Agent)", "(доступные инструменты подагентов Codex)") + "\n" + CODEX_SYSTEM
        system +=  (f"\n\nДополнительные указания пользователя:\n{s['extra_prompt']}" if s["extra_prompt"].strip() else "")
        db.move_task(t["id"], "progress", status="running", session_id=session_id, last_run_at=time.time(),
                     pending_prompt=None, error=None)
        task_event(t["id"], "system", "Запуск продолжения сессии" if resume else "Старт новой сессии")
        tid = t["id"]
        run = agent_run(
            key=f"task-{tid}", cwd=project["path"], prompt=prompt, session_id=session_id, resume=resume,
            role="task", system_prompt=system, task_id=tid, project_id=t["project_id"],
            model=t["model"] or s["model"] or None, effort=t["effort"] or s["effort"] or None,
            permission_mode=s["permission_mode"], log_path=db.DATA / "runs" / f"task-{tid}.jsonl",
            on_text=lambda text: task_event(tid, "text", text),
            on_tool=lambda n, i: None if n.startswith("mcp__tracker__") or n in QUIET_TOOLS else task_event(tid, "tool", short_tool(n, i)),
            on_rate_limit=lambda info: self._rate_limited(info),
            on_done=lambda r: self._done(tid, r),
            on_session=lambda sid: db.update_task(tid, session_id=sid),
        )
        with self.lock:
            self.runs[tid] = run
        try:
            run.start()
        except Exception as e:
            with self.lock:
                self.runs.pop(tid, None)
            db.update_task(tid, status="error", error=f"Не удалось запустить {NAME}: {e}")
        changed(t["project_id"])
        hub.send("state", state=self.state())

    def _rate_limited(self, info):
        resets = float(info.get("resetsAt") or 0) or time.time() + 15 * 60
        db.set_setting("pause_until", str(resets))
        db.set_setting("pause_estimated", "1" if info.get("estimated", not info.get("resetsAt")) else "0")
        hub.send("state", state=self.state())

    def _done(self, tid, r):
        # задача остаётся в runs, пока итог не разобран: иначе tick() успеет стартовать продолжение раньше
        with self.lock:
            try:
                self._finish_run(tid, r)
            finally:
                self.runs.pop(tid, None)
        hub.send("state", state=self.state())

    def _finish_run(self, tid, r):
        t = db.get_task(tid)
        if not t:
            return
        if AGENT == "claude":
            db.update_task(tid, cost_usd=(t["cost_usd"] or 0) + float(r.get("total_cost_usd") or 0))
        if not r.get("is_error") and not r.get("killed"):
            db.update_task(tid, last_provider=AGENT)
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
            elif restart.since:
                db.update_task(tid, status="resume",
                               pending_prompt="Работу прервали: трекер перезапускался. Продолжи задачу с того места, где остановился.")
                task_event(tid, "system", "Прервано перезапуском трекера — задача продолжится после него")
            else:
                db.update_task(tid, status="resume",
                               pending_prompt="Работу прервали (пайплайн выключали). Продолжи задачу с того места, где остановился.")
                task_event(tid, "system", "Пайплайн выключен — задача продолжится при следующем включении")
        elif r.get("rate_limited") or (r.get("is_error") and re.search(r"usage limit|rate limit|limit reached", str(r.get("result")), re.I)):
            self._rate_limited(r.get("rate_limited") or {})
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
        if r.get("reaped"):
            task_event(tid, "system", "Ход закончен, но процесс ждал фоновые задачи — остановлен вместе с ними")
        changed(t["project_id"])

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


def finish_task(tid, summary, report, telegram_text=None, telegram_files=None, telegram_notify=None):
    with orch.lock:
        return _finish_task(tid, summary, report, telegram_text, telegram_files, telegram_notify)


def _finish_task(tid, summary, report, telegram_text=None, telegram_files=None, telegram_notify=None):
    t = db.get_task(tid)
    if telegram_notify is True:
        db.update_task(tid, telegram_notify=1)
        t["telegram_notify"] = 1
    result = results.prepare(t, summary, report, telegram_text, telegram_files)
    if t["status"] == "done" and t["summary"] == summary:
        # Совместимость со старым исполнителем и повторным finish до перезапуска.
        results.save(result)
        if t["telegram_notify"]:
            prepare_telegram(t, summary)
        return  # повтор события завершения не создаёт новый отчёт или сообщение
    path = db.DATA / "reports" / f"task-{tid}.md"
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    md = f"# Задача {task_label(t)}: {t['title']}\n\nТехнический task_id: {tid}; project_id: {t['project_id']}.\n\n"
    md += f"**Проект:** {t['project_name']} · **Завершено:** {stamp}\n\n"
    md += f"## Итог\n\n{summary}\n\n## Отчёт\n\n{report}\n\n"
    md += "---\n\n### Служебное\n\n"
    md += f"- Сессия {NAME}: `{t['session_id']}`\n"
    md += f"- Продолжить вручную: `cd \"{t['project_path']}\"` и `{'codex resume' if AGENT == 'codex' else 'claude --resume'} {t['session_id']}`\n"
    transcript = transcript_path(t['project_path'], t['session_id'])
    if transcript:
        md += f"- Транскрипт: `{transcript}`\n"
    md += f"- Сырой лог пайплайна: `{db.DATA / 'runs' / f'task-{tid}.jsonl'}`\n"
    if path.exists():
        prev = path.read_text(encoding="utf-8")
        md += "\n\n---\n\n<details><summary>Предыдущий отчёт</summary>\n\n" + prev + "\n\n</details>\n"
    path.write_text(md, encoding="utf-8")
    with db._lock:
        db._conn.execute("BEGIN IMMEDIATE")
        try:
            db.move_task(tid, "done", status="done", summary=summary, report_path=str(path))
            results.save(result)
            if t["telegram_notify"]:
                prepare_telegram(t, summary)
            db.ex("""UPDATE telegram_deliveries SET state=CASE WHEN error IS NULL THEN 'pending' ELSE 'error' END
                     WHERE task_id=? AND run_key=? AND state='prepared'""", (tid, str(t["last_run_at"] or t["created_at"])))
            db._conn.execute("COMMIT")
        except BaseException:
            db._conn.execute("ROLLBACK")
            raise
    task_event(tid, "finish", summary)
    changed(t["project_id"])


def answer_question(qid, answer):
    with orch.lock:  # не пересекаться с разбором итога хода в orch._done
        return _answer_question(qid, answer)


def _answer_question(qid, answer):
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
        finish_task(tid, t["summary"] or "Задача отмечена выполненной вручную.",
                    "Задача перенесена в «Готово» вручную. Автоматические проверки при этом не запускались.")
    elif kind == "backlog" and t["status"] not in ("running",):
        db.update_task(tid, status="idle")
    if kind in ("todo", "done", "backlog"):
        db.ex("UPDATE questions SET status='cancelled' WHERE task_id=? AND status='open' AND kind='question'", (tid,))
        hub.send("questions")


# ---------------- чат ----------------
CHAT_TITLE_LEN = 50


def chat_title(text, attachments=None):
    """Название чата из первого сообщения пользователя: первая строка, не длиннее CHAT_TITLE_LEN."""
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    line = line or (attachments[0]["name"] if attachments else "") or "Новый чат"
    return line if len(line) <= CHAT_TITLE_LEN else line[:CHAT_TITLE_LEN - 1].rstrip() + "…"


class Chat:
    """Чаты проекта: у каждого своя сессия Claude, отвечать одновременно могут разные чаты, но не два ответа в одном."""

    def __init__(self):
        self.runs = {}  # chat_id -> ClaudeRun

    def running(self, cid):
        return cid in self.runs

    def info(self, c):
        c = db.agent_row(c)
        return {**c, "running": c["id"] in self.runs, "titling": titler.busy(c["id"])}

    def create(self, pid):
        """Новый чат. Пустой безымянный чат проекта не плодим — отдаём его."""
        empty = db.one("""SELECT id FROM chats c WHERE project_id=? AND title IS NULL
                          AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.chat_id=c.id)""", (pid,))
        cid = empty["id"] if empty else db.create_chat(pid)
        if empty:
            db.ex("UPDATE chats SET updated_at=? WHERE id=?", (time.time(), cid))
        hub.send("chats", project_id=pid, chat_id=cid)
        return self.info(db.get_chat(cid))

    def rename(self, cid, title):
        c = db.get_chat(cid)
        db.ex("UPDATE chats SET title=?, title_kind='manual' WHERE id=?", (title, cid))
        hub.send("chats", project_id=c["project_id"], chat_id=cid)

    def retitle(self, cid):
        """«Придумать название заново»: ручное название тоже отдаём модели. False — в чате ещё нет ответа."""
        if not titler.dialog(cid):
            return False
        db.ex("UPDATE chats SET title_kind=NULL WHERE id=?", (cid,))
        titler.request(cid)
        return True

    def send(self, cid, text, attachments=None, telegram_notify=False):
        """attachments — уже сохранённые файлы: [{name, file, size, type}] в db.chat_dir(pid, cid)."""
        if cid in self.runs or restart.since:
            return False
        c = db.get_chat(cid)
        pid = c["project_id"]
        p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        cwd = p["path"] if Path(p["path"]).is_dir() else str(db.ROOT)
        if not c["title"]:
            db.ex("UPDATE chats SET title=? WHERE id=?", (chat_title(text, attachments), cid))
        self._msg(cid, pid, "user", text, attachments)
        hub.send("chats", project_id=pid, chat_id=cid)
        folder = db.chat_dir(pid, cid)
        prompt = text
        images = []
        if attachments:
            prompt = (text or "Пользователь прислал вложения без текста — посмотри их.")
            prompt += "\n\nВложения (прочитай файлы доступными инструментами):\n"
            prompt += "\n".join(f"- {folder / a['file']}" for a in attachments)
        if AGENT == "codex":
            images = [str(folder / a["file"]) for a in (attachments or []) if a.get("type", "") in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp")]
        resume = bool(c["session_id"])
        previous = c.get("last_provider") or ("claude" if db.one("SELECT session_id FROM chats WHERE id=?", (cid,))["session_id"] else None)
        if previous and previous != AGENT:
            history = []
            for m in db.chat_messages(cid):
                files = "\n".join(str(folder / a["file"]) for a in m["attachments"])
                history.append(m["role"] + ": " + (m["text"] or "") + ("\nФайлы: " + files if files else ""))
            prompt = "Контекст чата с другим исполнителем:\n" + "\n\n".join(history) + "\n\nТекущее сообщение:\n" + prompt
        prompt += ("\n\nНастройка устройства для НОВЫХ задач из этого сообщения: telegram_notify="
                   + ("true" if telegram_notify else "false")
                   + ". Передавай её в create_task; явная просьба о Telegram важнее. Флаги старых задач не меняй без просьбы.")
        sid = c["session_id"] or (str(uuid.uuid4()) if AGENT == "claude" else None)
        db.set_chat_session(cid, sid)
        s = db.get_settings()
        run = agent_run(
            key=f"chat-{pid}-{cid}", cwd=cwd, prompt=prompt, session_id=sid, resume=resume, role="chat",
            system_prompt=CHAT_SYSTEM.format(name=p["name"], id=pid, path=p["path"]) + ("\n" + CODEX_SYSTEM if AGENT == "codex" else ""), project_id=pid,
            model=s["chat_model"] or s["model"] or None, permission_mode=s["permission_mode"],
            log_path=db.DATA / "runs" / f"chat-{pid}-{cid}.jsonl", add_dirs=[str(folder)] if attachments else None,
            on_text=lambda t: self._msg(cid, pid, "assistant", t),
            on_tool=lambda n, i: None if n in QUIET_TOOLS else self._msg(cid, pid, "tool", short_tool(n.replace("mcp__tracker__", "трекер."), i)),
            on_done=lambda r: self._done(cid, pid, r),
            on_session=lambda sid: db.set_chat_session(cid, sid), images=images,
            on_rate_limit=lambda info: orch._rate_limited(info),
        )
        self.runs[cid] = run
        hub.send("chat_state", project_id=pid, chat_id=cid, running=True)
        try:
            run.start()
        except Exception as e:
            self.runs.pop(cid, None)
            self._msg(cid, pid, "error", f"Не удалось запустить {NAME}: {e}")
            hub.send("chat_state", project_id=pid, chat_id=cid, running=False)
        return True

    def _msg(self, cid, pid, role, text, attachments=None):
        mid = db.add_chat(cid, role, text, attachments)
        if mid:  # None — чат удалили, пока шёл ответ
            hub.send("chat", project_id=pid, chat_id=cid, message={"id": mid, "role": role, "text": text,
                                                                   "ts": time.time(), "attachments": attachments or []})

    def _done(self, cid, pid, r):
        self.runs.pop(cid, None)
        if not r.get("is_error") and not r.get("killed"):
            db.ex("UPDATE chats SET last_provider=? WHERE id=?", (AGENT, cid))
        if r.get("is_error") and not r.get("killed"):
            err = str(r.get("result"))
            if re.search(r"No conversation found|thread.*not found|no rollout found", err, re.I):
                db.set_chat_session(cid, None)
                err += "\n(сессия чата сброшена, отправь сообщение ещё раз)"
            self._msg(cid, pid, "error", err[:2000])
        hub.send("chat_state", project_id=pid, chat_id=cid, running=False)
        c = db.get_chat(cid)
        if c and c["title_kind"] is None:  # пока модель не придумала название, пробуем после каждого ответа
            titler.request(cid)

    def stop(self, cid):
        run = self.runs.get(cid)
        if run:
            run.kill()

    def stop_project(self, pid):
        for run in list(self.runs.values()):
            if run.project_id == pid:
                run.kill()

    def delete(self, cid):
        """Удалить чат: его сообщения и вложения. Идущий ответ прерывается и уже никуда не пишется."""
        c = db.get_chat(cid)
        self.stop(cid)
        db.ex("DELETE FROM chat_messages WHERE chat_id=?", (cid,))
        db.ex("DELETE FROM chats WHERE id=?", (cid,))
        shutil.rmtree(db.chat_dir(c["project_id"], cid), ignore_errors=True)
        hub.send("chats", project_id=c["project_id"], chat_id=cid, deleted=True)


def drop_uploads(pid):
    """Вложения всех чатов проекта больше не нужны: проект удалён."""
    shutil.rmtree(db.UPLOADS / str(pid), ignore_errors=True)


chat = Chat()


# ---------------- автоназвания чатов ----------------
TITLE_SYSTEM = """Ты придумываешь название для чата по началу разговора, как заголовок сессии.
Название — 2–6 слов на языке разговора, о сути разговора. Не отвечай на сообщения и ничего не выполняй.
Ответь только названием: одной строкой, без кавычек и без точки в конце."""
TITLE_INPUT = 2000    # столько символов начала разговора уходит модели
TITLE_MSG = 700       # и не больше стольких от одного сообщения
TITLE_TIMEOUT = 90
TITLE_LEN = 80
OLD_TITLE = re.compile(r"Чат от \d\d\.\d\d\.\d{4}")  # название перенесённой старой переписки


class Titler:
    """Названия чатов от модели (как у сессий Claude Code): фоновая очередь, по одному claude -p за раз.
    Пока названия нет, в chats.title временное — обрезанное первое сообщение (chat_title)."""

    def __init__(self):
        self.qu = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()

    def busy(self, cid):
        return cid in self.pending

    def request(self, cid):
        with self.lock:
            if cid in self.pending:
                return
            self.pending.add(cid)
        self.qu.put(cid)
        self._changed(cid)

    def loop(self):
        self._backfill()
        while True:
            cid = self.qu.get()
            try:
                self._make(cid)
            except Exception as e:  # лимит, нет сети, таймаут — остаётся текущее название
                log_title(f"чат {cid}: не удалось придумать название: {str(e)[:300]}")
            finally:
                with self.lock:
                    self.pending.discard(cid)
                self._changed(cid)

    def _changed(self, cid):
        c = db.get_chat(cid)
        if c:
            hub.send("chats", project_id=c["project_id"], chat_id=cid)

    def _backfill(self):
        """Один раз для чатов до автоназваний: переименованные руками помечаем manual, остальным — название от модели."""
        for c in db.q("SELECT id, title, title_kind FROM chats WHERE title_kind IS NULL OR title_kind='legacy'"):
            if c["title_kind"] == "legacy":
                first = db.one("SELECT text, attachments FROM chat_messages WHERE chat_id=? AND role='user' ORDER BY id LIMIT 1",
                               (c["id"],))
                temp = first and c["title"] == chat_title(first["text"], json.loads(first["attachments"] or "null"))
                kind = None if temp or OLD_TITLE.fullmatch(c["title"] or "") else "manual"
                db.ex("UPDATE chats SET title_kind=? WHERE id=?", (kind, c["id"]))
                if kind:
                    continue
            if self.dialog(c["id"]):
                self.request(c["id"])

    def dialog(self, cid):
        """Начало разговора для модели. Пусто — Claude ещё не ответил, называть рано."""
        rows = db.q("""SELECT role, text, attachments FROM chat_messages WHERE chat_id=? AND role IN ('user', 'assistant')
                       ORDER BY id LIMIT 12""", (cid,))
        if not any(r["role"] == "assistant" for r in rows):
            return ""
        out = []
        for r in rows:
            text = (r["text"] or "").strip()
            if not text and r["attachments"]:
                text = "[вложения: " + ", ".join(a["name"] for a in json.loads(r["attachments"])) + "]"
            if len(text) > TITLE_MSG:
                text = text[:TITLE_MSG] + "…"
            out.append(("Пользователь: " if r["role"] == "user" else "Ассистент: ") + text)
        return "\n\n".join(out)[:TITLE_INPUT]

    def _make(self, cid):
        c = db.get_chat(cid)
        text = c and c["title_kind"] is None and self.dialog(cid)
        if not text:
            return
        title = self.generate(text)
        # пока модель думала, чат могли переименовать руками — его название не трогаем
        db.ex("UPDATE chats SET title=?, title_kind='auto' WHERE id=? AND title_kind IS NULL", (title, cid))

    def generate(self, text):
        """Отдельный claude -p без сессии, MCP, инструментов и настроек (без CLAUDE.md и хуков) на дешёвой модели."""
        if AGENT == "codex":
            from codex_runner import generate
            text = generate(f"Начало разговора:\n\n{text}", TITLE_SYSTEM, db.get_setting("chat_model") or db.get_setting("model") or None, timeout=TITLE_TIMEOUT)
            line = next((l for l in text.splitlines() if l.strip()), "")
            title = re.sub(r"\s+", " ", line).strip().strip('"\'`«»*#').rstrip(".").strip()
            if not title:
                raise RuntimeError("Codex вернул пустое название")
            return title[:TITLE_LEN]
        cmd = [CLAUDE, "-p", "--output-format", "json", "--strict-mcp-config", "--no-session-persistence",
               "--model", "haiku", "--tools", "", "--setting-sources", "", "--system-prompt", TITLE_SYSTEM]
        r = subprocess.run(cmd, input=f"Начало разговора:\n\n{text}".encode("utf-8"), capture_output=True,
                           cwd=tempfile.gettempdir(), timeout=TITLE_TIMEOUT, creationflags=NO_WINDOW)
        try:
            out = json.loads(r.stdout.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            out = {}
        if out.get("is_error") or not out.get("result"):
            raise RuntimeError(out.get("result") or r.stderr.decode("utf-8", errors="replace").strip() or f"код {r.returncode}")
        line = next((l for l in out["result"].splitlines() if l.strip()), "")
        title = re.sub(r"\s+", " ", line).strip().strip("\"'`«»„“”*#").rstrip(".").strip()
        if not title:
            raise RuntimeError(f"пустое название: {out['result'][:100]!r}")
        return title if len(title) <= TITLE_LEN else title[:TITLE_LEN - 1].rstrip() + "…"


def log_title(text):
    try:
        print(f"[title] {text}")
    except (UnicodeEncodeError, OSError):
        pass


titler = Titler()


# ---------------- перезапуск сервера ----------------
RESTART_TIMEOUT = 10 * 60  # дольше выполняющиеся запуски не ждём: прервём, после старта их подхватит recover()


def log_restart(text):
    with open(db.DATA / "restart.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%d.%m.%Y %H:%M:%S} [{BOOT_ID[:8]}] {text}\n")
    try:
        print(f"[restart] {text}")
    except (UnicodeEncodeError, OSError):  # консоль не в UTF-8 или её нет — в журнал уже записано
        pass


class Restart:
    """Отложенный перезапуск: ждём, пока не останется запусков задач и чата, новых не берём."""

    def __init__(self):
        self.since = None
        self.exiting = False
        self.exit = None  # main.py: запустить новый экземпляр и завершить этот

    def info(self):
        return {"since": self.since, "busy": len(active_runs())} if self.since else None

    def request(self, by):
        if not self.since:
            self.since = time.time()
            log_restart(f"запрошен ({by}), выполняется запусков: {len(orch.runs)}, чатов: {len(chat.runs)}")
        hub.send("state", state=orch.state())

    def check(self):
        if not self.since or self.exiting:
            return
        busy = active_runs
        if busy() and time.time() - self.since < RESTART_TIMEOUT:
            return
        if busy():
            log_restart(f"не дождались за {RESTART_TIMEOUT // 60} мин, прерываю запусков: {len(busy())}")
            for run in busy():
                run.kill()
            for _ in range(30):  # даём разобрать итоги: задачи уйдут в resume
                if not busy():
                    break
                time.sleep(0.5)
        hub.send("restarting")
        time.sleep(0.5)  # чтобы событие успело уйти в браузер
        self.exiting = True
        try:
            self.exit()
        except Exception as e:  # новый экземпляр не запустился — работаем дальше
            log_restart(f"не удалось перезапуститься: {e}")
            self.since, self.exiting = None, False
            hub.send("state", state=orch.state())


restart = Restart()


# ---------------- обработчики MCP-инструментов ----------------
def _task_brief(t):
    return {"id": t["id"], "task_id": t["id"], "task_number": t["task_number"],
            "project_id": t["project_id"], "project_name": t["project_name"],
            "title": t["title"], "column": t["column_name"], "status": t["status"],
            "summary": t["summary"], "links": [l["id"] for l in db.task_links(t["id"])]}


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
    if "telegram_notify" in a and not isinstance(a["telegram_notify"], bool):
        raise ValueError("telegram_notify должен быть логическим значением")
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
        finish_task(tid, a["summary"], a["report"], a.get("result_text", a.get("telegram_text")), a.get("result_files", a.get("telegram_files")), a.get("telegram_notify"))
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
        links = [int(x) for x in a.get("links") or []]
        for x in links:
            if not db.get_task(x):
                raise ValueError(f"Нет задачи с task_id={x} для связи")
        new = db.create_task(target, a["title"], a.get("description", ""), c["id"], telegram_notify=a.get("telegram_notify", False))
        for x in links:
            db.link_tasks(new, x)
        if c["kind"] == "todo":
            db.update_task(new, status="queued")
        if tid:
            task_event(tid, "progress", f"Создана задача {task_label(db.get_task(new))}: {a['title']}")
        changed_links(target, links)
        return J({**_task_brief(db.get_task(new)), "message": "Задача создана"})
    if tool == "list_projects":
        return J(db.q("SELECT id, name, path, active FROM projects ORDER BY id"))
    if tool == "list_tasks":
        target = int(a.get("project_id") or pid)
        rows = [db.get_task(r["id"]) for r in db.q("SELECT id FROM tasks WHERE project_id=? ORDER BY column_id, position", (target,))]
        if a.get("task_number") is not None:
            rows = [r for r in rows if r["task_number"] == int(a["task_number"])]
        if a.get("column"):
            c = _col(target, a["column"])
            rows = [r for r in rows if r["column_id"] == c["id"]]
        return J([_task_brief(r) for r in rows]) if rows else "Задач нет"
    if tool == "get_task":
        t = db.get_task(int(a["task_id"]))
        if not t:
            return "Нет такой задачи"
        out = {k: t[k] for k in ("id", "task_id", "task_number", "project_id", "title", "description", "column_name", "status", "summary", "error", "project_name", "telegram_notify")}
        out["telegram_delivery"] = telegram_delivery(t["id"])
        if t["report_path"] and Path(t["report_path"]).exists():
            out["report"] = Path(t["report_path"]).read_text(encoding="utf-8")[:6000]
        out["events"] = [f"{e['kind']}: {e['text'][:300]}" for e in db.task_events(t["id"], limit=30)]
        out["questions"] = db.q("SELECT id, text, answer, status FROM questions WHERE task_id=?", (t["id"],))
        out["links"] = [link_info(l) for l in db.task_links(t["id"])]
        return J(out)
    if tool == "update_task":
        t = db.get_task(int(a["task_id"]))
        if not t:
            return "Нет такой задачи"
        add, drop = [int(x) for x in a.get("links") or []], [int(x) for x in a.get("unlink") or []]
        for x in add:
            db.link_tasks(t["id"], x)
        for x in drop:
            db.unlink_tasks(t["id"], x)
        db.update_task(t["id"], **{k: a[k] for k in ("title", "description", "telegram_notify") if a.get(k) is not None})
        updated = db.get_task(t["id"])
        if updated["telegram_notify"] and updated["status"] == "done" and updated["summary"] and not telegram_delivery(t["id"]):
            prepare_telegram(updated, updated["summary"])
        changed_links(t["project_id"], add + drop)
        return "Обновлено" + (". Связи: " + (", ".join(task_label(l) + f" (task_id={l['id']})" for l in db.task_links(t["id"])) or "нет")
                              if add or drop else "")
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
        return J([{"question_id": r["id"], "task_id": r["task_id"], "task_number": r["task_number"], "project_id": r["project_id"],
                   "task": f"{task_label(r)} {r['task_title']}", "project": r["project_name"],
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
    if tool == "restart_tracker":
        restart.request(f"задача {task_label(db.get_task(tid))} (task_id={tid})" if tid else f"чат проекта {pid}")
        if tid:
            task_event(tid, "system", "Запрошен перезапуск трекера")
        return ("Перезапуск запрошен: трекер перезапустится, когда завершатся все запуски агентов и чата, включая твой "
                f"(не дольше {RESTART_TIMEOUT // 60} мин). Больше ничего не делай, заверши ход.")
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
        "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "telegram_notify": {"type": "boolean"}},
        "required": ["title", "description", "telegram_notify"]}}},
    "required": ["tasks"],
}

IMPORT_PROMPT = """Ниже текст, скопированный из другого трекера, заметок или переписки. Разбери его на отдельные задачи для канбан-доски.
Для каждой задачи: короткий понятный заголовок (до 80 символов) и описание, где сохранено всё полезное из исходника: детали, шаги воспроизведения, критерии, ссылки, номера тикетов. Ничего не выдумывай. Если задача в тексте одна — верни одну. Файлы и инструменты не используй, просто разбери текст. Пиши по-русски (термины и цитаты оставляй как есть).
telegram_notify=true только для задач, для которых в исходнике явно просят прислать результат в Telegram/ТГ; иначе false. Просьбу сохрани также в описании.

---
{text}"""


def import_parse(pid, text):
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    cwd = p["path"] if Path(p["path"]).is_dir() else str(db.ROOT)
    s = db.get_settings()
    if AGENT == "codex":
        from codex_runner import generate
        schema = json.loads(json.dumps(IMPORT_SCHEMA))
        schema["additionalProperties"] = False
        schema["properties"]["tasks"]["items"]["additionalProperties"] = False
        return json.loads(generate(IMPORT_PROMPT.format(text=text), "Разбирай только переданный текст. Не используй инструменты.",
                                   s["chat_model"] or s["model"] or None, schema)).get("tasks", [])
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
