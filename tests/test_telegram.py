"""Telegram на отдельной базе: транспорт и агенты заменены заглушками, без внешней отправки."""
import importlib.util
import json
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_pipeline as fixture
import telegram as tg

db, pl, main = fixture.db, fixture.pl, fixture.main


class TelegramTests(unittest.TestCase):
    def setUp(self):
        fixture.PipelineTests.setUp(self)
        self.service = tg.Telegram()

    def finish(self, files=None):
        db.update_task(self.tid, telegram_notify=1)
        pl.finish_task(self.tid, "Готово", "Подробный отчёт", "Сделано и проверено на заглушках", files)
        return tg.delivery(self.tid)

    def test_migration_and_explicit_requests(self):
        self.assertEqual(fixture.MIGRATED_TASK["telegram_notify"], 0)
        self.assertEqual(db.get_task(self.tid)["telegram_notify"], 0)

        for text in ("Скинь результат в ТГ", "Пришли скриншот в Telegram", "Отправить результат в телеграм", "В Telegram перешли итог",
                     "Нужно скинуть результат в ТГ", "Не забудь отправить результат в Telegram", "Скинь в ТГ, не забудь скриншот"):
            with self.subTest(text=text):
                tid = db.create_task(self.pid, "Работа", text, telegram_notify=False)
                self.assertEqual(db.get_task(tid)["telegram_notify"], 1)
        for text in ("Не отправляй в ТГ", "Не надо скидывать в Telegram", "Добавить интеграцию Telegram",
                     "Добавь отправку уведомлений в Telegram", "Проверить настройку отправки в телеграм"):
            self.assertFalse(db.telegram_requested(text))
        db.update_task(self.tid, description="Скинь результат в ТГ")
        self.assertEqual(db.get_task(self.tid)["telegram_notify"], 1)
        db.update_task(self.tid, telegram_notify=False)
        self.assertEqual(db.get_task(self.tid)["telegram_notify"], 0)
        db.update_task(self.tid, description="Скинь результат в ТГ")  # неизменённый текст не отменяет ручной флаг
        self.assertEqual(db.get_task(self.tid)["telegram_notify"], 0)

    def test_profile_directory_requires_live_verification(self):
        profile = tg.ROOT / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self.service.recover()
            self.assertEqual(db.get_setting("telegram_auth"), "unknown")
            with patch.object(self.service, "_call", return_value={"status": "auth_required", "error": "Нужен вход"}):
                self.service.lock.acquire()
                self.service._setup("check")
            self.assertEqual(db.get_setting("telegram_auth"), "auth_required")
        finally:
            profile.rmdir()

    def test_http_mcp_and_import_flags_are_per_task(self):
        t = main.api_task_create(None, {"title": "С телефона", "telegram_notify": True}, {}, self.pid)
        self.assertEqual(t["telegram_notify"], 1)
        main.api_task_update(None, {"telegram_notify": False}, {}, t["id"])
        self.assertEqual(db.get_task(t["id"])["telegram_notify"], 0)
        with self.assertRaises(main.HttpError):
            main.api_task_update(None, {"telegram_notify": "false"}, {}, t["id"])
        with self.assertRaises(ValueError):
            pl._mcp("create_task", {"title": "Некорректный флаг", "telegram_notify": "false"}, None, self.pid)
        for flag in (True, False):
            result = pl._mcp("create_task", {"title": "Из чата", "telegram_notify": flag}, None, self.pid)
            created = db.one("SELECT * FROM tasks ORDER BY id DESC LIMIT 1")
            self.assertEqual(created["telegram_notify"], int(flag), result)
        pl._mcp("update_task", {"task_id": created["id"], "telegram_notify": True}, None, self.pid)
        self.assertEqual(db.get_task(created["id"])["telegram_notify"], 1)
        cid = db.column_by_kind(self.pid, "backlog")["id"]
        main.api_import_commit(None, {"column_id": cid, "telegram_notify": True,
                                     "tasks": [{"title": "Импорт 1"}, {"title": "Импорт 2", "telegram_notify": False}]}, {}, self.pid)
        rows = db.q("SELECT telegram_notify FROM tasks WHERE title LIKE 'Импорт%' ORDER BY id")
        self.assertEqual([r["telegram_notify"] for r in rows], [1, 0])
        main.api_settings(None, {"telegram_auth": "ready"}, {})
        self.assertEqual(db.get_setting("telegram_auth"), "unconfigured")

    def test_chat_passes_requesting_device_setting_for_both_providers(self):
        with patch.object(pl, "agent_run", side_effect=lambda **kw: fixture.FakeRun(**kw)):
            for agent in ("claude", "codex"):
                with patch.object(pl, "AGENT", agent), patch.object(db, "AGENT", agent):
                    for flag in (False, True):
                        cid = db.create_chat(self.pid)
                        pl.chat.send(cid, "Создай задачу", telegram_notify=flag)
                        run = fixture.FakeRun.instances[-1]
                        self.assertIn("telegram_notify=" + str(flag).lower(), run.prompt)
                        self.assertIn("telegram_notify", run.system_prompt)
        self.assertEqual(db.get_task(self.tid)["telegram_notify"], 0)
        self.assertNotIn("Отправить", pl.task_prompt(db.get_task(self.tid), {"name": "x", "path": "x"}))

    def test_unsubscribed_and_later_device_changes_do_not_send(self):
        pl.finish_task(self.tid, "Готово", "Отчёт")
        self.assertIsNone(tg.delivery(self.tid))
        with patch.object(self.service, "_call") as call:
            self.service.tick()
            call.assert_not_called()

    def test_snapshot_duplicates_worker_waits_for_agent_and_opt_out(self):
        source = fixture.SANDBOX / "снимок.png"
        source.write_bytes(b"prepared-image")
        first = self.finish([str(source)])
        source.unlink()
        row = db.one("SELECT * FROM telegram_deliveries WHERE id=?", (first["id"],))
        parts = json.loads(row["payload"])
        self.assertFalse(Path(parts[1]["files"][0]).is_absolute())
        self.assertEqual((tg.ROOT / parts[1]["files"][0]).read_bytes(), b"prepared-image")
        pl.finish_task(self.tid, "Готово", "Отчёт", telegram_files=[str(source)])
        pl.finish_task(self.tid, "Уточнённый итог", "Отчёт")
        self.assertEqual(len(db.q("SELECT * FROM telegram_deliveries WHERE task_id=?", (self.tid,))), 1)
        db.set_setting("telegram_auth", "ready")
        self.service.running_tasks = lambda: {self.tid}
        with patch.object(self.service, "_call", return_value={"status": "sent"}) as call:
            self.service.tick()
            call.assert_not_called()
            self.service.running_tasks = lambda: set()
            db.update_task(self.tid, telegram_notify=False)
            self.service.tick()
            call.assert_not_called()
            db.update_task(self.tid, telegram_notify=True)
            self.service.tick()
            self.service.tick()
            self.assertEqual(call.call_count, 2)
        self.assertEqual(tg.delivery(self.tid)["state"], "sent")
        restarted = tg.Telegram()
        restarted.recover()
        with patch.object(restarted, "_call") as call:
            restarted.tick()
            call.assert_not_called()

    def test_partial_failure_reauth_retry_skips_sent_parts(self):
        source = fixture.SANDBOX / "artifact.txt"
        source.write_text("result", encoding="utf-8")
        self.finish([str(source)])
        db.set_setting("telegram_auth", "ready")
        with patch.object(self.service, "_call", side_effect=[{"status": "sent"}, {"status": "auth_required", "error": "Войдите заново"}]) as call:
            self.service.tick()
            self.assertEqual(call.call_count, 2)
        self.assertEqual(db.get_task(self.tid)["status"], "done")
        self.assertEqual(db.get_setting("telegram_auth"), "auth_required")
        self.assertEqual(tg.delivery(self.tid)["next_part"], 1)
        self.assertEqual(tg.delivery(self.tid)["state"], "error")
        db.set_setting("telegram_auth", "ready")
        self.service.retry(self.tid)
        with patch.object(self.service, "_call", return_value={"status": "sent"}) as call:
            self.service.tick()
            call.assert_called_once()
            self.assertEqual(len(call.call_args.args[1]["files"]), 1)
        self.assertEqual(tg.delivery(self.tid)["state"], "sent")

    def test_unknown_outcome_does_not_auto_repeat_after_restart(self):
        row = self.finish()
        db.set_setting("telegram_auth", "ready")
        db.ex("UPDATE telegram_deliveries SET state='sending' WHERE id=?", (row["id"],))
        self.service.recover()
        self.assertEqual(tg.delivery(self.tid)["state"], "uncertain")
        with patch.object(self.service, "_call") as call:
            self.service.tick()
            call.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Избранное"):
            self.service.retry(self.tid)
        self.service.retry(self.tid, "received")
        db.set_setting("telegram_auth", "ready")
        with patch.object(self.service, "_call") as call:
            self.service.tick()
            call.assert_not_called()
        self.assertEqual(tg.delivery(self.tid)["state"], "sent")

    def test_crash_before_completion_discards_uncommitted_delivery(self):
        db.update_task(self.tid, telegram_notify=True, status="running")
        tg.prepare(db.get_task(self.tid), "Ещё не закрыто", staged=True)
        self.assertIsNone(tg.delivery(self.tid))
        self.service.recover()
        self.assertFalse(db.q("SELECT * FROM telegram_deliveries WHERE task_id=?", (self.tid,)))
        db.update_task(self.tid, last_run_at=db.now())
        pl.finish_task(self.tid, "Теперь закрыто", "Отчёт")
        self.assertEqual(len(db.q("SELECT * FROM telegram_deliveries WHERE task_id=?", (self.tid,))), 1)

    def test_enabling_completed_task_is_explicit_and_does_not_resend_sent(self):
        pl.finish_task(self.tid, "Готово", "Отчёт")
        self.assertIsNone(tg.delivery(self.tid))
        main.api_task_update(None, {"telegram_notify": True}, {}, self.tid)
        self.assertEqual(tg.delivery(self.tid)["state"], "pending")
        db.set_setting("telegram_auth", "ready")
        with patch.object(self.service, "_call", return_value={"status": "sent"}) as call:
            self.service.tick()
            main.api_task_update(None, {"telegram_notify": False}, {}, self.tid)
            main.api_task_update(None, {"telegram_notify": True}, {}, self.tid)
            self.service.tick()
            call.assert_called_once()

    def test_new_run_gets_new_delivery_and_stop_preserves_pending(self):
        first = self.finish()
        db.update_task(self.tid, status="running", last_run_at=db.now())
        pl.finish_task(self.tid, "Вторая версия", "Отчёт")
        self.assertNotEqual(first["id"], tg.delivery(self.tid)["id"])
        self.service.stopping = lambda: True
        with patch.object(self.service, "_call") as call:
            self.service.tick()
            call.assert_not_called()
        self.assertEqual(tg.delivery(self.tid)["state"], "pending")
        with patch.object(pl.telegram, "busy", True):
            self.assertIn(pl.telegram, pl.active_runs())

    def test_unconfigured_and_missing_attachments_keep_task_done(self):
        self.finish([str(fixture.SANDBOX / "missing.png")])
        self.assertEqual(tg.delivery(self.tid)["state"], "error")
        self.service.retry(self.tid)
        db.set_setting("telegram_auth", "ready")
        with patch.object(self.service, "_call") as call:
            self.service.tick()
            call.assert_not_called()
        self.assertEqual(tg.delivery(self.tid)["state"], "error")
        self.assertEqual(db.get_task(self.tid)["status"], "done")
        other = db.create_task(self.pid, "Без настройки", telegram_notify=True)
        pl.finish_task(other, "Готово", "Отчёт")
        db.set_setting("telegram_auth", "unconfigured")
        with patch.object(self.service, "_call") as call:
            self.service.tick()
            call.assert_not_called()
        self.assertEqual(tg.delivery(other)["state"], "error")

    def test_snapshot_permission_error_does_not_fail_task(self):
        with patch.object(tg.Path, "mkdir", side_effect=PermissionError("private local path")):
            self.finish()
        self.assertEqual(db.get_task(self.tid)["status"], "done")
        self.assertEqual(tg.delivery(self.tid)["state"], "error")
        self.assertNotIn("private", tg.delivery(self.tid)["error"])

    def test_helper_errors_hide_raw_output_and_timeout_is_uncertain(self):
        ROOT = tg.ROOT
        with patch.object(tg.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (b"private browser error, account and token", None)
            popen.return_value.poll.return_value = 1
            result = self.service._call("send", {"text": "Итог", "files": []})
            self.assertEqual(result["status"], "uncertain")
            self.assertNotIn("private", result["error"])
            self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertFalse((ROOT / "job.json").exists())
        self.service.cancelled = False
        with patch.object(tg.subprocess, "Popen") as popen:
            popen.return_value.communicate.side_effect = subprocess.TimeoutExpired("browser", 330)
            popen.return_value.poll.return_value = 1
            self.assertEqual(self.service._call("send", {"text": "Итог", "files": []})["status"], "uncertain")

    def test_http_telegram_routes_and_validation(self):
        srv = main.Server(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            with patch.object(main.telegram, "start") as start:
                for operation in ("check", "login", "test"):
                    req = urllib.request.Request(base + "/api/telegram/" + operation, data=b"{}", headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as r:
                        self.assertIn("auth", json.load(r))
                    start.assert_called_with(operation)
            with urllib.request.urlopen(base + "/api/telegram") as r:
                self.assertNotIn("account", json.load(r))
            with urllib.request.urlopen(base + "/api/telegram/instructions") as r:
                self.assertIn("python -m pip install playwright", r.read().decode("utf-8"))
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/telegram/qr")
            self.assertEqual(error.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


class TelegramTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("telegram_web", fixture.SOURCE / "tools/telegram_web.py")
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def test_send_failure_is_uncertain_only_after_submit(self):
        from unittest.mock import MagicMock
        page = MagicMock()
        ctx = MagicMock()
        ctx.pages = [page]
        pw = MagicMock()
        pw.chromium.launch_persistent_context.return_value = ctx
        manager = MagicMock()
        manager.__enter__.return_value = pw
        fake = SimpleNamespace(sync_playwright=lambda: manager)
        args = SimpleNamespace(data_dir=fixture.SANDBOX / "helper-profile", show=False, operation="test")
        with patch.dict(sys.modules, {"playwright.sync_api": fake}), patch.object(self.helper, "open_saved"):
            for submitted in (False, True):
                def fail(page, part, before_send):
                    if submitted:
                        before_send()
                    raise RuntimeError("personal browser diagnostics")
                with patch.object(self.helper, "send", side_effect=fail):
                    result = self.helper.run(args)
                self.assertEqual(result["status"], "uncertain" if submitted else "error")
                self.assertNotIn("personal", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
