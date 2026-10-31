"""Явная проверка реального Codex по подписке: отдельная копия, два хода, без Claude/Telegram."""
import json
import threading
import time
from pathlib import Path

import test_pipeline as fixture
import results

db, pl, main, cr = fixture.db, fixture.pl, fixture.main, fixture.cr


def run():
    folder = fixture.SANDBOX / 'discussion-project'
    folder.mkdir()
    (folder / 'AGENTS.md').write_text('Это изолированный тест. Работай только в этой папке и с текущим тестовым проектом tracker. '
                                    'Не читай личную память и внешние источники. Не отправляй сообщения, не запускай другие проекты. '
                                    'Git выключен. Не вызывай реальные инструменты вне tracker и чтения тестового материала.\n', encoding='utf-8')
    db.set_setting('git_mode', 'off')
    db.set_setting('onboarding_status', 'completed')
    pid = db.create_project('Изолированное обсуждение', str(folder))
    tid = db.create_task(pid, 'Исторические кнопки', 'Маркер описания: historical-description-742.', telegram_notify=True)
    material = folder / 'material.txt'
    material.write_text('historical-material-619', encoding='utf-8')
    pl.finish_task(tid, 'Кнопки старого выпуска', 'Маркер отчёта: historical-report-853.', telegram_files=[str(material)])
    row = results.existing(db.get_task(tid))
    db.update_task(tid, status='running', description='Новая версия, другой контекст', last_run_at=db.now())
    pl.finish_task(tid, 'Новый выпуск', 'НЕ ОБСУЖДАЕМ: new-report-000')
    cid = db.create_chat(pid)
    srv = main.Server(('127.0.0.1', 0), main.Handler)
    cr.SERVER_URL = f'http://127.0.0.1:{srv.server_address[1]}'
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    evidence = {'sandbox': str(fixture.SANDBOX), 'real_codex': True, 'claude': False, 'telegram': False, 'checks': []}

    def send(body):
        main.api_chat_send(None, body, {}, cid)
        deadline = time.monotonic() + 180
        while pl.chat.running(cid) and time.monotonic() < deadline:
            time.sleep(.5)
        assert not pl.chat.running(cid), 'Истекло ожидание Codex'
        messages = db.chat_messages(cid)
        assert not any(m['role'] == 'error' for m in messages), str(messages)
        return '\n'.join(m['text'] for m in messages if m['role'] == 'assistant')

    output = Path(__file__).resolve().parents[1] / 'output/playwright/task-26'
    output.mkdir(parents=True, exist_ok=True)
    try:
        text = send({'text': 'Обсудим прикреплённый исторический результат. Назови три маркера: из описания, отчёта и содержимого material.txt. '
                     'Прочитай сохранённый материал доступным инструментом. Пока ничего не создавай и не меняй.',
                     'task_context': {'task_id': tid, 'result_id': row['id']}, 'telegram_notify': False,
                     'client_request_id': 'live-discussion-first-0001'})
        for marker in ('historical-description-742', 'historical-report-853', 'historical-material-619'):
            assert marker in text, text
        assert 'new-report-000' not in text, text
        assert len(db.q('SELECT id FROM tasks WHERE project_id=?', (pid,))) == 1
        sid = db.get_chat(cid)['session_id']
        evidence['checks'].append('historical_description_report_material_no_task')
        print('PASS historical result + material + no task created', flush=True)
        text = send({'text': 'Кнопки мелкие, создай задачу на увеличение в бэклоге. В описание добавь три маркера и адрес исторического результата. '
                     'Исходную выполненную задачу не меняй. Результат новой задачи в Telegram не нужен.',
                     'telegram_notify': False, 'client_request_id': 'live-discussion-followup-02'})
        rows = db.q('SELECT * FROM tasks WHERE project_id=? AND id<>?', (pid, tid))
        assert len(rows) == 1, rows
        followup = rows[0]
        assert any(t['id'] == tid for t in db.task_links(followup['id']))
        assert any(t['id'] == followup['id'] for t in db.task_links(tid))
        assert followup['telegram_notify'] == 0 and db.get_task(tid)['status'] == 'done'
        assert db.get_chat(cid)['session_id'] == sid
        for marker in ('historical-description-742', 'historical-report-853', 'historical-material-619'):
            assert marker in followup['description'], followup['description']
        evidence['checks'].append('same_session_linked_followup_current_telegram_flag')
        evidence['response'] = text
        evidence['followup'] = {'title': followup['title'], 'description': followup['description'], 'links': [t['id'] for t in db.task_links(followup['id'])]}
        evidence['success'] = True
        print('PASS real linked followup + resumed session + Telegram off', flush=True)
    finally:
        for active in list(pl.chat.runs.values()):
            active.kill()
        srv.shutdown(); srv.server_close(); thread.join(timeout=5)
        (output / 'live-discussions-codex.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    run()
