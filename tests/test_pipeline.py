"""Регрессии в отдельной копии. Запуск: python -m unittest discover -s tests -v."""
import ast
import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
SANDBOX = Path(tempfile.mkdtemp(prefix="pipeline-tests-"))
shutil.copytree(SOURCE / "server", SANDBOX / "server")
shutil.copytree(SOURCE / "web", SANDBOX / "web")
shutil.copytree(SOURCE / "tools", SANDBOX / "tools", ignore=shutil.ignore_patterns("__pycache__"))
(SANDBOX / "docs").mkdir()
shutil.copy2(SOURCE / "docs/telegram-results.md", SANDBOX / "docs/telegram-results.md")
(SANDBOX / "data").mkdir()
# Старая схема без новых колонок: проверяем именно миграцию.
tree = ast.parse((SOURCE / "server/db.py").read_text(encoding="utf-8"))
schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in n.targets))
legacy = sqlite3.connect(SANDBOX / "data/tracker.db")
legacy.executescript(schema)
legacy.execute("INSERT INTO settings VALUES('model','opus')")
legacy.execute("INSERT INTO projects(id,name,path) VALUES(1,'legacy',?)", (str(SANDBOX),))
legacy.execute("INSERT INTO columns(id,project_id,name,kind) VALUES(1,1,'old','backlog')")
legacy.execute("INSERT INTO tasks(id,project_id,column_id,title,session_id,model,cost_usd) VALUES(1,1,1,'old','claude-history','sonnet',1.25)")
legacy.execute("INSERT INTO chats(id,project_id,title,session_id) VALUES(1,1,'old','claude-chat')")
legacy.commit()
legacy.close()
os.environ["PIPELINE_AGENT"] = "codex"
sys.path.insert(0, str(SANDBOX / "server"))
import db
import pipeline as pl
import main
import codex_runner as cr
from runner import ClaudeRun
MIGRATED_TASK = db.one("SELECT * FROM tasks WHERE id=1")
MIGRATED_CHAT = db.one("SELECT * FROM chats WHERE id=1")
MIGRATED_SETTINGS = db.get_settings()


class FakeRun:
    instances = []
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.killed = False
        self.instances.append(self)
    def start(self):
        self.on_session("codex-test-session")
        return self
    def kill(self):
        self.killed = True
        self.on_done({"killed": True})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        pl.orch.runs.clear()
        pl.orch.manual_stops.clear()
        pl.chat.runs.clear()
        pl.restart.since = None
        for table in ("questions", "chat_messages", "chats", "events", "task_links", "tasks", "columns", "projects"):
            db.ex("DELETE FROM " + table)
        for k, v in db.DEFAULT_SETTINGS.items():
            db.ex("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
        self.pid = db.create_project("Тест", str(SANDBOX))
        self.tid = db.create_task(self.pid, "Проверка", "Описание")
        FakeRun.instances.clear()

    @unittest.skipUnless(os.name == "nt", "Стартовые CMD-файлы предназначены для Windows")
    def test_windows_launchers_parse_and_forward_arguments(self):
        with tempfile.TemporaryDirectory(prefix="agentpipeline-launcher-") as tmp:
            folder = Path(tmp) / "проверка запуска"
            folder.mkdir()
            for agent in ("codex", "claude"):
                shutil.copy2(SOURCE / f"start_{agent}.cmd", folder)
            env = os.environ.copy()
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            for agent in ("codex", "claude"):
                with self.subTest(agent=agent, python="missing"):
                    result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", f"start_{agent}.cmd"],
                                            cwd=folder, env=env, input=b"\n", capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 1, result.stderr.decode("utf-8", "replace"))
                    self.assertIn(b"https://www.python.org/downloads/", result.stdout)
            # Только заглушка интерпретатора: сервер, Claude и Codex не запускаются.
            (folder / "python.cmd").write_text("@echo off\necho LAUNCHER_PYTHON %*\nexit /b 0\n", encoding="ascii")
            for agent in ("codex", "claude"):
                with self.subTest(agent=agent, python="stub"):
                    result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", f"start_{agent}.cmd", "--no-browser"],
                                            cwd=folder, env=env, input=b"\n", capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                    self.assertIn(f"LAUNCHER_PYTHON server\\main.py --agent {agent} --no-browser".encode(), result.stdout)

    def test_00_legacy_migration_evidence(self):
        # Наличие колонок доказывает миграцию созданной до импорта базы.
        self.assertIn("codex_session_id", {r["name"] for r in db.q("PRAGMA table_info(tasks)")})
        self.assertIn("codex_session_id", {r["name"] for r in db.q("PRAGMA table_info(chats)")})
        self.assertEqual(MIGRATED_TASK["session_id"], "claude-history")
        self.assertEqual(MIGRATED_TASK["model"], "sonnet")
        self.assertEqual(MIGRATED_TASK["cost_usd"], 1.25)
        self.assertIsNone(MIGRATED_TASK["codex_session_id"])
        self.assertEqual(MIGRATED_CHAT["session_id"], "claude-chat")
        self.assertEqual(MIGRATED_SETTINGS["model"], "")
        self.assertEqual(MIGRATED_SETTINGS["codex_model"], "")

    def test_handoff_retried_after_failed_launch(self):
        db.ex("UPDATE tasks SET session_id='claude-old',last_provider='claude',summary='transfer-marker' WHERE id=?", (self.tid,))
        cid = db.create_chat(self.pid)
        db.ex("UPDATE chats SET session_id='claude-chat',last_provider='claude' WHERE id=?", (cid,))
        db.add_chat(cid, "user", "chat-transfer-marker")
        class BrokenRun(FakeRun):
            def start(self):
                self.on_session("created-but-failed")
                raise RuntimeError("simulated start failure")
        with patch.object(pl, "agent_run", side_effect=lambda **kw: BrokenRun(**kw)):
            for _ in range(2):
                pl.orch.start_task(db.get_task(self.tid))
                self.assertIn("transfer-marker", FakeRun.instances[-1].prompt)
                pl.chat.send(cid, "retry")
                self.assertIn("chat-transfer-marker", FakeRun.instances[-1].prompt)
        self.assertEqual(db.get_task(self.tid)["last_provider"], "claude")
        self.assertEqual(db.get_chat(cid)["last_provider"], "claude")

    def test_child_events_and_ephemeral_permissions(self):
        run = cr.CodexRun(key="filter", cwd=SANDBOX, prompt="", session_id="main", resume=True, role="chat", system_prompt="", ephemeral=True)
        run.turn_id = "main-turn"
        run._event("turn/completed", {"threadId": "child", "turn": {"status": "completed"}})
        run._event("turn/completed", {"threadId": "main", "turn": {"id": "old-turn", "status": "completed"}})
        run._event("item/completed", {"threadId": "main", "turnId": "old-turn", "item": {"type": "agentMessage", "text": "wrong"}})
        self.assertFalse(run.completed.is_set())
        self.assertFalse(run.messages)
        self.assertEqual(run._request("item/fileChange/requestApproval", {}), {"decision": "decline"})
        self.assertEqual(run._request("item/permissions/requestApproval", {"permissions": {"network": {"enabled": True}}}),
                         {"permissions": {}, "scope": "turn"})
        self.assertEqual(run._config()["mcp_servers"], {})
        self.assertEqual(run._config()["approval_policy"], "never")

    def test_exact_rate_limit_and_startup_diagnostics(self):
        resets = int(time.time()) + 600
        info = []
        run = cr.CodexRun(key="rate", cwd=SANDBOX, prompt="", session_id="s", resume=True, role="task", system_prompt="", on_rate_limit=info.append)
        run.limits = {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": resets}}}
        run._rate_error({"codexErrorInfo": "usageLimitExceeded"})
        self.assertEqual(info, [{"resetsAt": resets, "estimated": False}])
        with patch.object(cr.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI"):
                cr.executable()
        with patch.object(cr, "executable", return_value=["fake"]), patch.object(cr.subprocess, "run") as cmd:
            cmd.return_value.stdout = b"codex-cli 0.100.0"
            with self.assertRaisesRegex(RuntimeError, "0.166.0"):
                cr.preflight()

    def test_mcp_protocol_and_role_lists(self):
        import mcp_tracker as tracker
        self.assertIn("protocolVersion", tracker.handle({"method": "initialize", "params": {}}))
        self.assertIn("finish_task", {t["name"] for t in tracker.TASK_TOOLS})
        self.assertNotIn("finish_task", {t["name"] for t in tracker.CHAT_TOOLS})
        self.assertIn("create_task", {t["name"] for t in tracker.CHAT_TOOLS})
        with patch.object(tracker, "call_server", return_value={"text": "ok"}) as call:
            result = tracker.handle({"method": "tools/call", "params": {"name": "report_progress", "arguments": {"text": "marker"}}})
            self.assertEqual(result["content"][0]["text"], "ok")
            call.assert_called_once_with("report_progress", {"text": "marker"})

    def test_provider_settings_and_sessions(self):
        db.ex("UPDATE tasks SET session_id='claude-old', model='opus',cost_usd=2 WHERE id=?", (self.tid,))
        db.set_setting("model", "gpt-test")
        db.update_task(self.tid, session_id="codex-new", model="gpt-test", effort="high")
        task = db.get_task(self.tid)
        self.assertEqual(task["session_id"], "codex-new")
        self.assertEqual(task["model"], "gpt-test")
        self.assertIsNone(task["cost_usd"])
        self.assertEqual(db.one("SELECT session_id,model,cost_usd FROM tasks WHERE id=?", (self.tid,)),
                         {"session_id": "claude-old", "model": "opus", "cost_usd": 2})
        self.assertEqual(db.one("SELECT value FROM settings WHERE key='model'")["value"], "")
        with patch.object(db, "AGENT", "claude"):
            self.assertEqual(db.get_task(self.tid)["session_id"], "claude-old")
            db.set_setting("model", "sonnet")
        self.assertEqual(db.get_setting("model"), "gpt-test")

    def test_queue_parallelism_and_graceful_stop(self):
        pid2 = db.create_project("Второй", str(SANDBOX))
        ids = [self.tid, db.create_task(self.pid, "Ещё"), db.create_task(pid2, "Параллельно")]
        for tid in ids:
            db.move_task(tid, "todo", status="queued")
        db.set_setting("pipeline_enabled", "1")
        with patch.object(pl, "agent_run", side_effect=lambda **kw: FakeRun(**kw)):
            pl.orch.tick()
            self.assertEqual(set(pl.orch.runs), {ids[0], ids[2]})
            self.assertEqual(len({r.project_id for r in pl.orch.runs.values()}), 2)
            pl.orch.set_enabled(False)
            self.assertEqual(db.get_setting("pipeline_enabled"), "stopping")
            for tid in list(pl.orch.runs):
                pl.orch._done(tid, {"result": "Готово"})
            pl.orch.tick()
            self.assertEqual(db.get_setting("pipeline_enabled"), "0")

    def test_question_resume_report_and_repeat(self):
        db.move_task(self.tid, "progress", status="running", session_id="same-session", last_run_at=time.time())
        pl._mcp("ask_user", {"question": "Какой цвет?", "options": ["Синий"]}, self.tid, self.pid)
        qid = db.one("SELECT id FROM questions WHERE task_id=?", (self.tid,))["id"]
        self.assertEqual(db.get_task(self.tid)["status"], "waiting")
        pl.orch._finish_run(self.tid, {"result": "Жду"})
        self.assertTrue(pl.answer_question(qid, "Синий"))
        self.assertFalse(pl.answer_question(qid, "Другой"))
        self.assertEqual(db.get_task(self.tid)["status"], "resume")
        with patch.object(pl, "agent_run", side_effect=lambda **kw: FakeRun(**kw)):
            pl.orch.start_task(db.get_task(self.tid))
        run = FakeRun.instances[-1]
        self.assertTrue(run.resume)
        self.assertEqual(run.session_id, "same-session")
        self.assertIn("Синий", run.prompt)
        pl._mcp("finish_task", {"summary": "Сделано", "report": "Подробно"}, self.tid, self.pid)
        pl.orch._done(self.tid, {"result": "ok"})
        report = Path(db.get_task(self.tid)["report_path"]).read_text(encoding="utf-8")
        self.assertIn("Сессия Codex", report)
        self.assertIn("codex resume", report)
        pl.finish_task(self.tid, "Повтор", "Новый отчёт")
        self.assertIn("Предыдущий отчёт", Path(db.get_task(self.tid)["report_path"]).read_text(encoding="utf-8"))

    def test_codex_allow_and_decline(self):
        for answer, expected in (("Разрешить", "accept"), ("Запретить", "decline")):
            run = cr.CodexRun(key="test", cwd=SANDBOX, prompt="", session_id="s", resume=True, role="task",
                              system_prompt="", task_id=self.tid, project_id=self.pid)
            result = []
            worker = threading.Thread(target=lambda: result.append(run._request("item/commandExecution/requestApproval", {"command": "example"})))
            worker.start()
            deadline = time.time() + 3
            while time.time() < deadline:
                row = db.one("SELECT id FROM questions WHERE status='open' AND task_id=?", (self.tid,))
                if row:
                    break
                time.sleep(0.02)
            self.assertTrue(row)
            pl.answer_question(row["id"], answer)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [{"decision": expected}])

    def test_limit_pause_and_recovery(self):
        db.move_task(self.tid, "progress", status="running", session_id="s")
        pl.orch._finish_run(self.tid, {"is_error": True, "rate_limited": {"estimated": True}, "result": "usage limit"})
        self.assertEqual(db.get_task(self.tid)["status"], "resume")
        self.assertGreater(float(db.get_setting("pause_until")), time.time())
        self.assertEqual(db.get_setting("pause_estimated"), "1")
        db.update_task(self.tid, status="running")
        pl.orch.recover()
        self.assertEqual(db.get_task(self.tid)["status"], "resume")

    def test_stop_and_restart_interruption(self):
        with patch.object(pl, "agent_run", side_effect=lambda **kw: FakeRun(**kw)):
            pl.orch.start_task(db.get_task(self.tid))
        pl.orch.stop_task(self.tid)
        self.assertEqual(db.get_task(self.tid)["status"], "interrupted")
        self.assertFalse(pl.orch.runs)
        with patch.object(pl, "agent_run", side_effect=lambda **kw: FakeRun(**kw)):
            pl.orch.start_task(db.get_task(self.tid))
        pl.restart.since = time.time()
        pl.orch.runs[self.tid].kill()
        self.assertEqual(db.get_task(self.tid)["status"], "resume")

    def test_recover_orphan_permission_preserves_real_questions(self):
        db.move_task(self.tid, "waiting", status="waiting", session_id="recover-session")
        db.add_question(self.tid, "Разрешить?", kind="permission")
        other = db.create_task(self.pid, "Настоящий вопрос")
        db.move_task(other, "waiting", status="waiting", session_id="another-session")
        db.add_question(other, "Какой вариант?", kind="question")
        pl.orch.recover()
        self.assertEqual(db.get_task(self.tid)["status"], "resume")
        self.assertEqual(db.get_task(other)["status"], "waiting")
        self.assertEqual(db.one("SELECT status FROM questions WHERE task_id=?", (self.tid,))["status"], "cancelled")

    def test_handoff_and_chat_images(self):
        db.ex("UPDATE tasks SET session_id='claude-task',last_provider='claude',summary='Прошлый результат' WHERE id=?", (self.tid,))
        cid = db.create_chat(self.pid)
        db.ex("UPDATE chats SET session_id='claude-chat',last_provider='claude' WHERE id=?", (cid,))
        db.add_chat(cid, "user", "Старое сообщение")
        with patch.object(pl, "agent_run", side_effect=lambda **kw: FakeRun(**kw)):
            pl.orch.start_task(db.get_task(self.tid))
            task_run = FakeRun.instances[-1]
            self.assertFalse(task_run.resume)
            self.assertIsNone(task_run.session_id)
            self.assertIn("Прошлый результат", task_run.prompt)
            pl.chat.send(cid, "Посмотри", [{"file": "sample.png", "name": "sample.png", "type": "image/png", "size": 10}])
        chat_run = FakeRun.instances[-1]
        self.assertFalse(chat_run.resume)
        self.assertIn("Старое сообщение", chat_run.prompt)
        self.assertTrue(chat_run.images[0].endswith("sample.png"))
        self.assertEqual(db.get_chat(cid)["session_id"], "codex-test-session")
        self.assertEqual(db.one("SELECT session_id FROM chats WHERE id=?", (cid,))["session_id"], "claude-chat")

    def test_chat_title_import_and_links(self):
        cid = pl.chat.create(self.pid)["id"]
        self.assertEqual(pl.chat.create(self.pid)["id"], cid)
        db.add_chat(cid, "user", "Привет")
        db.add_chat(cid, "assistant", "Ответ")
        with patch.object(cr, "generate", return_value="Название разговора"):
            pl.titler._make(cid)
        self.assertEqual(db.get_chat(cid)["title"], "Название разговора")
        pl.chat.rename(cid, "Вручную")
        with patch.object(cr, "generate", side_effect=AssertionError("Ручное название нельзя менять")):
            pl.titler._make(cid)
        with patch.object(cr, "generate", return_value='{"tasks":[{"title":"Новая","description":"Описание"}]}'):
            self.assertEqual(pl.import_parse(self.pid, "Новая"), [{"title": "Новая", "description": "Описание"}])
        out = pl._mcp("create_task", {"title": "Связанная", "links": [self.tid]}, None, self.pid)
        self.assertEqual(json.loads(out)["message"], "Задача создана")
        self.assertEqual(len(db.task_links(self.tid)), 1)
        self.assertIn("Связанные задачи", pl.task_prompt(db.get_task(self.tid), {"name": "Тест", "path": str(SANDBOX)}))

    def test_git_modes_and_claude_compatibility_without_execution(self):
        for mode in ("off", "commit", "commit_push"):
            db.set_setting("git_mode", mode)
            self.assertIn(mode, pl.git_line())
        with patch.object(pl, "AGENT", "claude"):
            run = pl.agent_run(key="fixture", cwd=str(SANDBOX), prompt="test", session_id="legacy-session", resume=True,
                               role="task", system_prompt="rules", permission_mode="default", model="sonnet", effort="high",
                               on_session=lambda s: None, images=[])
        self.assertIsInstance(run, ClaudeRun)
        cmd = run.command()
        self.assertIn("--resume", cmd)
        self.assertIn("legacy-session", cmd)
        self.assertIn("mcp__tracker__approve", cmd)
        result = []
        run.on_done = result.append
        run._handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "Ответ"}]}})
        run.result = {"result": "ok"}
        run._finish()
        run._finish()
        self.assertEqual(len(result), 1)

    def test_git_readiness_endpoint_is_project_scoped_and_validated(self):
        expected = {"status": "local_ready", "checks": [], "write_access": "not_checked"}
        with patch.object(main, "check_git_readiness", return_value=expected) as check:
            self.assertEqual(main.api_git_readiness(None, None, {"auth": ["gh"]}, self.pid), expected)
            check.assert_called_once_with(str(SANDBOX), "commit_push", "gh")
            db.set_setting("git_mode", "off")
            main.api_git_readiness(None, None, {}, self.pid)
            check.assert_called_with(str(SANDBOX), "off", "existing")
            for query in ({"mode": ["bad"]}, {"auth": ["bad"]}):
                with self.assertRaises(main.HttpError) as err:
                    main.api_git_readiness(None, None, query, self.pid)
                self.assertEqual(err.exception.code, 400)
            with self.assertRaises(main.HttpError) as err:
                main.api_git_readiness(None, None, {}, 999999)
            self.assertEqual(err.exception.code, 404)

    def test_codex_events_and_once_callback(self):
        result, text, tools = [], [], []
        run = cr.CodexRun(key="events", cwd=SANDBOX, prompt="", session_id="s", resume=True, role="task", system_prompt="",
                          on_done=result.append, on_text=text.append, on_tool=lambda n, a: tools.append(n))
        run._event("item/started", {"item": {"type": "commandExecution", "command": "echo test"}})
        run._event("item/completed", {"item": {"type": "agentMessage", "text": "Готово"}})
        run._event("turn/completed", {"turn": {"status": "completed"}})
        run._finish()
        run._finish()
        self.assertEqual(text, ["Готово"])
        self.assertEqual(tools, ["commandExecution"])
        self.assertEqual(len(result), 1)

    def test_http_uploads_sse_and_board(self):
        srv = main.Server(("127.0.0.1", 0), main.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(srv.server_port)
        try:
            with urllib.request.urlopen(url + "/api/state") as response:
                self.assertEqual(json.load(response)["agent"], "codex")
            with urllib.request.urlopen(url + f"/api/projects/{self.pid}/board") as response:
                self.assertEqual(len(json.load(response)["tasks"]), 1)
            with urllib.request.urlopen(url + "/api/events", timeout=3) as response:
                self.assertIn(b"hello", b"".join(response.readline() for _ in range(2)))
            cid = db.create_chat(self.pid)
            folder = db.chat_dir(self.pid, cid)
            attachments = main.save_uploads(folder, [{"name": "test.txt", "type": "text/plain", "data": base64.b64encode(b"file-content").decode()}])
            self.assertEqual((folder / attachments[0]["file"]).read_bytes(), b"file-content")
            mid = db.add_chat(cid, "user", "Файл", attachments)
            with urllib.request.urlopen(url + f"/api/chat/{mid}/files/0") as response:
                self.assertEqual(response.read(), b"file-content")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_single_instance_and_restart_arguments(self):
        code = "import sys;sys.path.insert(0,'server');import runtime;runtime.acquire();print('LOCKED',flush=True);input()"
        proc = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=SANDBOX, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertIn(b"LOCKED", proc.stdout.readline())
            other = subprocess.run([sys.executable, "-u", "-c", code], cwd=SANDBOX, input=b"\n", capture_output=True, timeout=5)
            self.assertNotEqual(other.returncode, 0)
        finally:
            proc.communicate(b"\n", timeout=5)
        with patch.object(main.subprocess, "Popen") as spawn:
            main.spawn_successor()
            args = spawn.call_args.args[0]
            self.assertEqual(args[args.index("--agent") + 1], "codex")


if __name__ == "__main__":
    unittest.main()
