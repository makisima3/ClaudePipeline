"""Проверка предпосылок коммита/push без изменения репозитория и проверки права записи."""
import os
import re
import subprocess
from urllib.parse import urlsplit


class CheckError(Exception):
    pass


def run(cwd, *args, discard=False):
    """Только чтение; сырой вывод ошибок и gh никогда не уходит клиенту или в лог."""
    try:
        result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL if discard else subprocess.PIPE,
                                stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=8,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
                                     "GH_PROMPT_DISABLED": "1", "GIT_OPTIONAL_LOCKS": "0"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise CheckError(f"{args[0]} не найден в PATH. После установки перезапусти трекер.") from None
    except subprocess.TimeoutExpired:
        raise CheckError("Проверка не ответила за 8 секунд. Повтори позже.") from None
    except OSError:
        raise CheckError("Не удалось запустить проверку. Проверь доступность папки проекта.") from None
    return result.returncode, (result.stdout or "").strip()


def check(path, mode="commit_push", auth="existing"):
    """Структурированный результат для настроек и будущего мастера первого запуска."""
    checks = []
    result = {"mode": mode, "auth": auth, "checks": checks, "write_access": "not_checked"}

    def add(key, status, message):
        checks.append({"key": key, "status": status, "message": message})

    def done():
        result["status"] = ("needs_setup" if any(c["status"] == "error" for c in checks) else
                            "review" if any(c["status"] == "warning" for c in checks) else "local_ready")
        return result

    def git(*args):
        return run(path, "git", *args)

    def config(key, *options):
        code, value = git("config", *options, "--get", key)
        if code not in (0, 1):
            raise CheckError("Не удалось прочитать Git-конфигурацию. Проверь её в терминале проекта.")
        return value

    if mode == "off":
        add("mode", "ok", "Git выключен: Git и авторизация для выполнения задач не нужны.")
        return done()
    try:
        code, _ = git("--version")
        if code:
            raise CheckError("Git не запускается. Установи Git и проверь git --version в терминале.")
        add("git", "ok", "Git доступен серверу трекера.")
        code, inside = git("rev-parse", "--is-inside-work-tree")
        if code or inside != "true":
            add("repository", "error", "Папка проекта не является доступным рабочим Git-репозиторием. "
                "Клонируй свой репозиторий или выполни git init; при ошибке доступа проверь git status в этой папке.")
            return done()
        add("repository", "ok", "Рабочий Git-репозиторий проекта найден.")
        for field in ("name", "email"):
            value = config("user." + field)
            add("author_" + field, "ok" if value else "error",
                f"user.{field} настроен." if value else
                f'Не задан user.{field}. В терминале проекта: git config user.{field} "Твоё значение". '
                "Для всех проектов можно добавить --global.")
        if mode == "commit":
            add("auth", "ok", "Для локальных коммитов remote и GitHub-авторизация не нужны.")
            return done()
        code, branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
        if code or not branch:
            add("branch", "error", "HEAD не указывает на ветку (detached HEAD). Выбери рабочую ветку через git switch.")
            return done()
        result["branch"] = branch
        add("branch", "ok", f"Текущая ветка: {branch}.")
        code, names = git("remote")
        if code:
            raise CheckError("Не удалось прочитать remote. Проверь git remote в терминале проекта.")
        remotes = names.splitlines()
        fetch_remote = config(f"branch.{branch}.remote")
        pull_remote = fetch_remote or (remotes[0] if len(remotes) == 1 else "origin")
        remote = config(f"branch.{branch}.pushRemote") or config("remote.pushDefault") or fetch_remote
        remote = remote or (remotes[0] if len(remotes) == 1 else "origin")
        if remote not in remotes:
            add("remote", "error", "Remote для push не настроен или задан напрямую адресом. "
                "Добавь именованный remote своего репозитория/форка (git remote add origin АДРЕС) "
                "и выбери его для ветки. Существующие адреса проверяй в терминале, без токенов в URL.")
            return done()
        result["remote"] = remote
        code, urls = git("remote", "get-url", "--push", "--all", remote)
        if code or not urls:
            add("remote", "error", "У выбранного remote нет доступного адреса push. Проверь git remote -v в терминале.")
            return done()
        add("remote", "ok", f"Remote для push: {remote}. Используется адрес push с учётом Git-конфигурации.")
        code, refspec = git("config", "--get-all", f"remote.{remote}.push")
        if code not in (0, 1):
            raise CheckError("Не удалось прочитать правила push в Git-конфигурации.")
        push_mode = config("push.default") or "simple"
        merge = config(f"branch.{branch}.merge")
        has_upstream = bool(fetch_remote and merge)
        auto = config("push.autoSetupRemote", "--bool") == "true"
        if refspec or config(f"remote.{remote}.mirror", "--bool") == "true":
            add("target", "warning", "Настроены особые правила remote.push или mirror. "
                "Проверь направление и отсутствие принудительного обновления вручную; конфигурация не менялась.")
        elif push_mode in ("matching", "nothing") or push_mode not in ("simple", "current", "upstream"):
            add("target", "warning", "push.default не задаёт обычный push одной текущей ветки. "
                "Проверь правила push в терминале проекта.")
        elif push_mode == "upstream" and remote != pull_remote:
            add("target", "error", "При push.default=upstream remote для push должен совпадать с upstream ветки.")
        elif push_mode != "current" and not (push_mode == "simple" and remote != pull_remote) and not has_upstream and not auto:
            add("target", "error", "У ветки нет upstream для обычного git push. Для существующей удалённой ветки: "
                "git branch --set-upstream-to=REMOTE/ВЕТКА. Для новой ветки первый git push -u REMOTE ВЕТКА "
                "выполни сам, когда будешь готов отправить реальные изменения.")
        elif push_mode == "simple" and remote == pull_remote and has_upstream and merge != f"refs/heads/{branch}":
            add("target", "error", "Имя upstream отличается от текущей ветки: git push в режиме simple откажет. "
                "Выбери подходящую ветку или настрой направление push вручную.")
        else:
            target = merge.removeprefix("refs/heads/") if push_mode == "upstream" and merge else branch
            result["target_branch"] = target
            add("target", "ok", f"Направление обычного git push: {remote} → {target}.")
        if len(urls.splitlines()) != 1:
            add("urls", "warning", "У remote несколько адресов push. Проверь каждый в терминале; авторизация здесь не проверяется.")
        elif auth == "gh":
            gh_status(path, urls, add)
        else:
            add("auth", "ok", "Используется существующий SSH/HTTPS или другой доступ Git. gh не требуется. "
                "Работоспособность авторизации здесь не проверяется.")
    except CheckError as e:
        add("check", "error", str(e))
    return done()


def gh_status(path, url, add):
    """Проверка выбранного gh для HTTPS-узла без чтения или показа токена."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        host = ""
        parts = None
    if not parts or parts.scheme != "https" or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        add("auth", "warning", "У remote не обычный HTTPS-адрес. Для SSH и других способов gh не требуется; "
            "выбери проверку существующего доступа Git.")
        return
    try:
        code, _ = run(path, "gh", "auth", "status", "--active", "--hostname", host, discard=True)
    except CheckError as e:
        add("auth", "error", f"GitHub CLI: {e} Можно использовать существующий доступ Git без gh.")
        return
    add("auth", "ok" if code == 0 else "warning",
        "Вход gh для HTTPS-узла подтверждён. Вход не гарантирует право push в этот репозиторий или ветку." if code == 0 else
        "Вход gh не подтверждён: проверь gh auth status в терминале. При необходимости сам выполни "
        "gh auth login, затем gh auth setup-git для HTTPS. Возможна также ошибка сети.")
    code, helper = run(path, "git", "config", "--get-urlmatch", "credential.helper", url)
    if code or not re.search(r"\bauth\s+git-credential\b", helper):
        add("gh_git", "warning", "Подключение gh к Git для этого HTTPS-адреса не подтверждено. "
            "Если выбран gh, выполни gh auth setup-git в терминале. "
            "Если уже работает другой менеджер/SSH, выбери существующий доступ Git.")
    else:
        add("gh_git", "ok", "Для push-адреса настроен credential helper gh; его запуск здесь не проверяется.")
