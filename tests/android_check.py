"""Проверка настоящего APK только на эмуляторе и tests/android_demo.py; без моделей/Telegram."""
import argparse
import json
import re
import shlex
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", default="emulator-5554")
    parser.add_argument("--reboot-only", action="store_true", help="Отдельно проверить сохранённое задание после настоящей перезагрузки эмулятора")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "output/playwright/task-24"
    demo = json.loads((output / "demo.json").read_text(encoding="utf-8"))
    base = "http://127.0.0.1:" + str(demo["port"])
    pkg = "space.agentpipeline.android"

    def adb(*argv):
        return subprocess.check_output([args.adb, "-s", args.serial, *argv], timeout=45, stderr=subprocess.PIPE).decode("utf-8", "replace").strip()

    assert adb("shell", "getprop", "ro.kernel.qemu") == "1", "Разрешён только изолированный эмулятор"
    with urllib.request.urlopen(base + "/api/demo/info") as response:
        assert json.load(response)["project_id"] > 0

    def wait(fn, label, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = fn()
            if value is not None and value is not False:
                return value
            time.sleep(1)
        raise AssertionError(label)

    def notifications():
        return set(line for line in adb("shell", "cmd notification list").splitlines() if "|" + pkg + "|" in line)

    def key_for(result):
        return next((k for k in notifications() if ":" + str(result["id"]) + "|" in k), None)

    def record(key):
        return adb("shell", "cmd notification get " + shlex.quote(key))

    def create():
        req = urllib.request.Request(base + "/api/demo/finish", data=b"{}", headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))

    def force():
        assert "Running job" in adb("shell", "cmd jobscheduler run -f " + pkg + " 2101"), "Сначала подключите APK и включите уведомления"

    def ui():
        for _ in range(4):
            if "dumped" in adb("shell", "uiautomator dump /sdcard/agentpipeline-ui.xml"):
                return ET.fromstring(adb("shell", "cat /sdcard/agentpipeline-ui.xml"))
        return ET.fromstring("<empty/>")

    def node(root, text):
        return next((n for n in root.iter("node") if visible(n) and (text in n.get("text", "") or text in n.get("content-desc", ""))), None)

    def visible(n):
        bounds = list(map(int, re.findall(r"\d+", n.get('bounds', ''))))
        return len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]

    def tap(n):
        left, top, right, bottom = map(int, re.findall(r"\d+", n.attrib["bounds"]))
        adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))

    def screenshot(name):
        (output / name).write_bytes(subprocess.check_output([args.adb, "-s", args.serial, "exec-out", "screencap", "-p"], timeout=20))

    evidence = {"device": adb("shell", "getprop", "ro.build.version.release"), "forced_job": True}
    if args.reboot_only:
        assert 'unknown' not in adb('shell', 'cmd jobscheduler get-job-state ' + pkg + ' 2101').lower()
        adb('reboot'); time.sleep(3)
        def booted():
            try:
                return adb('shell', 'getprop', 'sys.boot_completed') == '1'
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return False
        wait(booted, 'Эмулятор не перезагрузился', 180)
        adb('reverse', 'tcp:8765', 'tcp:' + str(demo['port']))
        assert 'unknown' not in adb('shell', 'cmd jobscheduler get-job-state ' + pkg + ' 2101').lower()
        result = create(); force()
        wait(lambda: key_for(result), 'Не пришло новое уведомление после перезагрузки', 60)
        assert len(notifications()) == 1, 'Повторены старые уведомления после перезагрузки'
        evidence.update(persisted_job_after_reboot=True, new_notification_without_activity=True, old_not_repeated=True)
        (output / 'android-reboot.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence), flush=True)
        return
    # Убираем старые демо-уведомления, чтобы Android не подменил клик автогруппой с launcher Intent.
    adb("shell", "input keyevent 224"); adb("shell", "wm dismiss-keyguard")
    adb("shell", "cmd statusbar expand-notifications")
    clear = node(ui(), "Clear all")
    if clear is not None:
        tap(clear)
    adb("shell", "cmd statusbar collapse")
    adb("shell", "input keyevent KEYCODE_HOME"); adb("shell", "input keyevent 223")
    assert "mWakefulness=Awake" not in adb("shell", "dumpsys power")
    first = create(); force()
    key = wait(lambda: key_for(first), "Фоновое уведомление не пришло")
    first_record = record(key)
    assert first["title"] in first_record
    evidence["screen_off_background"] = True
    force(); time.sleep(3)
    assert record(key) == first_record, "Повторно опубликовано то же уведомление"
    evidence["no_repeat_notification"] = True

    adb("reverse", "--remove", "tcp:8765")
    missed = [create(), create()]
    force(); time.sleep(3)
    assert not any(key_for(r) for r in missed)
    adb("reverse", "tcp:8765", "tcp:" + str(demo["port"]))
    force()
    wait(lambda: all(key_for(r) for r in missed), "Не восстановлены пропущенные результаты")
    evidence["offline_recovery"] = True
    previous = notifications()
    adb("shell", "am kill " + pkg); force(); time.sleep(3)
    assert notifications() == previous
    evidence["process_restart_without_duplicates"] = True

    adb("shell", "input keyevent 224"); adb("shell", "wm dismiss-keyguard")
    adb("shell", "cmd statusbar expand-notifications")
    wanted = f"#{missed[-1]['task_number']} ·"
    def notification_target():
        root = ui()
        # В свёрнутой автогруппе inbox_text ведёт к launcher, не к отдельному PendingIntent.
        item = next((n for n in root.iter('node') if visible(n) and n.get('resource-id') == 'android:id/title' and wanted in n.get('text', '')), None)
        if item is not None:
            return item
        expand = next((n for n in root.iter('node') if n.get('resource-id') == 'android:id/expand_button'), None)
        if expand is not None:
            tap(expand)
        return None
    target = wait(notification_target, "Не найдено отдельное уведомление в шторке", 50)
    screenshot("android-notification.png"); tap(target)
    wait(lambda: node(ui(), "Полный отчёт этого завершения"), "Не открыт результат из уведомления", 60)
    assert node(ui(), "Мастерская #" + str(missed[-1]["task_number"])) is not None
    screenshot("android-result.png")
    evidence["notification_deep_link"] = True
    adb("shell", "input keyevent KEYCODE_BACK")
    wait(lambda: node(ui(), "Всё прочитано"), "Назад не вернула к ленте")
    evidence["system_back"] = True
    screenshot("android-results.png")
    (output / "android-check.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
