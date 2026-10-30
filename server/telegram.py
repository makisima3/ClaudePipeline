"""Локальная очередь Telegram: снимки материалов, отдельные ошибки и один браузер за раз."""
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import db
from runner import NO_WINDOW

ROOT = db.DATA / "telegram"
RESULTS = ROOT / "results"
HELPER = db.ROOT / "tools" / "telegram_web.py"
UNCERTAIN = "Подтверждение Telegram не получено. Проверьте Избранное перед повтором."


def clear_qr():
    for name in ("qr.png", "qr-next.png"):
        try:
            (ROOT / name).unlink(missing_ok=True)
        except OSError:
            pass


def delivery(tid):
    """Последняя доставка без локальных путей и содержимого вложений."""
    row = db.one("""SELECT id, state, next_part, error, created_at, updated_at, payload
                     FROM telegram_deliveries WHERE task_id=? AND state<>'prepared' ORDER BY id DESC LIMIT 1""", (tid,))
    if row:
        parts = json.loads(row.pop("payload"))
        row["parts"] = len(parts)
        row["marker"] = parts[row["next_part"]]["text"].split("\n")[-1] if row["next_part"] < len(parts) else None
    return row


def prepare(t, summary, text=None, files=None, staged=False):
    """Скопировать материалы до выхода агента; повторный finish того же запуска безвреден."""
    key = str(t["last_run_at"] or t["created_at"])
    existing = db.one("SELECT * FROM telegram_deliveries WHERE task_id=? AND run_key=?", (t["id"], key))
    repair = existing and existing["state"] == "error" and existing["next_part"] == 0 and json.loads(existing["payload"])[0].get("error")
    if existing and not repair:
        return
    token = uuid.uuid4().hex
    folder = RESULTS / token
    header = f"#{t['id']} · {t['title']}\n{t['project_name']}\n\n"
    body = (text or summary or "Задача выполнена.").strip()
    parts = [{"text": (header + body)[:3500], "files": []}]
    error = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not isinstance(files or [], list) or len(files or []) > 10:
            raise ValueError("Можно подготовить до 10 вложений.")
        total = 0
        for i, name in enumerate(files or []):
            f = Path(name)
            if not f.is_absolute():
                f = Path(t["project_path"]) / f
            if not f.is_file():
                raise ValueError(f"Вложение {i + 1} недоступно. Подготовьте материалы заново.")
            size = f.stat().st_size
            total += size
            if size > 100 * 1024**2 or total > 300 * 1024**2:
                raise ValueError("Лимит материалов: 100 МБ на файл, 300 МБ суммарно.")
            target = folder / f"{i + 1}-{f.name}"
            shutil.copyfile(f, target)
            parts.append({"text": f"#{t['id']} · {f.name}"[:800], "files": [target.relative_to(ROOT).as_posix()]})
    except (OSError, ValueError, TypeError):
        # Результат задачи остаётся успешным даже при потере исходного файла.
        error = "Не удалось сохранить вложения. Проверьте файлы и подготовьте материалы заново."
        parts = [{"text": "", "files": [], "error": error}]
    for i, part in enumerate(parts):
        part["text"] += f"\n\n[AP:{token}:{i + 1}]"
    if repair:
        db.ex("DELETE FROM telegram_deliveries WHERE id=?", (existing["id"],))
    db.ex("""INSERT OR IGNORE INTO telegram_deliveries(task_id, run_key, payload, state, error, created_at, updated_at)
             VALUES(?,?,?,?,?,?,?)""", (t["id"], key, json.dumps(parts, ensure_ascii=False),
                                       "prepared" if staged else "error" if error else "pending", error, db.now(), db.now()))


class Telegram:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.busy = False
        self.operation = None
        self.change = lambda pid=None: None
        self.stopping = lambda: False
        self.running_tasks = lambda: set()
        self.cancelled = False

    def state(self):
        return {"auth": db.get_setting("telegram_auth"), "error": db.get_setting("telegram_error"),
                "test_at": float(db.get_setting("telegram_test_at") or 0),
                "busy": self.busy, "operation": self.operation,
                "qr": self.operation == "login" and (ROOT / "qr.png").is_file()}

    def auth(self, status, error=""):
        db.set_setting("telegram_auth", status)
        db.set_setting("telegram_error", error)
        self.change()

    def recover(self):
        # Черновик finish до фиксации статуса done не считается завершением задачи.
        db.ex("DELETE FROM telegram_deliveries WHERE state='prepared'")
        # После аварии неизвестно, успел ли Telegram принять текущую часть. Не повторяем её сами.
        db.ex("UPDATE telegram_deliveries SET state='uncertain', error=?, updated_at=? WHERE state='sending'",
              (UNCERTAIN, db.now()))
        clear_qr()
        if db.get_setting("telegram_auth") != "unconfigured" or (ROOT / "profile").is_dir():
            self.auth("unknown", "Нужно проверить сохранённую сессию.")

    def _call(self, operation, part=None):
        job = ROOT / "job.json"
        started = False
        args = [sys.executable, str(HELPER), operation, "--data-dir", str(ROOT)]
        if part is not None:
            args += ["--job", str(job)]
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            if part is not None:
                job.write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
            if self.cancelled or self.stopping():
                return {"status": "error", "error": "Операция отменена."}
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            creationflags=NO_WINDOW, start_new_session=os.name != "nt")
            started = True
            out, _ = self.process.communicate(timeout=330)
            result = json.loads(out.decode("utf-8"))
            if not isinstance(result, dict) or result.get("status") not in ("ready", "sent", "auth_required", "error", "uncertain"):
                raise ValueError()
            return result
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.kill()
            uncertain = operation == "send" and started
            return {"status": "uncertain" if uncertain else "error",
                    "error": UNCERTAIN if uncertain else "Не удалось запустить Telegram. Проверьте компоненты и доступ к data/telegram/."}
        finally:
            self.process = None
            try:
                job.unlink(missing_ok=True)
            except OSError:
                pass

    def kill(self):
        self.cancelled = True
        p = self.process
        if p and p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            else:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def start(self, operation):
        """Настройка выполняется асинхронно, HTTP не ждёт QR или загрузки Telegram."""
        if operation not in ("check", "login", "test"):
            raise ValueError("Неизвестная операция Telegram.")
        if self.stopping() or not self.lock.acquire(blocking=False):
            raise ValueError("Telegram занят или трекер перезапускается.")
        self.busy, self.operation, self.cancelled = True, operation, False
        try:
            clear_qr()
            self.auth("checking" if operation != "login" else "login")
            threading.Thread(target=self._setup, args=(operation,), daemon=True).start()
        except Exception:
            self.busy, self.operation = False, None
            self.lock.release()
            raise ValueError("Не удалось начать операцию Telegram.") from None

    def _setup(self, operation):
        try:
            result = self._call(operation)
            status = result["status"]
            if operation == "test" and status == "sent":
                db.set_setting("telegram_test_at", db.now())
            self.auth("ready" if status in ("ready", "sent") else status, result.get("error", ""))
        except Exception:
            self.auth("error", "Операция Telegram прервана. Проверьте компоненты и доступ к data/telegram/.")
        finally:
            clear_qr()
            self.busy, self.operation = False, None
            self.lock.release()
            self.change()

    def retry(self, tid, resolution=None):
        with db._lock:
            t = db.get_task(tid)
            if not t or not t["telegram_notify"]:
                raise ValueError("Сначала включите отправку в Telegram в карточке задачи.")
            row = delivery(tid)
            if not row or row["state"] not in ("error", "uncertain"):
                raise ValueError("Нет доставки, доступной для повтора.")
            if row["state"] == "uncertain" and resolution not in ("received", "resend"):
                raise ValueError("Сначала проверьте Избранное: часть получена или её нужно повторить?")
            if row["state"] != "uncertain" and resolution is not None:
                raise ValueError("Для обычной ошибки используйте повтор доставки без подтверждения части.")
            if resolution == "received":
                db.ex("UPDATE telegram_deliveries SET next_part=next_part+1 WHERE id=?", (row["id"],))
            db.ex("UPDATE telegram_deliveries SET state='pending', error=NULL, updated_at=? WHERE id=?",
                  (db.now(), row["id"]))
        self.change(t["project_id"])

    def tick(self):
        if self.stopping() or not self.lock.acquire(blocking=False):
            return
        try:
            rows = db.q("""SELECT d.* FROM telegram_deliveries d JOIN tasks t ON t.id=d.task_id
                           WHERE d.state='pending' AND t.status='done' AND t.telegram_notify=1 ORDER BY d.id""")
            row = next((r for r in rows if r["task_id"] not in self.running_tasks()), None)
            if not row:
                return
            self.busy, self.operation, self.cancelled = True, "send", False
            try:
                self._deliver(row)
            except Exception:
                current = db.one("SELECT state FROM telegram_deliveries WHERE id=?", (row["id"],))
                self._status(row["id"], "uncertain" if current and current["state"] == "sending" else "error",
                             "Доставка прервана. Проверьте Избранное и сохранённые материалы перед повтором.")
        finally:
            self.busy, self.operation = False, None
            self.lock.release()

    def _deliver(self, row):
        tid = row["task_id"]
        parts = json.loads(row["payload"])
        for i in range(row["next_part"], len(parts)):
            if self.stopping() or self.cancelled:
                return
            t = db.get_task(tid)
            if not t or not t["telegram_notify"] or t["status"] != "done":
                return
            part = parts[i]
            if part.get("error"):
                self._status(row["id"], "error", part["error"])
                return
            part = {**part, "files": [str(ROOT / f) for f in part["files"]]}
            if any(not Path(f).is_file() for f in part["files"]):
                self._status(row["id"], "error", "Сохранённое вложение удалено. Подготовьте материалы заново.")
                return
            if db.get_setting("telegram_auth") != "ready":
                self._status(row["id"], "error", "Подключите Telegram в настройках, затем повторите доставку.")
                return
            self._status(row["id"], "sending")
            result = self._call("send", part)
            status = result["status"]
            if status != "sent":
                if status == "auth_required":
                    self.auth(status, result.get("error", "Требуется повторный вход."))
                self._status(row["id"], "uncertain" if status == "uncertain" else "error",
                             result.get("error", "Telegram не подтвердил доставку."))
                return
            db.ex("UPDATE telegram_deliveries SET next_part=?, state='pending', updated_at=? WHERE id=?",
                  (i + 1, db.now(), row["id"]))
        self._status(row["id"], "sent")
        t = db.get_task(tid)
        self.change(t["project_id"] if t else None)

    def _status(self, did, state, error=None):
        db.ex("UPDATE telegram_deliveries SET state=?, error=?, updated_at=? WHERE id=?", (state, error, db.now(), did))
        row = db.one("SELECT t.project_id FROM tasks t JOIN telegram_deliveries d ON d.task_id=t.id WHERE d.id=?", (did,))
        self.change(row["project_id"] if row else None)

    def loop(self):
        while True:
            try:
                if not self.stopping() and not self.busy and db.get_setting("telegram_auth") == "unknown":
                    self.start("check")
                else:
                    self.tick()
            except Exception:
                # Содержимое профиля/сообщений и сырые ошибки браузера не попадают в консоль.
                pass
            time.sleep(1)


telegram = Telegram()
