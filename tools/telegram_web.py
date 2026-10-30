"""Опциональный транспорт Telegram Web: отдельный профиль, получатель только «Избранное»."""
import argparse
import json
import sys
import time
from pathlib import Path

URL = "https://web.telegram.org/k/"
INPUT = "div.input-message-input[contenteditable='true']:not(.input-field-input-fake)"
QR = "canvas[class*='qrCanvas'], .qr-canvas"


class TelegramError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def type_text(page, box, text):
    box.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    for i, line in enumerate(text.split("\n")):
        if i:
            page.keyboard.press("Shift+Enter")
        if line:
            page.keyboard.insert_text(line)


def open_saved(page):
    raw = page.evaluate("localStorage.getItem('user_auth')")
    try:
        uid = int(json.loads(raw)["id"])
        if uid <= 0:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise TelegramError("auth_required", "Войдите в свой Telegram заново.")
    # Получатель берётся исключительно из текущей авторизации, аргумента --to нет.
    peer = page.evaluate("""async id => {
        await appImManager.setInnerPeer({peerId: id});
        return String(appImManager.chat.peerId);
    }""", uid)
    if peer != str(uid):
        raise TelegramError("error", "Не удалось подтвердить открытие Избранного. Отправка отменена.")
    page.locator(f"#column-center {INPUT}").first.wait_for(state="visible", timeout=30000)
    page.wait_for_timeout(1000)


def wait_sent(page, marker):
    bubble = page.locator("#column-center .bubble").filter(has_text=marker).last
    bubble.wait_for(state="visible", timeout=30000)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if bubble.evaluate("e => e.classList.contains('is-error')"):
            raise TelegramError("uncertain", "Telegram показал ошибку сообщения. Проверьте Избранное перед повтором.")
        if not bubble.evaluate("e => e.classList.contains('is-sending')"):
            page.wait_for_timeout(1500)
            if not bubble.evaluate("e => e.classList.contains('is-sending') || e.classList.contains('is-error')"):
                return
        page.wait_for_timeout(500)
    raise TelegramError("uncertain", "Нет подтверждения Telegram. Проверьте Избранное перед повтором.")


def send(page, part, before_send):
    text, files = part.get("text", ""), part.get("files") or []
    if not text or len(text) > (1000 if files else 4000):
        raise TelegramError("error", "Некорректная длина сообщения Telegram.")
    for f in files:
        if not Path(f).is_file():
            raise TelegramError("error", "Сохранённое вложение недоступно.")
    marker = text.split("\n")[-1]
    if files:
        page.locator("#column-center .attach-file").first.click()
        item = page.locator(".btn-menu-item").filter(has_text="Document").first
        with page.expect_file_chooser() as chooser:
            item.click()
        chooser.value.set_files(files)
        popup = page.locator(".popup-new-media")
        popup.wait_for(state="visible", timeout=30000)
        type_text(page, popup.locator(INPUT).first, text)
        before_send()
        popup.locator("button.btn-primary").first.click()
        popup.wait_for(state="detached", timeout=30000)
    else:
        type_text(page, page.locator(f"#column-center {INPUT}").first, text)
        before_send()
        page.keyboard.press("Enter")
    wait_sent(page, marker)


def run(args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "error", "error": "Playwright не установлен. Откройте инструкцию подключения в настройках."}
    root = args.data_dir.resolve()
    profile = root / "profile"
    root.mkdir(parents=True, exist_ok=True)
    submitted = False

    def before_send():
        nonlocal submitted
        submitted = True

    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(str(profile), headless=not args.show,
                        locale="en-US", viewport={"width": 1000, "height": 800}, accept_downloads=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(30000)
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                if args.operation == "login":
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        if page.locator("#column-left .chatlist").first.is_visible():
                            break
                        # Только QR, без снимка чатов/номера телефона/других данных аккаунта.
                        qr = page.locator(QR).first
                        if qr.count() and qr.is_visible():
                            tmp = root / "qr-next.png"
                            qr.screenshot(path=str(tmp))
                            tmp.replace(root / "qr.png")
                        page.wait_for_timeout(1000)
                    else:
                        raise TelegramError("auth_required", "Вход не завершён. Повторите QR или выполните вход на ПК по инструкции.")
                else:
                    try:
                        page.wait_for_selector("#column-left .chatlist", timeout=60000)
                    except Exception:
                        if page.locator(QR).count() or not page.evaluate("localStorage.getItem('user_auth')"):
                            raise TelegramError("auth_required", "Сессия Telegram истекла. Выполните вход заново.")
                        raise TelegramError("error", "Telegram Web не загрузился. Проверьте сеть и повторите проверку.")
                open_saved(page)
                if args.operation in ("send", "test"):
                    if args.operation == "test":
                        part = {"text": "AgentPipeline: Telegram подключён. Результаты задач будут приходить в Избранное.", "files": []}
                    else:
                        part = json.loads(args.job.read_text(encoding="utf-8"))
                    send(page, part, before_send)
                    return {"status": "sent"}
                # Ждём сохранения браузерной авторизации перед закрытием профиля.
                page.wait_for_timeout(1500)
                return {"status": "ready"}
            finally:
                ctx.close()
    except TelegramError as e:
        return {"status": e.status, "error": e.message}
    except Exception:
        return {"status": "uncertain" if submitted else "error",
                "error": "Нет подтверждения Telegram. Проверьте Избранное перед повтором." if submitted else
                         "Не удалось открыть Telegram Web. Проверьте Playwright, Chromium, сеть и доступность профиля по инструкции."}
    finally:
        (root / "qr.png").unlink(missing_ok=True)
        (root / "qr-next.png").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("login", "check", "test", "send"))
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data/telegram")
    parser.add_argument("--show", action="store_true", help="Окно для ручного входа, включая пароль 2FA")
    parser.add_argument("--job", type=Path, help="Подготовленное сообщение для send")
    args = parser.parse_args()
    if args.operation == "send" and not args.job:
        parser.error("Для send нужен --job")
    result = run(args)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return 0 if result["status"] in ("ready", "sent") else 1


if __name__ == "__main__":
    raise SystemExit(main())
