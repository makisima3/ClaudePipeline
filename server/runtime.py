"""Выбор исполнителя и блокировка рабочей базы до её открытия."""
import atexit
import os
import sys
import time
from pathlib import Path

AGENT = os.environ.get("PIPELINE_AGENT", "claude").lower()
if "--agent" in sys.argv:
    AGENT = sys.argv[sys.argv.index("--agent") + 1].lower()
if AGENT not in ("claude", "codex"):
    raise SystemExit("Исполнитель должен быть claude или codex")
os.environ["PIPELINE_AGENT"] = AGENT
NAME = "Codex" if AGENT == "codex" else "Claude"
_instance = None


def acquire():
    """Одна блокировка на папку data, независимо от порта и исполнителя."""
    global _instance
    folder = Path(__file__).resolve().parent.parent / "data"
    folder.mkdir(exist_ok=True)
    _instance = open(folder / "server.lock", "a+b")
    _instance.seek(0, 2)
    if not _instance.tell():
        _instance.write(b"0")
        _instance.flush()
    deadline = time.monotonic() + (90 if "--after" in sys.argv else 0)
    while True:
        try:
            _instance.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(_instance.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(_instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise SystemExit("Трекер с этой базой уже запущен. Закрой его перед сменой исполнителя.")
            time.sleep(0.25)
    atexit.register(_instance.close)
