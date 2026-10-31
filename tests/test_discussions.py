"""Обсуждение конкретного завершения: изолированная БД и заглушки обоих исполнителей."""
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_pipeline as fixture
import discussions
import results

db, pl, main = fixture.db, fixture.pl, fixture.main


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        fixture.PipelineTests.setUp(self)
        self.cid = db.create_chat(self.pid)
        self.file = fixture.SANDBOX / 'контекст.txt'
        self.file.write_text('Исторический материал', encoding='utf-8')
        pl.finish_task(self.tid, 'Исторический итог', 'Исторический отчёт', telegram_files=[str(self.file)])
        self.result = results.existing(db.get_task(self.tid))
        self.ref = {'task_id': self.tid, 'result_id': self.result['id']}
        self.body = {'text': 'Кнопки мелкие, обсудим', 'task_context': self.ref,
                     'client_request_id': 'discussion-request-00001', 'telegram_notify': False}

    def send(self, body=None):
        return main.api_chat_send(None, body or self.body, {}, self.cid)

    def test_new_resume_and_provider_handoff_keep_exact_snapshot(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider), patch.object(pl, 'AGENT', provider), patch.object(db, 'AGENT', provider), \
                    patch.object(pl, 'agent_run', side_effect=lambda **kw: fixture.FakeRun(**kw)):
                self.cid = db.create_chat(self.pid)
                self.send()
                first = fixture.FakeRun.instances[-1]
                self.assertFalse(first.resume)
                self.assertIn('Исторический итог', first.prompt)
                self.assertIn('Исторический отчёт', first.prompt)
                self.assertIn('Описание', first.prompt)
                self.assertIn(f'links=[{self.tid}]', first.prompt)
                self.assertIn('telegram_notify=false', first.prompt)
                self.assertIn(str(results.ROOT), first.add_dirs)
                saved = db.chat_messages(self.cid)[0]
                self.assertEqual(saved['task_context']['result_id'], self.result['id'])
                self.assertEqual(saved['task_context']['status'], 'done')
                self.assertTrue(Path(saved['task_context']['report_path']).is_file())
                first.on_done({'is_error': False})
                db.update_task(self.tid, status='running', description='Новое описание', last_run_at=db.now())
                pl.finish_task(self.tid, 'НОВЫЙ ИТОГ', 'НОВЫЙ ОТЧЁТ')
                self.send({'text': 'Создай продолжение', 'telegram_notify': True, 'client_request_id': 'discussion-request-00002'})
                resumed = fixture.FakeRun.instances[-1]
                self.assertTrue(resumed.resume)
                self.assertIn('Исторический итог', resumed.prompt)
                self.assertNotIn('НОВЫЙ ОТЧЁТ', resumed.prompt)
                self.assertIn('telegram_notify=true', resumed.prompt)
                resumed.on_done({'is_error': False})
                other = 'codex' if provider == 'claude' else 'claude'
                db.ex('UPDATE chats SET last_provider=? WHERE id=?', (other, self.cid))
                db.set_chat_session(self.cid, None)
                self.send({'text': 'Продолжим', 'client_request_id': 'discussion-request-00003'})
                handoff = fixture.FakeRun.instances[-1]
                self.assertIn('Контекст чата с другим исполнителем', handoff.prompt)
                self.assertIn('Исторический отчёт', handoff.prompt)
                handoff.on_done({'is_error': False})

    def test_duplicate_and_concurrent_retry_do_not_save_files_or_launch_twice(self):
        self.body['attachments'] = [{'name': 'note.txt', 'data': 'aGk=', 'type': 'text/plain'}]
        answers = []
        with patch.object(pl, 'agent_run', side_effect=lambda **kw: fixture.FakeRun(**kw)):
            threads = [threading.Thread(target=lambda: answers.append(self.send())) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(answers), 4)
            self.assertEqual(sum(bool(r.get('duplicate')) for r in answers), 3)
            self.assertEqual(len(fixture.FakeRun.instances), 1)
            self.assertEqual(len(list(db.chat_dir(self.pid, self.cid).glob('*'))), 1)
            self.assertEqual(len(db.chat_messages(self.cid)), 1)
            pl.chat.runs[self.cid].on_done({'is_error': False})
            self.assertTrue(self.send()['duplicate'])

    def test_validate_before_uploads_no_spoofing_or_cross_project_numbers(self):
        other = db.create_project('Другой', str(fixture.SANDBOX))
        tid = db.create_task(other, 'Такой же локальный номер')
        pl.finish_task(tid, 'Другой', 'Другой')
        row = results.existing(db.get_task(tid))
        self.assertEqual(db.get_task(tid)['task_number'], db.get_task(self.tid)['task_number'])
        for ref in ({'task_id': tid, 'result_id': row['id']}, {'task_id': tid, 'result_id': self.result['id']},
                    {'task_id': self.tid, 'result_id': 999999}, {'task_id': True, 'result_id': self.result['id']}):
            with self.subTest(ref=ref), self.assertRaises(main.HttpError):
                self.send({**self.body, 'task_context': ref, 'attachments': [{'name': 'bad.txt', 'data': 'aGk='}]})
        self.assertFalse(db.chat_messages(self.cid))
        self.assertFalse(db.chat_dir(self.pid, self.cid).exists())
        with patch.object(pl, 'agent_run', side_effect=lambda **kw: fixture.FakeRun(**kw)):
            self.send({**self.body, 'task_context': {**self.ref, 'summary': 'ПОДДЕЛКА'}})
        self.assertNotIn('ПОДДЕЛКА', fixture.FakeRun.instances[-1].prompt)

    def test_deleted_task_and_reused_global_id_never_link_wrong_card(self):
        db.ex('DELETE FROM tasks WHERE id=?', (self.tid,))
        context = discussions.capture(self.pid, self.ref)
        self.assertFalse(context['task_available'])
        reused = db.create_task(self.pid, 'Другая карточка')
        self.assertEqual(reused, self.tid)
        prompt = discussions.prompt(context)
        self.assertIn('удалена или недоступна', prompt)
        self.assertNotIn(f'links=[{reused}]', prompt)
        self.assertIn('Исторический отчёт', prompt)

    def test_old_result_missing_report_file_and_process_restart(self):
        db.ex('UPDATE task_results SET description=NULL,status=NULL WHERE id=?', (self.result['id'],))
        with patch.object(Path, 'write_text', side_effect=OSError('test')):
            context = discussions.capture(self.pid, self.ref)
        self.assertIsNone(context['description'])
        self.assertIsNone(context['report_path'])
        self.assertIn('Исторический отчёт', discussions.prompt(context))
        db.add_chat(self.cid, 'user', 'История', task_context=context, client_request_id='persist-discussion-001')
        script = f'import db,json; print(json.dumps(db.chat_messages({self.cid}),ensure_ascii=True))'
        env = {**os.environ, 'PYTHONPATH': str(fixture.SANDBOX / 'server')}
        proc = subprocess.run([sys.executable, '-c', script], env=env, cwd=fixture.SANDBOX, capture_output=True, check=True)
        self.assertEqual(json.loads(proc.stdout)[0]['task_context'], context)

    def test_linked_followup_uses_existing_mcp_and_current_device_flag(self):
        context = discussions.capture(self.pid, self.ref)
        self.assertIn('сама привязка не является просьбой', discussions.prompt(context))
        before = db.get_task(self.tid)
        created = json.loads(pl._mcp('create_task', {'title': 'Увеличить кнопки', 'links': [self.tid],
                                    'telegram_notify': False}, None, self.pid))
        self.assertEqual(db.task_links(self.tid)[0]['id'], created['task_id'])
        self.assertEqual(db.task_links(created['task_id'])[0]['id'], self.tid)
        self.assertEqual(db.get_task(created['task_id'])['telegram_notify'], 0)
        self.assertEqual(db.get_task(self.tid), before)

    def test_receipt_only_cannot_create_message_but_recovers_accepted_upload(self):
        with self.assertRaises(main.HttpError) as error:
            self.send({'client_request_id': self.body['client_request_id'], 'receipt_only': True})
        self.assertEqual(error.exception.code, 409)
        self.assertFalse(db.chat_messages(self.cid))
        with patch.object(pl, 'agent_run', side_effect=lambda **kw: fixture.FakeRun(**kw)):
            self.send()
            receipt = self.send({'client_request_id': self.body['client_request_id'], 'receipt_only': True})
            self.assertTrue(receipt['duplicate'])
            self.assertEqual(len(fixture.FakeRun.instances), 1)
