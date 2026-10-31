"""Настоящий APK 1.0, демо-сервер и системный ввод; только изолированный эмулятор."""
import argparse
import json
import re
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', default='emulator-5554')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / 'output/playwright/task-26'
    output.mkdir(parents=True, exist_ok=True)
    port = json.loads((root / 'output/playwright/task-24/demo.json').read_text(encoding='utf-8'))['port']
    base = f'http://127.0.0.1:{port}'
    pkg = 'space.agentpipeline.android'

    def adb(*argv):
        return subprocess.check_output([args.adb, '-s', args.serial, *argv], timeout=40).decode('utf-8', 'replace').strip()

    def api(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        return json.load(urllib.request.urlopen(req))

    assert adb('shell', 'getprop', 'ro.kernel.qemu') == '1', 'Разрешён только эмулятор'
    assert api('/api/demo/info')['stub_agents']
    adb('reverse', 'tcp:8765', f'tcp:{port}')
    adb('shell', 'settings', 'put', 'secure', 'show_ime_with_hard_keyboard', '1')

    def ui():
        for _ in range(4):
            try:
                result = adb('shell', 'uiautomator', 'dump', '/sdcard/discussion-ui.xml')
                if 'dumped' in result:
                    return ET.fromstring(adb('shell', 'cat', '/sdcard/discussion-ui.xml'))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ET.ParseError):
                pass
            time.sleep(.5)
        raise AssertionError('UIAutomator не смог прочитать дерево после перехода')

    def bounds(n):
        return list(map(int, re.findall(r'\d+', n.get('bounds', ''))))

    def find(tree, text, exact=False):
        for n in tree.iter('node'):
            b = bounds(n)
            if len(b) != 4 or b[2] <= b[0] or b[3] <= b[1]:
                continue
            labels = (n.get('text', ''), n.get('content-desc', ''))
            if any(x == text if exact else text in x for x in labels):
                return n

    def wait_node(text, exact=False, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = find(ui(), text, exact)
            if n is not None:
                return n
        raise AssertionError(f'Не найден элемент: {text}')

    def tap(n):
        l, t, r, b = bounds(n)
        adb('shell', 'input', 'tap', str((l+r)//2), str((t+b)//2))
        time.sleep(.35)

    def click(text, exact=False):
        tap(wait_node(text, exact))

    def screenshot(name):
        (output / name).write_bytes(subprocess.check_output([args.adb, '-s', args.serial, 'exec-out', 'screencap', '-p']))

    def focus_editor():
        editor = next(n for n in ui().iter('node') if n.get('class') == 'android.widget.EditText')
        if editor.get('focused') == 'true' and 'mInputShown=true' in adb('shell', 'dumpsys', 'input_method'):
            return
        l, t, r, b = bounds(editor)
        # WebView accessibility может включать в bounds всю многострочную подсказку, скрытую под навигацией.
        adb('shell', 'input', 'tap', str(l + 30), str(t + 25))

    r = api('/api/demo/finish', {'title': 'Android: обсуждение готового результата'})
    adb('shell', 'am', 'start', '-n', pkg + '/.MainActivity')
    dialog = next((n for n in ui().iter('node') if n.get('resource-id') == 'android:id/button1'), None)
    if dialog is not None:
        tap(dialog)
    click('Обновить страницу')
    click('Результаты')
    wait_node('Android: обсуждение')
    before = api('/api/demo/discussions')['runs']
    for _ in range(6):
        n = find(ui(), 'Обсудить', True)
        if n is not None:
            tap(n)
            dialog = next((x for x in ui().iter('node') if x.get('resource-id') == 'android:id/button1'), None)
            if dialog is not None:
                tap(dialog)
            break
        adb('shell', 'input', 'swipe', '540', '1450', '540', '800', '300')
    wait_node('Убрать прикреплённую задачу')
    assert api('/api/demo/discussions')['runs'] == before, 'Нажатие запустило модель'
    # Фокус, клавиатура и системная «Назад» проверяются через настоящие Android-события.
    focus_editor()
    adb('shell', 'input', 'keycombination', '113', '29')
    adb('shell', 'input', 'text', 'Android%sdraft%sfor%sresult')
    assert 'mInputShown=true' in adb('shell', 'dumpsys', 'input_method')
    screenshot('android-keyboard.png')
    adb('shell', 'input', 'keyevent', '4')
    wait_node('Убрать прикреплённую задачу')
    cards = [n for n in ui().iter('node') if n.get('text') == 'Карточка' and bounds(n) != [0, 0, 0, 0]]
    tap(cards[-1])  # Карточка в черновике, а не одноимённая ссылка старого сообщения.
    wait_node('Сохранить', True)
    adb('shell', 'input', 'keyevent', '4')
    wait_node('Убрать прикреплённую задачу')
    adb('shell', 'input', 'keyevent', '4')
    click('Чат', True)
    wait_node('Убрать прикреплённую задачу')
    assert find(ui(), 'Android draft for result') is not None
    note = output / 'DiscussionContext.txt'
    note.write_text('android-context-file-742', encoding='utf-8')
    adb('push', str(note), '/sdcard/Download/DiscussionContext.txt')
    click('📎', True)
    click('Show roots')
    # Последний узел — строка выдвинутого меню, а не лежащая под ним хлебная крошка.
    downloads = [n for n in ui().iter('node') if n.get('text') == 'Downloads' and bounds(n) != [0, 0, 0, 0]]
    tap(downloads[-1])
    click('DiscussionContext.txt', True)
    wait_node('DiscussionContext.txt')
    click('Обновить страницу')
    wait_node('Убрать прикреплённую задачу')
    assert find(ui(), 'Android draft for result') is not None, 'Черновик не пережил перезагрузку'
    wait_node('DiscussionContext.txt')
    screenshot('android-draft.png')
    adb('reverse', '--remove', 'tcp:8765')
    # Убиваем только приложение эмулятора: открытые pooled HTTP-сокеты иначе переживают adb reverse --remove.
    adb('shell', 'am', 'force-stop', pkg)
    adb('shell', 'am', 'start', '-n', pkg + '/.MainActivity')
    wait_node('ERR_CONNECTION_REFUSED')
    adb('reverse', 'tcp:8765', f'tcp:{port}')
    click('Обновить страницу')
    wait_node('Убрать прикреплённую задачу')
    wait_node('Android draft for result')
    wait_node('DiscussionContext.txt')
    click('Отправить сообщение')
    wait_node('Демо без модели: обсуждаем')
    deadline = time.monotonic() + 30
    sent = None
    while sent is None and time.monotonic() < deadline:
        for c in api(f"/api/projects/{r['project_id']}/chats")['chats']:
            messages = api(f"/api/chats/{c['id']}/messages")['messages']
            sent = next((m for m in messages if m['text'] == 'Android draft for result'
                         and (m.get('task_context') or {}).get('result_id') == r['id']), None)
            if sent:
                break
        time.sleep(.5)
    assert sent, 'Нет принятого сообщения с контекстом именно этого результата'
    assert sent['task_context']['result_id'] == r['id'] and sent['task_context']['task_id'] == r['source_task_id']
    assert sent['attachments'][0]['name'] == 'DiscussionContext.txt'
    with urllib.request.urlopen(base + f"/api/chat/{sent['id']}/files/0") as response:
        assert response.read() == b'android-context-file-742', 'Байты файла из WebView потеряны'
    focus_editor()
    adb('shell', 'input', 'text', 'create%sfollowup')
    adb('shell', 'input', 'keyevent', '4')
    click('Отправить сообщение')
    wait_node('Создана связанная задача')
    linked = api(f"/api/tasks/{r['source_task_id']}")
    assert linked['status'] == 'done' and linked['links'], 'Нет связанного продолжения'
    screenshot('android-history.png')
    adb('shell', 'am', 'force-stop', pkg)
    adb('shell', 'am', 'start', '-n', pkg + '/.MainActivity')
    wait_node('Результат и отчёт')
    evidence = {'android': adb('shell', 'getprop', 'ro.build.version.release'), 'apk': '1.0 (versionCode 1)',
                'result_id': r['id'], 'task_id': r['source_task_id'], 'no_agent_on_attach': True,
                'keyboard_and_back': True, 'draft_reload': True, 'structured_message': True,
                'native_file_picker_and_persistent_file': True,
                'offline_reconnect_preserves_draft': True,
                'linked_followup_stub': True, 'history_process_restart': True, 'real_models': False}
    (output / 'android-discussions.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=True))


if __name__ == '__main__':
    main()
