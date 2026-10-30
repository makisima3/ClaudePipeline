"""Локальные номера и техническая адресация: отдельные базы, процессы и заглушки."""
import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_pipeline as fixture
import telegram as tg

db, pl, main = fixture.db, fixture.pl, fixture.main


class TaskNumberTests(unittest.TestCase):
    def setUp(self):
        fixture.PipelineTests.setUp(self)

    def test_independent_numbers_deletion_and_immutability(self):
        other = db.create_project("Другой", str(fixture.SANDBOX))
        a2 = db.create_task(self.pid, "Вторая")
        b1, b2 = [db.create_task(other, title) for title in ("Первая", "Вторая")]
        self.assertEqual([db.get_task(i)["task_number"] for i in (self.tid, a2, b1, b2)], [1, 2, 1, 2])
        self.assertEqual(len({self.tid, a2, b1, b2}), 4)
        for tid in (b2, b1):
            main.api_task_delete(None, {}, {}, tid)
        db._migrate_task_numbers()
        b3 = db.create_task(other, "После удаления всех")
        self.assertEqual(db.get_task(b3)["task_number"], 3)
        for kind in ("todo", "progress", "waiting", "done", "backlog"):
            db.move_task(a2, kind, status="idle")
            self.assertEqual(db.get_task(a2)["task_number"], 2)
        for field in ("id", "task_id", "project_id", "task_number"):
            with self.assertRaises(ValueError):
                db.update_task(a2, **{field: 99})
        with self.assertRaises(sqlite3.IntegrityError):
            db.ex("UPDATE tasks SET task_number=1 WHERE id=?", (a2,))

    def test_atomic_creation_from_ui_mcp_import_and_internal_call(self):
        col = db.column_by_kind(self.pid, "backlog")["id"]

        def create(n):
            if n % 4 == 0:
                main.api_task_create(None, {"title": f"UI {n}"}, {}, self.pid)
            elif n % 4 == 1:
                pl._mcp("create_task", {"title": f"MCP {n}"}, None, self.pid)
            elif n % 4 == 2:
                main.api_import_commit(None, {"column_id": col, "tasks": [{"title": f"Импорт {n}/{i}"} for i in range(3)]}, {}, self.pid)
            else:
                db.create_task(self.pid, f"Внутренняя {n}")

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(create, range(48)))
        rows = db.q("SELECT task_number, position FROM tasks WHERE project_id=? ORDER BY task_number", (self.pid,))
        self.assertEqual([r["task_number"] for r in rows], list(range(1, 74)))
        self.assertEqual(len({r["position"] for r in rows}), 73)
        before = db.one("SELECT last_number FROM project_task_counters WHERE project_id=?", (self.pid,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_task(self.pid, "Неуспешная вставка", column_id=-123)
        self.assertEqual(db.one("SELECT last_number FROM project_task_counters WHERE project_id=?", (self.pid,)), before)

    def test_search_and_mcp_expose_context_and_keep_global_commands(self):
        other = db.create_project("Другой", str(fixture.SANDBOX))
        b = json.loads(pl._mcp("create_task", {"title": "Совпадение", "project_id": other}, None, self.pid))
        self.assertEqual(b["task_number"], 1)
        self.assertNotEqual(b["task_id"], self.tid)
        results = main.api_task_search(None, {}, {"q": ["#1"]})
        self.assertEqual({r["task_id"] for r in results}, {self.tid, b["task_id"]})
        self.assertEqual({r["project_name"] for r in results}, {"Тест", "Другой"})
        scoped = main.api_task_search(None, {}, {"q": ["1"], "project_id": [str(other)]})
        self.assertEqual([r["task_id"] for r in scoped], [b["task_id"]])
        self.assertEqual(main.api_task_search(None, {}, {"q": [str(b["task_id"])]}), [])
        listed = json.loads(pl._mcp("list_tasks", {"project_id": other, "task_number": 1}, None, self.pid))
        self.assertEqual(listed[0]["task_id"], b["task_id"])
        pl._mcp("update_task", {"task_id": b["task_id"], "title": "Исправлена", "links": [self.tid]}, None, self.pid)
        self.assertEqual(db.get_task(self.tid)["title"], "Проверка")
        self.assertEqual(db.get_task(b["task_id"])["title"], "Исправлена")
        linked = main.api_task(None, {}, {}, self.tid)["links"][0]
        self.assertEqual((linked["task_id"], linked["task_number"], linked["project_id"]), (b["task_id"], 1, other))
        detail = json.loads(pl._mcp("get_task", {"task_id": b["task_id"]}, None, self.pid))
        self.assertEqual((detail["task_id"], detail["task_number"], detail["project_id"]), (b["task_id"], 1, other))
        prompt = pl.task_prompt(db.get_task(self.tid), db.one("SELECT * FROM projects WHERE id=?", (self.pid,)))
        self.assertIn(f"Другой #1", prompt)
        self.assertIn(f"task_id={b['task_id']}", prompt)

    def test_resume_finish_report_and_telegram_target_global_id(self):
        other = db.create_project("Другой", str(fixture.SANDBOX))
        tid = db.create_task(other, "Точная адресация", telegram_notify=True)
        db.move_task(tid, "progress", status="running", session_id="preserved-session", last_run_at=123)
        pl.mcp_call({"task_id": tid, "project_id": other, "tool": "ask_user", "args": {"question": "Выбор?"}})
        qu = db.list_questions()[0]
        self.assertEqual((qu["task_id"], qu["task_number"], qu["project_name"]), (tid, 1, "Другой"))
        listed = json.loads(pl._mcp("list_questions", {}, None, self.pid))[0]
        self.assertEqual(listed["task_id"], tid)
        self.assertIn("Другой #1", listed["task"])
        pl.answer_question(qu["id"], "Ответ")
        with patch.object(pl, "agent_run", side_effect=lambda **kw: fixture.FakeRun(**kw)):
            pl.orch.start_task(db.get_task(tid))
        run = fixture.FakeRun.instances[-1]
        self.assertEqual((run.task_id, run.key, run.session_id), (tid, f"task-{tid}", "preserved-session"))
        self.assertIn(f"task_id={tid}", run.prompt)
        self.assertIn("Другой #1", run.prompt)
        self.assertTrue(str(run.log_path).endswith(f"task-{tid}.jsonl"))
        status = json.loads(pl._mcp("pipeline_status", {}, None, self.pid))
        self.assertEqual(status["running"][0]["task_id"], tid)
        self.assertEqual(status["running"][0]["project_name"], "Другой")
        material = fixture.SANDBOX / "result.txt"
        material.write_text("Материал", encoding="utf-8")
        result = pl.mcp_call({"task_id": tid, "project_id": other, "tool": "finish_task",
                              "args": {"summary": "Готово", "report": "Проверено", "telegram_files": [str(material)]}})
        self.assertFalse(result.get("isError"), result)
        pl.orch._done(tid, {"result": "Готово"})
        self.assertEqual(db.get_task(self.tid)["status"], "idle")
        report = main.api_task_report(None, {}, {}, tid)
        self.assertIn("Задача Другой #1", report["markdown"])
        self.assertEqual(report["task_id"], tid)
        self.assertTrue(db.get_task(tid)["report_path"].endswith(f"task-{tid}.md"))
        delivery = db.one("SELECT * FROM telegram_deliveries WHERE task_id=?", (tid,))
        parts = json.loads(delivery["payload"])
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(p["text"].startswith("Другой #1") for p in parts))
        pl.finish_task(tid, "Повторный вызов", "Отчёт")
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM telegram_deliveries WHERE task_id=?", (tid,))["n"], 1)
        self.assertIsNone(tg.delivery(self.tid))

    def test_queue_priority_still_uses_resume_then_column_position(self):
        other = db.create_project("Другой", str(fixture.SANDBOX))
        first = db.create_task(other, "Продолжение")
        second = db.create_task(self.pid, "Раньше по позиции")
        db.move_task(self.tid, "todo", status="queued")
        db.move_task(second, "todo", status="queued")
        db.update_task(self.tid, position=99)
        db.move_task(first, "progress", status="resume", pending_prompt="Продолжи", session_id="resume-id")
        db.set_setting("pipeline_enabled", "1")
        db.set_setting("max_parallel", "1")
        with patch.object(pl, "agent_run", side_effect=lambda **kw: fixture.FakeRun(**kw)):
            pl.orch.tick()
            self.assertEqual(list(pl.orch.runs), [first])
            queue = json.loads(pl._mcp("pipeline_status", {}, None, self.pid))["queue"]
            self.assertEqual({r["task_id"] for r in queue}, {self.tid, second})
            self.assertTrue(all(r["project_id"] == self.pid and r["project_name"] == "Тест" for r in queue))
            pl.orch._done(first, {"result": "Продолжение готово"})
            pl.orch.tick()
            self.assertEqual(list(pl.orch.runs), [second])
            self.assertEqual(db.get_task(second)["task_number"], 2)
            self.assertEqual(db.get_task(self.tid)["status"], "queued")


class NumberMigrationTests(unittest.TestCase):
    def run_code(self, root, code):
        result = subprocess.run([sys.executable, "-c", code], cwd=root / "server", capture_output=True, timeout=20,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def test_old_schema_preserves_data_and_files_and_never_renumbers(self):
        with tempfile.TemporaryDirectory(prefix="pipeline-numbers-migration-") as tmp:
            root = Path(tmp)
            shutil.copytree(fixture.SOURCE / "server", root / "server")
            (root / "data/reports").mkdir(parents=True)
            (root / "data/runs").mkdir()
            report = root / "data/reports/task-20.md"
            log = root / "data/runs/task-20.jsonl"
            report.write_text("Исторический отчёт #20", encoding="utf-8")
            log.write_text('NOT_MODIFIED', encoding="utf-8")
            tree = ast.parse((root / "server/db.py").read_text(encoding="utf-8"))
            schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in n.targets))
            conn = sqlite3.connect(root / "data/tracker.db")
            conn.executescript(schema)
            conn.execute("DROP TABLE project_task_counters")
            for field in ("codex_session_id", "codex_model", "codex_effort", "last_provider"):
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {field} TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN telegram_notify INTEGER NOT NULL DEFAULT 0")
            for pid in (7, 9):
                conn.execute("INSERT INTO projects(id,name,path) VALUES(?,?,?)", (pid, str(pid), str(root)))
                conn.execute("INSERT INTO columns(id,project_id,name,kind) VALUES(?,?,?,?)", (pid, pid, "Колонка", "todo"))
            for tid, pid, created, status in ((20, 7, 2, "resume"), (8, 7, 1, "waiting"), (3, 7, 2, "queued"), (40, 9, 1, "running")):
                conn.execute("""INSERT INTO tasks(id,project_id,column_id,title,created_at,status,session_id,codex_session_id,
                                pending_prompt,report_path,last_run_at,telegram_notify) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (tid, pid, pid, str(tid), created, status, f"claude-{tid}", f"codex-{tid}", "Ответ", str(report), 99, 1))
                conn.execute("INSERT INTO events(task_id,kind,text) VALUES(?,'comment','Событие')", (tid,))
            conn.execute("INSERT INTO questions(task_id,text,status) VALUES(8,'Вопрос','open')")
            conn.execute("INSERT INTO task_links VALUES(8,40,10)")
            conn.execute("INSERT INTO telegram_deliveries(task_id,run_key,payload,state,next_part) VALUES(20,'99','[]','uncertain',2)")
            conn.commit()
            tables = ("projects", "columns", "tasks", "events", "questions", "task_links", "telegram_deliveries")
            fields = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}
            before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
            conn.close()
            code = "import db,json; print(json.dumps(db.q('SELECT id,task_number FROM tasks ORDER BY id')))"
            expected = [{"id": 3, "task_number": 2}, {"id": 8, "task_number": 1},
                        {"id": 20, "task_number": 3}, {"id": 40, "task_number": 1}]
            self.assertEqual(self.run_code(root, code), expected)
            conn = sqlite3.connect(root / "data/tracker.db")
            for t in tables:
                self.assertEqual(conn.execute(f"SELECT {','.join(fields[t])} FROM {t} ORDER BY 1").fetchall(), before[t], t)
            conn.execute("UPDATE tasks SET created_at=-id")
            conn.commit()
            conn.close()
            self.assertEqual(self.run_code(root, code), expected)
            self.assertEqual(report.read_text(encoding="utf-8"), "Исторический отчёт #20")
            self.assertEqual(log.read_text(encoding="utf-8"), "NOT_MODIFIED")
            self.run_code(root, "import db,json; db.ex('DELETE FROM tasks WHERE project_id=7'); print(json.dumps(True))")
            after = self.run_code(root, "import db,json; t=db.create_task(7,'new',column_id=7); print(json.dumps(db.get_task(t)['task_number']))")
            self.assertEqual(after, 4)

    def test_separate_processes_allocate_without_duplicates(self):
        with tempfile.TemporaryDirectory(prefix="pipeline-numbers-concurrent-") as tmp:
            root = Path(tmp)
            shutil.copytree(fixture.SOURCE / "server", root / "server")
            pid = self.run_code(root, "import db,json; print(json.dumps(db.create_project('P','.')))")
            code = f"import db,json; print(json.dumps([db.create_task({pid},'Concurrent') for _ in range(15)]))"
            with ThreadPoolExecutor(max_workers=4) as pool:
                ids = [tid for batch in pool.map(lambda _: self.run_code(root, code), range(4)) for tid in batch]
            self.assertEqual(len(set(ids)), 60)
            numbers = self.run_code(root, "import db,json; print(json.dumps([r['task_number'] for r in db.q('SELECT task_number FROM tasks ORDER BY task_number')]))")
            self.assertEqual(numbers, list(range(1, 61)))


if __name__ == "__main__":
    unittest.main()
