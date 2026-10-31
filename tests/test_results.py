"""Общая история на изолированной базе: Telegram не является источником новых результатов."""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import test_pipeline as fixture
import results
import telegram as tg

db, pl, main = fixture.db, fixture.pl, fixture.main


class ResultTests(unittest.TestCase):
    def setUp(self):
        fixture.PipelineTests.setUp(self)
        self.image = fixture.SANDBOX / "результат.png"
        self.image.write_bytes(b"immutable-result-image")

    def finish(self, tid=None):
        tid = tid or self.tid
        pl.finish_task(tid, "Итог", "Отчёт именно этого запуска", "Сделано и проверено", [str(self.image)])
        return results.existing(db.get_task(tid))

    def test_telegram_matrix_and_both_providers(self):
        for provider in ("claude", "codex"):
            for flag, auth, transport in ((False, "ready", "sent"), (True, "ready", "sent"),
                                           (True, "unconfigured", None), (True, "ready", "error")):
                with self.subTest(provider=provider, flag=flag, auth=auth, transport=transport):
                    tid = db.create_task(self.pid, provider, telegram_notify=flag)
                    with patch.object(pl, "AGENT", provider):
                        row = self.finish(tid)
                    db.set_setting("telegram_auth", auth)
                    service = tg.Telegram()
                    with patch.object(service, "_call", return_value={"status": transport, "error": "Тестовая ошибка"}) as call:
                        service.tick()
                        if not flag or auth != "ready":
                            call.assert_not_called()
                    self.assertEqual(db.get_task(tid)["status"], "done")
                    self.assertEqual(row["text"], "Сделано и проверено")
                    self.assertEqual((results.ROOT / json.loads(row["files"])[0]["path"]).read_bytes(), self.image.read_bytes())
                    self.assertEqual(len(db.q("SELECT id FROM task_results WHERE source_task_id=?", (tid,))), 1)
                    if not flag:
                        self.assertIsNone(tg.delivery(tid))

    def test_repeat_rerun_immutable_report_and_materials(self):
        first = self.finish()
        self.image.unlink()
        pl.finish_task(self.tid, "Повторный вызов", "Другой отчёт", telegram_files=[str(self.image)])
        self.assertEqual(first, results.existing(db.get_task(self.tid)))
        self.image.write_bytes(b"new-run")
        db.update_task(self.tid, status="running", last_run_at=db.now())
        second = self.finish()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(results.page(self.pid)["items"]), 2)
        self.assertEqual((results.ROOT / json.loads(first["files"])[0]["path"]).read_bytes(), b"immutable-result-image")

    def test_late_telegram_uses_common_snapshot_after_source_deleted(self):
        row = self.finish()
        self.image.unlink()
        main.api_task_update(None, {"telegram_notify": True}, {}, self.tid)
        self.assertEqual(tg.delivery(self.tid)["state"], "pending")
        parts = json.loads(db.one("SELECT payload FROM telegram_deliveries WHERE task_id=?", (self.tid,))["payload"])
        self.assertIn(row["text"], parts[0]["text"])
        self.assertEqual((tg.ROOT / parts[1]["files"][0]).read_bytes(), b"immutable-result-image")

    def test_project_isolation_and_task_deletion_preserves_history(self):
        first = self.finish()
        other = db.create_project("Другой", str(fixture.SANDBOX))
        other_tid = db.create_task(other, "Другой итог")
        self.finish(other_tid)
        self.assertEqual([r["id"] for r in results.page(self.pid)["items"]], [first["id"]])
        db.ex("DELETE FROM tasks WHERE id=?", (self.tid,))
        row = db.one("SELECT * FROM task_results WHERE id=?", (first["id"],))
        self.assertIsNone(row["task_id"])
        self.assertEqual(results.public(row, True)["report"], "Отчёт именно этого запуска")
        self.assertTrue((results.ROOT / json.loads(row["files"])[0]["path"]).is_file())

    def test_atomic_done_and_result_rollback(self):
        with patch.object(db, "move_task", side_effect=RuntimeError("test rollback")):
            with self.assertRaises(RuntimeError):
                self.finish()
        self.assertNotEqual(db.get_task(self.tid)["status"], "done")
        self.assertFalse(results.page(self.pid)["items"])
        self.finish()
        self.assertEqual(len(results.page(self.pid)["items"]), 1)

    def test_cursor_pagination_restores_all_missed_results(self):
        for n in range(107):
            tid = db.create_task(self.pid, f"Завершение {n}")
            pl.finish_task(tid, str(n), "Проверка курсора")
        cursor, seen = 0, []
        while True:
            page = results.page(after=cursor, limit=20)
            ids = [r["id"] for r in page["items"]]
            seen.extend(ids)
            cursor = ids[-1] if ids else cursor
            if not page["has_more"]:
                break
        self.assertEqual(len(seen), 107)
        self.assertEqual(seen, sorted(set(seen)))
        self.assertFalse(results.page(after=cursor)["items"])

    def test_identity_restart_and_migration_are_idempotent(self):
        row = self.finish()
        identity = results.installation_id()
        script = "import results,json,db; results.recover(); print(json.dumps([results.installation_id(),db.one('SELECT COUNT(*) AS n FROM task_results')[\"n\"]]))"
        env = {**os.environ, "PYTHONPATH": str(fixture.SANDBOX / "server"), "PIPELINE_AGENT": "claude"}
        for _ in range(2):
            out = subprocess.run([sys.executable, "-c", script], cwd=fixture.SANDBOX, env=env, capture_output=True, check=True)
            self.assertEqual(json.loads(out.stdout), [identity, 1])
        self.assertEqual(row["id"], results.page(self.pid)["items"][0]["id"])

    def test_legacy_completed_task_is_imported_once(self):
        db.update_task(self.tid, status="done", summary="Старый итог")
        results.recover(); results.recover()
        page = results.page(self.pid)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["text"], "Старый итог")

    def test_manual_completion_also_enters_common_history(self):
        done = db.one("SELECT id FROM columns WHERE project_id=? AND kind='done'", (self.pid,))
        main.api_task_move(None, {"column_id": done["id"]}, {}, self.tid)
        row = results.existing(db.get_task(self.tid))
        self.assertIn("вручную", row["text"])
        self.assertEqual(len(results.page(self.pid)["items"]), 1)
        self.assertIsNone(tg.delivery(self.tid))

    def test_http_files_range_report_and_validation(self):
        row = self.finish()
        srv = main.Server(("127.0.0.1", 0), main.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            url = results.public(row)["files"][0]["url"]
            with urllib.request.urlopen(base + url) as response:
                self.assertEqual(response.read(), b"immutable-result-image")
                self.assertIn("filename*=", response.headers["Content-Disposition"])
            with urllib.request.urlopen(urllib.request.Request(base + url, headers={"Range": "bytes=2-5"})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"muta")
            with urllib.request.urlopen(base + f"/api/results/{row['id']}") as response:
                self.assertEqual(json.load(response)["report"], "Отчёт именно этого запуска")
            for path, status in (("/api/results?after=-1", 400), ("/api/results?limit=no", 400),
                                 (f"/api/results/{row['id']}/files/99", 404), ("/api/projects/99999/results", 404)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + path)
                self.assertEqual(error.exception.code, status)
        finally:
            srv.shutdown(); srv.server_close(); thread.join(3)


if __name__ == "__main__":
    unittest.main()
