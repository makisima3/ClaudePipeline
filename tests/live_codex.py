"""Явный сквозной тест по подписке: python tests/live_codex.py. Claude не запускается."""
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path


def png():
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 24, 24, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 24) * 24)) + chunk(b"IEND", b"")


def main():
    source = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="pipeline-live-codex-"))
    for folder in ("server", "web"):
        shutil.copytree(source / folder, root / folder)
    for file in ("start_codex.cmd", "start_claude.cmd"):
        shutil.copy2(source / file, root / file)
    project = root / "project"
    project.mkdir()
    # Явно ограничиваем работу модельного теста его временной папкой.
    (project / "AGENTS.md").write_text("Работай только над явным тестовым заданием в этой папке. Не читай личную память, не отправляй сообщения и не запускай другие проекты. Git выключен. Используй инструменты MCP tracker для вопросов и отчётов.\n", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(PIPELINE_PORT=str(port), PIPELINE_AGENT="codex", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    setup = "import sys;sys.path.insert(0,'server');import db;db.set_setting('update_check_minutes','0');db.set_setting('git_mode','off');db.set_setting('pipeline_enabled','0')"
    subprocess.run([sys.executable, "-c", setup], cwd=root, env=env, check=True)
    output = open(root / "server-output.log", "wb")
    command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "start_codex.cmd", "--no-browser"] if os.name == "nt" else [sys.executable, "server/main.py", "--agent", "codex", "--no-browser"]
    process = subprocess.Popen(command, cwd=root, env=env, stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    url = f"http://127.0.0.1:{port}"
    evidence = {"workspace": str(root), "port": port, "checks": []}
    print("WORKSPACE " + str(root), flush=True)

    def api(method, path, data=None):
        req = urllib.request.Request(url + path, data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)

    def wait(check, seconds=180):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                last = check()
                if last:
                    return last
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        raise AssertionError("Timeout; last=" + str(last))

    def passed(label):
        evidence["checks"].append(label)
        print("PASS " + label, flush=True)

    try:
        state = wait(lambda: api("GET", "/api/state"), 60)
        assert state["agent"] == "codex" and state["agent_info"]["models"]
        evidence["version"] = state["agent_info"]["version"]
        passed("start_codex.cmd + ChatGPT auth + models")
        pid = api("POST", "/api/projects", {"name": "Codex integration", "path": str(project)})["id"]
        desc = "Это короткий интеграционный тест. Создай result.txt со строкой pipeline-ok. Затем ОБЯЗАТЕЛЬНО вызови MCP tracker ask_user с вопросом 'Проверка продолжения?' и заверши ход. Пока не получишь ответ, finish_task не вызывай. Не делай других действий."
        task = api("POST", f"/api/projects/{pid}/tasks", {"title": "Integration question", "description": desc})
        tid = task["id"]
        board = api("GET", f"/api/projects/{pid}/board")
        todo = next(c["id"] for c in board["columns"] if c["kind"] == "todo")
        api("POST", f"/api/tasks/{tid}/move", {"column_id": todo})
        api("POST", "/api/pipeline", {"enabled": True})
        def waiting():
            t = api("GET", f"/api/tasks/{tid}")
            if t["status"] == "error":
                raise AssertionError(t["error"])
            return t if t["status"] == "waiting" and any(q["status"] == "open" for q in t["questions"]) else None
        task = wait(waiting)
        sid = task["session_id"]
        assert sid and "pipeline-ok" in (project / "result.txt").read_text(encoding="utf-8")
        qid = next(q["id"] for q in task["questions"] if q["status"] == "open")
        passed("real task + command + file + tracker MCP ask_user")
        api("POST", f"/api/questions/{qid}/answer", {"answer": "Да. Допиши строку resume-ok в result.txt и обязательно вызови MCP tracker finish_task с итогом и отчётом."})
        def done():
            t = api("GET", f"/api/tasks/{tid}")
            if t["status"] == "error":
                raise AssertionError(t["error"])
            return t if t["status"] == "done" and not t["running"] else None
        task = wait(done)
        assert task["session_id"] == sid
        assert "resume-ok" in (project / "result.txt").read_text(encoding="utf-8")
        assert Path(task["report_path"]).is_file()
        with urllib.request.urlopen(url + f"/report/{tid}") as response:
            assert b"<!DOCTYPE html>" in response.read() or response.status == 200
        passed("real same-session answer + finish_task + report")
        api("POST", f"/api/tasks/{tid}/comment", {"text": "Допиши строку rerun-ok в result.txt и заверши через finish_task. Больше вопросов не задавай."})
        api("POST", f"/api/tasks/{tid}/move", {"column_id": todo})
        task = wait(done)
        assert task["session_id"] == sid and "rerun-ok" in (project / "result.txt").read_text(encoding="utf-8")
        passed("real completed task rerun with preserved session")
        api("POST", "/api/pipeline", {"enabled": False})
        cid = api("POST", f"/api/projects/{pid}/chats", {})["id"]
        files = [{"name": "marker.txt", "type": "text/plain", "data": base64.b64encode(b"attachment-ok").decode()},
                 {"name": "red.png", "type": "image/png", "data": base64.b64encode(png()).decode()}]
        api("POST", f"/api/chats/{cid}/messages", {"text": "Прочитай вложенный marker.txt и определи основной цвет картинки. Ответь кратко: содержимое файла и цвет по-русски. Доску не меняй.", "attachments": files})
        def chat_done():
            c = api("GET", f"/api/chats/{cid}/messages")
            if any(m["role"] == "error" for m in c["messages"]):
                raise AssertionError(str(c["messages"]))
            return c if not c["running"] and any(m["role"] == "assistant" for m in c["messages"]) else None
        conversation = wait(chat_done)
        text = " ".join(m["text"] for m in conversation["messages"] if m["role"] == "assistant")
        evidence["image_response"] = text
        assert "attachment-ok" in text and "красн" in text.lower(), ascii(text)
        passed("real chat + text attachment + image recognition")
        wait(lambda: api("GET", f"/api/chats/{cid}/messages")["chat"]["title_kind"] == "auto", 120)
        passed("real automatic chat title")
        imported = api("POST", f"/api/projects/{pid}/import", {"text": "Две отдельные задачи: 1. Исправить кнопку входа. 2. Добавить поиск по названию."})
        assert len(imported["tasks"]) == 2, imported
        passed("real structured import")
        boot = api("GET", "/api/state")["boot_id"]
        api("POST", "/api/restart", {})
        new_state = wait(lambda: (s if (s := api("GET", "/api/state"))["boot_id"] != boot else None), 90)
        assert new_state["agent"] == "codex"
        assert api("GET", f"/api/tasks/{tid}")["session_id"] == sid
        passed("real server restart + provider and session persistence")
        evidence["success"] = True
    finally:
        log = root / "data/restart.log"
        successor = None
        if log.exists():
            matches = re.findall(r"новый экземпляр запущен \(pid (\d+)\)", log.read_text(encoding="utf-8"))
            if matches:
                successor = int(matches[-1])
        targets = [process.pid] + ([successor] if successor else [])
        if os.name == "nt":
            for pid in targets:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        process.wait(timeout=15)
        output.close()
        (root / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("EVIDENCE " + str(root / "evidence.json"), flush=True)


if __name__ == "__main__":
    main()
