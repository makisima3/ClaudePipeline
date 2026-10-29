"""Обновление трекера с GitHub: фоновая проверка origin/main и fast-forward pull с перезапуском."""
import os
import re
import subprocess
import threading
import time

import db
import pipeline as pl
from runner import NO_WINDOW

REMOTE, BRANCH = "origin", "main"
UPSTREAM = f"{REMOTE}/{BRANCH}"
MAX_COMMITS = 50  # сколько заголовков новых коммитов показывать
# git не должен ничего спрашивать: ни пароль в консоли, ни окно Git Credential Manager
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}

HINTS = [  # понятная причина по выводу git
    (r"not a git repository", "Папка трекера — не git-репозиторий"),
    (r"does not appear to be a git repository|No such remote", f"Удалённый репозиторий {REMOTE} не найден или не настроен"),
    (r"couldn't find remote ref|unknown revision|bad revision|ambiguous argument", f"В {REMOTE} нет ветки {BRANCH}"),
    (r"Could not resolve host|Failed to connect|unable to access|timed out|Connection (refused|reset)|network",
     "Нет связи с GitHub"),
    (r"Authentication failed|could not read Username|Permission denied|Repository not found|403|401",
     "Нет доступа к репозиторию (проверь вход в git/gh)"),
    (r"dubious ownership", "git не доверяет папке трекера (safe.directory)"),
]


class GitError(Exception):
    pass


def git(*args, timeout=20):
    """Вызов git в папке трекера без окна и без вопросов; ошибка — GitError с понятным текстом."""
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=db.ROOT, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=GIT_ENV, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise GitError("git не найден (нужен в PATH)")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} не ответил за {timeout} с")
    if r.returncode:
        out = (r.stderr or r.stdout).strip()
        hint = next((h for rx, h in HINTS if re.search(rx, out, re.I)), f"git {args[0]} завершился с ошибкой")
        raise GitError(f"{hint}: {out.splitlines()[-1] if out else 'код ' + str(r.returncode)}"[:500])
    return r.stdout


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    return one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many


class Updater:
    def __init__(self):
        self.lock = threading.Lock()  # одна проверка или обновление за раз
        self.wake = threading.Event()
        self.st = {"checked_at": None, "checking": False, "error": None, "branch": None, "head": None,
                   "remote": None, "behind": 0, "ahead": 0, "commits": [], "updating": False,
                   "update_error": None, "updated": None}

    def state(self):
        return dict(self.st)

    def _set(self, **kw):
        self.st.update(kw)
        pl.hub.send("update", update=self.state())

    def loop(self):
        """Проверка при старте и потом раз в update_check_minutes (0 — только вручную)."""
        last = 0
        while True:
            try:
                minutes = float(db.get_setting("update_check_minutes") or 0)
            except ValueError:
                minutes = 0
            if minutes > 0 and time.time() - last >= minutes * 60 and not pl.restart.since:
                last = time.time()
                self.check()
            self.wake.wait(30)
            self.wake.clear()

    def check(self, fetch=True):
        """git fetch + сравнение HEAD с origin/main. Ошибки не бросает, а кладёт в состояние."""
        with self.lock:
            self._set(checking=True)
            try:
                if fetch:
                    git("fetch", "--quiet", REMOTE, BRANCH, timeout=90)
                branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
                head = git("rev-parse", "--short", "HEAD").strip()
                remote = git("rev-parse", "--short", UPSTREAM).strip()
                ahead, behind = map(int, git("rev-list", "--left-right", "--count", f"HEAD...{UPSTREAM}").split())
                log = git("log", f"-{MAX_COMMITS}", "--format=%h%x1f%s%x1f%an%x1f%ct", f"HEAD..{UPSTREAM}") if behind else ""
                commits = [dict(zip(("hash", "subject", "author", "ts"), ln.split("\x1f"))) for ln in log.splitlines() if ln]
                for c in commits:
                    c["ts"] = float(c["ts"])
                blocked = self.st["update_error"]
                if fetch and blocked:  # прошлый отказ: вдруг причину уже устранили
                    blocked = self.blockers() if behind else None
                self._set(checking=False, checked_at=time.time(), error=None, branch=branch, head=head,
                          remote=remote, ahead=ahead, behind=behind, commits=commits, update_error=blocked)
            except GitError as e:
                self._set(checking=False, checked_at=time.time(), error=str(e))
            except Exception as e:
                self._set(checking=False, checked_at=time.time(), error=f"Не удалось проверить обновления: {e}")
        return self.state()

    def blockers(self):
        """Почему обновлять нельзя (None — можно). Локальная работа не должна потеряться."""
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch != BRANCH:
            return f"Сейчас выбрана ветка «{branch}», а обновление идёт из {BRANCH}. Переключись: git checkout {BRANCH}."
        dirty = [ln for ln in git("status", "--porcelain", "--untracked-files=all").splitlines() if ln.strip()]
        if dirty:
            files = "\n".join(f"  {ln}" for ln in dirty[:20]) + (f"\n  … и ещё {len(dirty) - 20}" if len(dirty) > 20 else "")
            return (f"В папке трекера есть незакоммиченные изменения ({len(dirty)} {plural(len(dirty), 'файл', 'файла', 'файлов')}):\n"
                    f"{files}\nЗакоммить их (или откати ненужное, а временные файлы добавь в .gitignore) и обнови снова.")
        ahead = int(git("rev-list", "--count", f"{UPSTREAM}..HEAD").strip())
        if ahead:
            return (f"Есть локальные коммиты, которых нет в {UPSTREAM}: {ahead}. "
                    f"Отправь их (git push) или перенеси поверх новой версии (git pull --rebase) и обнови снова.")
        return None

    def update(self):
        """git pull --ff-only и отложенный перезапуск. Возвращает (ok, текст)."""
        if pl.restart.since:
            return False, "Трекер уже перезапускается"
        if not self.lock.acquire(blocking=False):
            return False, "Уже идёт проверка или обновление, попробуй через минуту"
        try:
            self._set(updating=True, update_error=None)
            git("fetch", "--quiet", REMOTE, BRANCH, timeout=90)
            reason = self.blockers()
            if reason:
                raise GitError(reason)
            old = git("rev-parse", "--short", "HEAD").strip()
            if not int(git("rev-list", "--count", f"HEAD..{UPSTREAM}").strip()):
                raise GitError("Обновлять нечего: уже последняя версия")
            git("pull", "--ff-only", "--quiet", REMOTE, BRANCH, timeout=180)
            # код на диске уже новый: дальше ничто не должно помешать запросить перезапуск
            try:
                new = git("rev-parse", "--short", "HEAD").strip()
            except GitError:
                new = "?"
            broken = self._compile_errors()
            if broken:  # с синтаксической ошибкой новый экземпляр не поднимется — остаёмся на старом коде в памяти
                pl.log_restart(f"обновление {old} -> {new} не компилируется, перезапуск отменён: {broken}")
                self.st["updated"] = {"from": old, "to": new, "at": time.time(), "broken": broken}
                raise GitError(f"Код обновлён до {new}, но не компилируется, перезапуск отменён: {broken}")
            self._set(updating=False, updated={"from": old, "to": new, "at": time.time()})
            pl.restart.request(f"обновление {old} -> {new}")  # запишет в restart.log
            return True, f"Обновлено до {new}, перезапуск после завершения текущих задач"
        except GitError as e:
            self._set(updating=False, update_error=str(e))
            return False, str(e)
        except Exception as e:
            self._set(updating=False, update_error=f"Не удалось обновить: {e}")
            return False, f"Не удалось обновить: {e}"
        finally:
            self.lock.release()
            threading.Thread(target=self.check, args=(False,), daemon=True).start()  # обновить «позади/впереди»

    @staticmethod
    def _compile_errors():
        for f in sorted((db.ROOT / "server").glob("*.py")):
            try:
                compile(f.read_text(encoding="utf-8"), str(f), "exec")
            except (SyntaxError, ValueError) as e:
                return f"{f.name}: {e}"
        return None


updater = Updater()
