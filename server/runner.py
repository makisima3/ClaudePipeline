"""Запуск `claude -p` как подпроцесса и разбор его stream-json вывода."""
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from db import DATA

ROOT = Path(__file__).resolve().parent
CLAUDE = shutil.which("claude") or "claude"
PORT = int(os.environ.get("PIPELINE_PORT", "8765"))
SERVER_URL = f"http://127.0.0.1:{PORT}"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EXIT_GRACE = 5  # сек после result: не вышел сам — значит ждёт свои фоновые задачи, гасим


def short_tool(name, inp):
    """Человекочитаемая строка про вызов инструмента."""
    inp = inp or {}
    for key in ("command", "file_path", "path", "pattern", "url", "description", "query", "prompt"):
        if key in inp and isinstance(inp[key], str):
            val = inp[key].replace("\n", " ")
            return f"{name}: {val[:200]}"
    s = json.dumps(inp, ensure_ascii=False)
    return f"{name}: {s[:200]}" if inp else name


class ClaudeRun:
    """Один запуск сессии. Колбэки вызываются из фонового потока:
    on_text(str), on_tool(name, input), on_rate_limit(info), on_done(result_dict).
    on_done вызывается ровно один раз; killed — остановлен через kill(), reaped — погашен после конца хода."""

    def __init__(self, *, key, cwd, prompt, session_id, resume, role, system_prompt,
                 task_id=None, project_id=None, model=None, effort=None, permission_mode="bypassPermissions",
                 log_path=None, add_dirs=None, on_text=None, on_tool=None, on_rate_limit=None, on_done=None):
        self.key = key
        self.cwd = cwd
        self.prompt = prompt
        self.session_id = session_id
        self.resume = resume
        self.role = role
        self.system_prompt = system_prompt
        self.task_id = task_id
        self.project_id = project_id
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.log_path = log_path
        self.add_dirs = add_dirs or []
        self.on_text = on_text or (lambda t: None)
        self.on_tool = on_tool or (lambda n, i: None)
        self.on_rate_limit = on_rate_limit or (lambda info: None)
        self.on_done = on_done or (lambda r: None)
        self.proc = None
        self.killed = False
        self.reaped = False
        self.rate_limited = None
        self.result = None
        self._timer = None
        self._finished = False
        self._lock = threading.Lock()

    def _mcp_config(self):
        cfg_dir = DATA / "runs" / "mcp"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        path = cfg_dir / f"{self.key}.json"
        env = {"PIPELINE_URL": SERVER_URL, "PIPELINE_ROLE": self.role, "PYTHONIOENCODING": "utf-8"}
        if self.task_id is not None:
            env["PIPELINE_TASK_ID"] = str(self.task_id)
        if self.project_id is not None:
            env["PIPELINE_PROJECT_ID"] = str(self.project_id)
        cfg = {"mcpServers": {"tracker": {"command": sys.executable,
                                          "args": ["-u", str(ROOT / "mcp_tracker.py")], "env": env}}}
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return path

    def command(self):
        cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
               "--mcp-config", str(self._mcp_config()),
               "--permission-mode", self.permission_mode,
               "--allowedTools", "mcp__tracker",
               "--append-system-prompt", self.system_prompt]
        if self.permission_mode != "bypassPermissions":
            cmd += ["--permission-prompt-tool", "mcp__tracker__approve"]
        cmd += ["--resume", self.session_id] if self.resume else ["--session-id", self.session_id]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        for d in self.add_dirs:
            cmd += ["--add-dir", d]
        return cmd

    def start(self):
        env = os.environ.copy()
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        env["MCP_TOOL_TIMEOUT"] = str(7 * 24 * 3600 * 1000)  # ожидание разрешения может быть долгим
        self.proc = subprocess.Popen(self.command(), cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        try:
            self.proc.stdin.write(self.prompt.encode("utf-8"))
            self.proc.stdin.close()
        except OSError:
            pass
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def kill(self):
        self.killed = True
        self._kill_tree()

    def _kill_tree(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                self.proc.kill()

    def _arm(self):
        """Ход закончен: ждём нормального выхода EXIT_GRACE секунд."""
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(EXIT_GRACE, self._reap)
        self._timer.daemon = True
        self._timer.start()

    def _reap(self):
        """claude -p не выходит, пока живут его фоновые задачи (серверы, watch), и сам начинает
        по ним новые ходы. Ход уже закончен и результат есть — гасим всё дерево."""
        if self._finished:
            return
        if self.proc.poll() is None:
            self.reaped = True
            self._kill_tree()
        # если stdout унаследовал кто-то вне дерева, EOF не придёт — не ждём его вечно
        t = threading.Timer(EXIT_GRACE, self._finish)
        t.daemon = True
        t.start()

    def _finish(self):
        with self._lock:
            if self._finished:
                return
            self._finished = True
        if self._timer:
            self._timer.cancel()
        result = dict(self.result)
        result["killed"] = self.killed
        result["reaped"] = self.reaped
        result["rate_limited"] = self.rate_limited
        self.on_done(result)

    def _read(self):
        log = open(self.log_path, "a", encoding="utf-8") if self.log_path else None
        if log:  # что ушло агенту на вход: в stream-json самого промпта нет
            log.write(json.dumps({"type": "pipeline_prompt", "resume": self.resume, "prompt": self.prompt},
                                 ensure_ascii=False) + "\n")
        stderr_chunks = []
        threading.Thread(target=lambda: stderr_chunks.append(self.proc.stderr.read()), daemon=True).start()
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if log:
                    log.write(line + "\n")
                    log.flush()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    self.result = self._handle(ev) or self.result
                except Exception as e:  # колбэки не должны ронять чтение
                    print(f"[runner] callback error: {e}", file=sys.stderr)
        finally:
            self.proc.wait()
            if log:
                log.close()
        if self.result is None:
            err = b"".join(c for c in stderr_chunks if c).decode("utf-8", errors="replace").strip()
            self.result = {"is_error": True, "result": err or f"Процесс завершился с кодом {self.proc.returncode}"}
        self._finish()

    def _handle(self, ev):
        t = ev.get("type")
        if t == "assistant" and not ev.get("parent_tool_use_id"):
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.on_text(block["text"])
                elif block.get("type") == "tool_use":
                    self.on_tool(block.get("name", "?"), block.get("input"))
        elif t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if info.get("status") == "rejected":
                self.rate_limited = info
                self.on_rate_limit(info)
        elif t == "system" and ev.get("subtype") == "init" and self._timer:
            self._timer.cancel()  # фоновая задача завершилась, и claude успел начать новый ход — даём доделать
            self._timer = None
        elif t == "result":
            self._arm()
            return ev
        return None
