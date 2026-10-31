"""Изолированный демо-сервер для ручной проверки сайта/APK. Модели и Telegram не запускаются."""
import json
import re
import threading
from pathlib import Path

import test_pipeline as fixture
import results
import discussions

db, pl, main = fixture.db, fixture.pl, fixture.main


def run():
    class DemoRun(fixture.FakeRun):
        def start(self):
            self.on_session('demo-session-without-model')
            cid = int(self.key.rsplit('-', 1)[1])
            context = discussions.latest(cid)
            if context:
                answer = f"Демо без модели: обсуждаем **{context['project_name']} #{context['task_number']}**, результат №{context['result_id']}. Итог: {context['summary']}"
                last = db.chat_messages(cid)[-1]['text'] or ''
                if 'создай задачу' in last.lower() or 'create followup' in last.lower():
                    notify = 'telegram_notify=true' in self.prompt.rsplit('Настройка устройства', 1)[-1]
                    created = json.loads(pl._mcp('create_task', {'title': 'Увеличить кнопки', 'links': [context['task_id']],
                                                               'telegram_notify': notify}, None, context['project_id']))
                    answer += f"\n\nСоздана связанная задача #{created['task_number']}."
                self.on_text(answer)
            else:
                self.on_text('Демонстрационный ответ. Вложения получены; модель не запускалась.')
            self.on_done({'is_error': False})
            return self
    pl.agent_run = lambda **kwargs: DemoRun(**kwargs)
    db.ex("DELETE FROM projects")
    pid = db.create_project("Мастерская", str(fixture.SANDBOX))
    second = db.create_project("Личный сайт", str(fixture.SANDBOX))
    db.set_setting("onboarding_status", "completed")
    db.set_setting("git_mode", "off")
    art = fixture.SANDBOX / "демо-доска.png"
    art.write_bytes((fixture.SOURCE / "docs/screenshots/board.png").read_bytes())
    doc = fixture.SANDBOX / "инструкция.txt"
    doc.write_text("Демонстрационный материал AgentPipeline. Общая история сайта и Android.", encoding="utf-8")
    video = fixture.SOURCE / "output/playwright/task-24/demo.mp4"
    for i, (notify, auth) in enumerate(((False, "unconfigured"), (True, "unconfigured"), (True, "ready"), (True, "error"))):
        tid = db.create_task(pid, ("Новая доска проекта", "Материалы сохранены без Telegram", "Завершение с файлами", "Результат при ошибке доставки")[i], telegram_notify=notify)
        materials = [str(art), str(doc)]
        if video.is_file() and i == 3:
            materials.append(str(video))
        pl.finish_task(tid, "Работа завершена. Материалы доступны в общей истории.",
                       "## Проверка\n\nДемонстрационные данные для интерфейса. Реальные модели и отправка не запускались.",
                       "Доска готова к работе. Итог и материалы сохранены в проекте.\n\nМожно открыть карточку, прочитать отчёт и скачать файлы.", materials)
    other = db.create_task(second, "Отдельный результат другого проекта")
    pl.finish_task(other, "Изоляция проектов", "Демо второго проекта")
    todo = db.create_task(pid, "Подготовить следующую итерацию", "Демо карточки: создание, изменение и перенос.")
    cid = db.create_chat(pid)
    db.add_chat(cid, "user", "Где посмотреть завершённые задачи?")
    db.add_chat(cid, "assistant", "Открой **Результаты**. Итоги и материалы доступны и на сайте, и в приложении.")
    db.ex("UPDATE chats SET title='Работа над проектом',title_kind='manual' WHERE id=?", (cid,))
    question_task = db.create_task(pid, "Выбрать следующий этап")
    pl._mcp("ask_user", {"question": "Какой этап взять следующим?", "options": ["Интерфейс", "Материалы"]}, question_task, pid)
    # Только здесь существуют вспомогательные маршруты демо; боевой сервер их не импортирует.
    @main.route("POST", "/api/demo/finish")
    def finish(h, body, query):
        tid = db.create_task(pid, body.get("title", "Фоновая проверка Android"))
        pl.finish_task(tid, "Новый результат получен в фоне", "Проверка системного JobScheduler на эмуляторе Android.", telegram_files=[str(doc)])
        return results.public(results.existing(db.get_task(tid)))

    @main.route("GET", "/api/demo/info")
    def info(h, body, query):
        return {"project_id": pid, "second_project_id": second, "task_id": todo, "chat_id": cid, "question_task_id": question_task, "stub_agents": True}

    @main.route("GET", "/api/demo/discussions")
    def discussion_info(h, body, query):
        return {"runs": len(fixture.FakeRun.instances), "tasks": db.q("SELECT id,title,telegram_notify FROM tasks"),
                "messages": db.chat_messages(cid), "prompts": [r.prompt for r in fixture.FakeRun.instances]}

    srv = main.Server(("127.0.0.1", 0), main.Handler)
    output = fixture.SOURCE / "output/playwright/task-24"
    output.mkdir(parents=True, exist_ok=True)
    (output / "demo.json").write_text(json.dumps({"port": srv.server_address[1], "sandbox": str(fixture.SANDBOX)}, ensure_ascii=False), encoding="utf-8")
    print(f"DEMO_PORT={srv.server_address[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    run()
