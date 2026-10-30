"""Codex app-server по stdio: сессии, события, разрешения и подписка ChatGPT."""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from runner import ClaudeRun, NO_WINDOW, ROOT, SERVER_URL

MIN_VERSION = (0, 166, 0)
INFO = {"models": [], "version": None}
ACTIVE = set()
ACTIVE_LOCK = threading.RLock()


def active_runs():
    with ACTIVE_LOCK:
        return list(ACTIVE)


def executable():
    path = shutil.which("codex")
    if not path:
        raise RuntimeError("Не найден Codex CLI. Установи его по https://learn.chatgpt.com/docs/codex/cli и выполни codex login.")
    p = Path(path)
    if p.suffix.lower() in (".cmd", ".bat", ".ps1"):
        package = p.parent / "node_modules" / "@openai" / "codex"
        binaries = sorted(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe"))
        if binaries:
            return [str(binaries[0])]
        script = package / "bin" / "codex.js"
        if script.exists() and shutil.which("node"):
            return [shutil.which("node"), str(script)]
        raise RuntimeError("Не найден исполняемый файл Codex CLI. Переустанови Codex CLI; запуск npm-обёртки через shell не используется.")
    return [path]


def toml(value):
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k) + " = " + toml(v) for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


class Rpc:
    """Читатель не блокируется ожиданием ответа пользователя на запрос разрешения."""
    def __init__(self, cwd, config=None, event=None, request=None, log=None):
        cmd = executable() + ["app-server"]
        config = {"forced_login_method": "chatgpt", "model_provider": "openai", **(config or {})}
        for k, v in config.items():
            cmd += ["-c", k + "=" + toml(v)]
        self.event = event or (lambda m, p: None)
        self.request = request
        self.log = log
        self.pending = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.stderr = bytearray()
        env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(key, None)
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW,
                                     start_new_session=os.name != "nt")
        self.readers = [threading.Thread(target=self._errors, daemon=True), threading.Thread(target=self._read, daemon=True)]
        for reader in self.readers:
            reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "claudepipeline", "version": "1.0"},
                                     "capabilities": {"experimentalApi": True}}, timeout=30)
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def send(self, obj):
        with self.lock:
            self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def call(self, method, params=None, timeout=60):
        with self.lock:
            rid = str(time.monotonic_ns())
            qu = queue.Queue()
            self.pending[rid] = qu
        try:
            self.send({"id": rid, "method": method, "params": params or {}})
            try:
                msg = qu.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError(f"Codex не ответил на {method} за {timeout} с")
            if "error" in msg:
                raise RuntimeError(f"Codex {method}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result") or {}
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def _errors(self):
        with self.proc.stderr:
            for part in iter(lambda: self.proc.stderr.read(4096), b""):
                self.stderr.extend(part)
                del self.stderr[:-16000]

    def _answer(self, msg):
        try:
            if self.request:
                result = self.request(msg["method"], msg.get("params") or {})
                self.send({"id": msg["id"], "result": result})
            else:
                self.send({"id": msg["id"], "error": {"code": -32601, "message": "Запрос не поддерживается"}})
        except Exception as e:
            if not self.closed.is_set():
                try:
                    self.send({"id": msg["id"], "error": {"code": -32603, "message": str(e)}})
                except OSError:
                    pass

    def _read(self):
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "method" in msg:
                    if self.log:
                        self.log(msg)
                    if "id" in msg:
                        threading.Thread(target=self._answer, args=(msg,), daemon=True).start()
                    else:
                        self.event(msg["method"], msg.get("params") or {})
                else:
                    with self.lock:
                        qu = self.pending.get(str(msg.get("id")))
                    if qu:
                        qu.put(msg)
        finally:
            self.proc.stdout.close()
            self.closed.set()
            with self.lock:
                for qu in self.pending.values():
                    qu.put({"error": {"message": "Процесс Codex завершился: " + self.stderr.decode("utf-8", "replace")[-2000:]}})

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                kill_tree(self.proc)
        for reader in self.readers:
            if reader is not threading.current_thread():
                reader.join(timeout=2)


def kill_tree(proc):
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=10)


def preflight():
    version = subprocess.run(executable() + ["--version"], capture_output=True, timeout=15,
                             creationflags=NO_WINDOW).stdout.decode("utf-8", "replace").strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < MIN_VERSION:
        raise RuntimeError("Обнови Codex CLI до 0.166.0 или новее. Установлено: " + version)
    rpc = Rpc(tempfile.gettempdir())
    try:
        account = rpc.call("account/read").get("account") or {}
        if account.get("type") != "chatgpt":
            raise RuntimeError("Для работы по подписке выполни codex login и войди через ChatGPT.")
        models, cursor = [], None
        while True:
            result = rpc.call("model/list", {"cursor": cursor, "limit": 100})
            models += result.get("data", [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        INFO.update(version=version, models=models)
    finally:
        rpc.close()


class CodexRun(ClaudeRun):
    def __init__(self, *, on_session=None, images=None, output_schema=None, ephemeral=False, **kwargs):
        super().__init__(**kwargs)
        self.on_session = on_session or (lambda sid: None)
        self.images = images or []
        self.output_schema = output_schema
        self.ephemeral = ephemeral
        self.completed = threading.Event()
        self.rpc = None
        self.messages = []
        self.limits = None
        self.turn_id = None
        self.finished = threading.Event()
        self.log_lock = threading.Lock()
        self.log = None

    def start(self):
        with ACTIVE_LOCK:
            ACTIVE.add(self)
        threading.Thread(target=self._work, daemon=True).start()
        return self

    def _write(self, obj):
        with self.log_lock:
            if self.log:
                self.log.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.log.flush()

    def _config(self):
        env = {"PIPELINE_URL": SERVER_URL, "PIPELINE_ROLE": self.role, "PYTHONIOENCODING": "utf-8"}
        if self.task_id is not None:
            env["PIPELINE_TASK_ID"] = str(self.task_id)
        if self.project_id is not None:
            env["PIPELINE_PROJECT_ID"] = str(self.project_id)
        cfg = {"mcp_servers.tracker": {"command": sys.executable, "args": ["-u", str(ROOT / "mcp_tracker.py")],
                                        "env": env, "tool_timeout_sec": 604800},
               "approval_policy": "never" if self.permission_mode == "bypassPermissions" else "on-request",
               "approvals_reviewer": "user"}
        if self.ephemeral:
            cfg.pop("mcp_servers.tracker")
            cfg.update({"mcp_servers": {}, "features.apps": False, "features.shell_tool": False,
                        "web_search": "disabled", "approval_policy": "never"})
        return cfg

    def _work(self):
        try:
            if self.log_path:
                self.log = open(self.log_path, "a", encoding="utf-8")
            self._write({"type": "pipeline_prompt", "agent": "codex", "resume": self.resume, "prompt": self.prompt})
            if self.killed:
                return
            self.rpc = Rpc(self.cwd, self._config(), self._event, self._request, self._write)
            self.proc = self.rpc.proc
            if self.killed:
                return
            sandbox = {"bypassPermissions": "danger-full-access", "acceptEdits": "workspace-write", "default": "read-only"}[self.permission_mode]
            params = {"cwd": str(self.cwd), "developerInstructions": self.system_prompt,
                      "approvalPolicy": "never" if self.ephemeral or self.permission_mode == "bypassPermissions" else "on-request",
                      "approvalsReviewer": "user", "sandbox": sandbox, "modelProvider": "openai"}
            if self.model:
                params["model"] = self.model
            if self.resume:
                params["threadId"] = self.session_id
            else:
                params["ephemeral"] = self.ephemeral
            out = self.rpc.call("thread/resume" if self.resume else "thread/start", params)
            self.session_id = out["thread"]["id"]
            self.on_session(self.session_id)
            actual_model = out.get("model") or self.model
            catalog = next((m for m in INFO["models"] if actual_model in (m.get("model"), m.get("id"))), None)
            if self.effort and catalog and self.effort not in {e["reasoningEffort"] for e in catalog.get("supportedReasoningEfforts", [])}:
                raise RuntimeError(f"Модель {actual_model} не поддерживает effort={self.effort}. Выбери поддерживаемый effort или значение по умолчанию.")
            try:
                self.limits = self.rpc.call("account/rateLimits/read")
            except RuntimeError:
                pass
            inp = [{"type": "text", "text": self.prompt}]
            inp += [{"type": "localImage", "path": str(p)} for p in self.images]
            turn = {"threadId": self.session_id, "input": inp}
            if self.effort:
                turn["effort"] = self.effort
            if self.output_schema:
                turn["outputSchema"] = self.output_schema
            if sandbox == "workspace-write":
                turn["sandboxPolicy"] = {"type": "workspaceWrite", "writableRoots": [str(Path(self.cwd).resolve()), *self.add_dirs], "networkAccess": False}
            response = self.rpc.call("turn/start", turn)
            self.turn_id = response["turn"]["id"]
            while not self.completed.wait(0.25):
                if self.killed:
                    break
                if self.rpc.closed.is_set():
                    raise RuntimeError("Codex завершился до конца хода: " + self.rpc.stderr.decode("utf-8", "replace")[-2000:])
        except Exception as e:
            self.result = {"is_error": True, "result": str(e)}
        finally:
            if self.rpc:
                try:
                    if self.session_id:
                        self.rpc.call("thread/backgroundTerminals/clean", {"threadId": self.session_id}, timeout=5)
                except (RuntimeError, OSError):
                    pass
                self.rpc.close()
            if self.result is None:
                self.result = {"is_error": not self.killed, "result": "Работа остановлена"}
            with self.log_lock:
                if self.log:
                    self.log.close()
                    self.log = None
            try:
                self._finish()
            finally:
                self.finished.set()
                with ACTIVE_LOCK:
                    ACTIVE.discard(self)

    def kill(self):
        self.killed = True
        if self.rpc:
            kill_tree(self.rpc.proc)
        self.completed.set()

    def _rate_error(self, error):
        text = json.dumps(error, ensure_ascii=False)
        if re.search(r"usage.?limit|rate.?limit|quota|limit.?reached", text, re.I):
            limits = (self.limits or {}).get("rateLimitsByLimitId") or {"codex": (self.limits or {}).get("rateLimits") or {}}
            resets = [w["resetsAt"] for bucket in limits.values() for w in (bucket.get("primary"), bucket.get("secondary"))
                      if w and w.get("usedPercent", 0) >= 100 and w.get("resetsAt", 0) > time.time()]
            self.rate_limited = {"resetsAt": max(resets) if resets else None, "estimated": not bool(resets)}
            self.on_rate_limit(self.rate_limited)

    def _event(self, method, p):
        if p.get("threadId") and self.session_id and p["threadId"] != self.session_id:
            return
        turn_id = p.get("turnId") or (p.get("turn") or {}).get("id")
        if turn_id and self.turn_id and turn_id != self.turn_id:
            return
        if method == "account/rateLimits/updated":
            self.limits = p
        elif method == "item/completed":
            item = p.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("text", "").strip():
                self.messages.append(item["text"])
                self.on_text(item["text"])
        elif method == "item/started":
            item = p.get("item") or {}
            kind = item.get("type")
            if kind == "mcpToolCall":
                self.on_tool("mcp__" + item.get("server", "") + "__" + item.get("tool", ""), item.get("arguments"))
            elif kind in ("commandExecution", "fileChange", "webSearch", "imageView", "collabToolCall"):
                self.on_tool(kind, item)
        elif method == "error":
            self._rate_error(p.get("error") or p)
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            error = turn.get("error") or {}
            self._rate_error(error)
            self.result = {"is_error": turn.get("status") != "completed",
                           "result": error.get("message") or (self.messages[-1] if self.messages else ""),
                           "session_id": self.session_id}
            self.completed.set()

    def _permission(self, method, p):
        if self.ephemeral:
            return False
        import pipeline as pl
        payload = {"tool_name": method, "input": p}
        out = json.loads(pl._mcp("approve", payload, self.task_id, self.project_id))
        qid = out.get("question_id")
        while qid and not self.killed and not self.completed.wait(0.2):
            out = json.loads(pl._mcp("approve_status", {"question_id": qid}, self.task_id, self.project_id))
            if out.get("done"):
                break
        return not self.killed and out.get("behavior") == "allow"

    def _request(self, method, p):
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "accept" if self._permission(method, p) else "decline"}
        if method == "item/permissions/requestApproval":
            return {"permissions": p.get("permissions", {}) if self._permission(method, p) else {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            import pipeline as pl
            answers = {}
            for question in p.get("questions", []):
                if not self.task_id:
                    answers[question["id"]] = {"answers": ["Задай вопрос пользователю обычным сообщением в чате."]}
                    continue
                pl._mcp("ask_user", {"question": question["question"], "options": [o["label"] for o in question.get("options", [])]},
                        self.task_id, self.project_id)
                answers[question["id"]] = {"answers": ["Вопрос отправлен в трекер. Заверши ход; ответ придёт следующим ходом."]}
            return {"answers": answers}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline", "content": None, "_meta": None}
        raise RuntimeError("Неподдерживаемый запрос Codex: " + method)


def generate(prompt, system, model=None, schema=None, timeout=600):
    done, result = threading.Event(), {}
    def finish(r):
        result.update(r)
        done.set()
    run = CodexRun(key="generate", cwd=tempfile.gettempdir(), prompt=prompt, session_id=None, resume=False,
                   role="chat", system_prompt=system, model=model, output_schema=schema, ephemeral=True,
                   permission_mode="default", on_done=finish)
    run.start()
    if not done.wait(timeout):
        run.kill()
        done.wait(20)
        raise RuntimeError("Codex: превышено время ожидания ответа")
    if result.get("is_error"):
        raise RuntimeError(result.get("result"))
    return result.get("result", "")
