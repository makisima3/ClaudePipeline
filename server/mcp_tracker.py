"""MCP-сервер (stdio) трекера. Запускается самим Claude Code из --mcp-config.

Тонкая прослойка: список инструментов зависит от роли (task / chat), а вся логика
выполняется на основном сервере через POST /api/mcp/call.
"""
import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("PIPELINE_URL", "http://127.0.0.1:8765")
ROLE = os.environ.get("PIPELINE_ROLE", "task")
TASK_ID = os.environ.get("PIPELINE_TASK_ID")
PROJECT_ID = os.environ.get("PIPELINE_PROJECT_ID")


def S(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


STR = {"type": "string"}
INT = {"type": "integer"}
IDS = {"type": "array", "items": INT}

APPROVE = {
    "name": "approve",
    "description": "Служебный: запрос разрешения на действие у пользователя (вызывается Claude Code автоматически).",
    "inputSchema": S({"tool_name": STR, "input": {"type": "object"}, "tool_use_id": STR}, ["tool_name"]),
}
CREATE_TASK = {
    "name": "create_task",
    "description": "Создать задачу в трекере. column: 'backlog' (по умолчанию), 'todo' (сразу в работу) или имя колонки. "
                   "project_id — если не текущий проект. links — id задач, нужных для контекста: связь видна в обеих "
                   "карточках, а агент получит их итоги и пути к отчётам.",
    "inputSchema": S({"title": STR, "description": STR, "column": STR, "project_id": INT, "links": IDS}, ["title"]),
}
GET_TASK = {
    "name": "get_task",
    "description": "Подробности задачи: описание, статус, итог, отчёт, последние события, вопросы, связанные задачи.",
    "inputSchema": S({"task_id": INT}, ["task_id"]),
}
RESTART = {
    "name": "restart_tracker",
    "description": "Перезапустить сервер трекера, чтобы вступили в силу изменения в его коде (server/). Перезапуск "
                   "отложенный: трекер дождётся завершения всех запусков агентов и чата, включая твой, и новых "
                   "задач до этого не возьмёт. После вызова заверши ход.",
    "inputSchema": S({}),
}

TASK_TOOLS = [
    {
        "name": "ask_user",
        "description": "Задать пользователю вопрос, когда без его решения дальше нельзя (выбор варианта, уточнение, "
                       "разрешение). После вызова СРАЗУ заверши ход коротким сообщением — ответ придёт следующим "
                       "сообщением в этой же сессии. options — варианты ответа (необязательно, пользователь может "
                       "ответить своим текстом).",
        "inputSchema": S({"question": STR, "options": {"type": "array", "items": STR}}, ["question"]),
    },
    {
        "name": "report_progress",
        "description": "Короткая заметка о ходе работы, видна в тикете (например: 'нашёл причину, правлю X').",
        "inputSchema": S({"text": STR}, ["text"]),
    },
    {
        "name": "finish_task",
        "description": "Завершить задачу. summary — 1-3 предложения для карточки. report — подробный отчёт в markdown: "
                       "что сделано и как, изменённые файлы, как проверить, принятые решения, открытые вопросы.",
        "inputSchema": S({"summary": STR, "report": STR}, ["summary", "report"]),
    },
    CREATE_TASK,
    GET_TASK,
    RESTART,
    APPROVE,
]

CHAT_TOOLS = [
    {"name": "list_projects", "description": "Список проектов трекера.", "inputSchema": S({})},
    {
        "name": "list_tasks",
        "description": "Задачи проекта (по умолчанию текущего). column — фильтр по kind или имени колонки.",
        "inputSchema": S({"project_id": INT, "column": STR}),
    },
    GET_TASK,
    CREATE_TASK,
    {
        "name": "update_task",
        "description": "Изменить заголовок/описание задачи. links — связать с задачами (id), unlink — убрать связи.",
        "inputSchema": S({"task_id": INT, "title": STR, "description": STR, "links": IDS, "unlink": IDS}, ["task_id"]),
    },
    {
        "name": "move_task",
        "description": "Переместить задачу в колонку: backlog, todo (в работу), done или имя колонки.",
        "inputSchema": S({"task_id": INT, "column": STR}, ["task_id", "column"]),
    },
    {"name": "list_questions", "description": "Открытые вопросы агентов, ждущие ответа.", "inputSchema": S({})},
    {
        "name": "answer_question",
        "description": "Ответить на вопрос агента от имени пользователя (только если пользователь явно дал ответ).",
        "inputSchema": S({"question_id": INT, "answer": STR}, ["question_id", "answer"]),
    },
    {"name": "pipeline_status", "description": "Состояние пайплайна: включён ли, что выполняется.", "inputSchema": S({})},
    {
        "name": "set_pipeline",
        "description": "Включить/выключить выполнение задач (только по просьбе пользователя).",
        "inputSchema": S({"enabled": {"type": "boolean"}}, ["enabled"]),
    },
    RESTART,
    APPROVE,
]

TOOLS = TASK_TOOLS if ROLE == "task" else CHAT_TOOLS


def call_server(tool, args):
    body = json.dumps({"role": ROLE, "task_id": TASK_ID, "project_id": PROJECT_ID, "tool": tool, "args": args}).encode()
    req = urllib.request.Request(URL + "/api/mcp/call", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def approve(args):
    """Блокирующий запрос разрешения: создаём вопрос и ждём ответа пользователя.
    Возвращает {"text": <JSON с behavior>} — формат, который ждёт Claude Code."""
    res = json.loads(call_server("approve", args)["text"])
    qid = res.pop("question_id", None)
    if not qid:
        return {"text": json.dumps(res, ensure_ascii=False)}
    while True:
        time.sleep(2)
        try:
            st = json.loads(call_server("approve_status", {"question_id": qid})["text"])
        except Exception:
            continue
        if st.pop("done", False):
            return {"text": json.dumps(st, ensure_ascii=False)}


def handle(msg):
    method = msg.get("method")
    if method == "initialize":
        return {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tracker", "version": "1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = msg.get("params", {})
        name, args = p.get("name"), p.get("arguments") or {}
        try:
            res = approve(args) if name == "approve" else call_server(name, args)
            return {"content": [{"type": "text", "text": res.get("text", "")}], "isError": bool(res.get("isError"))}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Ошибка трекера: {e}"}], "isError": True}
    raise KeyError(method)


def main():
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue  # уведомления
        try:
            resp = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except KeyError:
            resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}}
        out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()


if __name__ == "__main__":
    main()
