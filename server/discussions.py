"""Структурированный контекст конкретного завершения в обычном чате проекта."""
import json

import db
import results


def capture(project_id, ref):
    """Клиент передаёт только адреса; заголовок, итог и материалы берём с сервера."""
    if not isinstance(ref, dict) or any(type(ref.get(k)) is not int for k in ("task_id", "result_id")):
        raise ValueError("Неверное прикрепление результата. Уберите его и выберите результат заново.")
    row = db.one("SELECT * FROM task_results WHERE id=?", (ref["result_id"],))
    if not row or row["project_id"] != project_id or row["source_task_id"] != ref["task_id"]:
        raise ValueError("Результат недоступен или относится к другому проекту. Текст сохранён; уберите прикрепление или выберите результат заново.")
    task = db.get_task(row["task_id"]) if row["task_id"] else None
    # Отчёт берём из снимка завершения, а не из изменяемого reports/task-<id>.md.
    report = results.ROOT / "reports" / f"result-{row['id']}.md"
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(row["report"], encoding="utf-8")
    except OSError:
        report = None  # Полный текст остаётся в структурированном снимке и промпте.
    files = [{**f, "path": str(results.ROOT / f["path"]),
              "url": f"/api/results/{row['id']}/files/{i}"} for i, f in enumerate(json.loads(row["files"]))]
    return {"task_id": row["source_task_id"], "result_id": row["id"], "project_id": project_id,
            "project_name": row["project_name"], "task_number": row["task_number"], "title": row["title"],
            "run_key": row["run_key"], "created_at": row["created_at"], "description": row["description"],
            "current_description": task["description"] if task else None,
            "status": row["status"], "current_status": task["status"] if task else "deleted",
            "task_available": task is not None, "summary": row["summary"], "text": row["text"],
            "report_path": str(report) if report else None, "report": row["report"], "files": files, "error": row["error"]}


def latest(cid):
    row = db.one("SELECT task_context FROM chat_messages WHERE chat_id=? AND task_context IS NOT NULL ORDER BY id DESC LIMIT 1", (cid,))
    return json.loads(row["task_context"]) if row else None


def prompt(context):
    if not context:
        return ""
    c = context
    task = db.get_task(c["task_id"])
    available = task is not None and task["project_id"] == c["project_id"] and task["task_number"] == c["task_number"]
    return ("\n\n## Прикреплённый результат задачи (структурированный снимок сообщения)\n"
            + json.dumps(c, ensure_ascii=False) + "\n"
            + f"Карточка сейчас: {task['status'] if available else 'удалена или недоступна'}. "
            "Описание, итог, отчёт и материалы выше относятся именно к result_id/run_key, не подменяй их новым запуском. "
            "Если description/status=null, старый результат их не сохранял; так и сообщи, не выдумывай исторические данные. "
            "current_description/current_status — данные карточки на момент прикрепления, не исторического запуска. "
            "Отчёт дан целиком; report_path указан только при доступной локальной копии. Материалы перечислены с локальными путями и API. "
            "Если файл недоступен, сообщи об этом. Обсуждай результат; сама привязка не является просьбой создать задачу. "
            "Только по намерению пользователя создай продолжение через create_task, "
            + (f"обязательно добавь links=[{c['task_id']}] (глобальный task_id). " if available else
               "исходная карточка удалена: не связывай по её номеру и не подменяй другой карточкой, поясни невозможность связи. ")
            + "В описание продолжения включи result_id/run_key и существенный контекст этого результата. "
            "Выполненную исходную задачу не меняй без прямой просьбы. Telegram выбирай по текущему сообщению/настройке устройства.")
