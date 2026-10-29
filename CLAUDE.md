# ClaudePipeline — заметки для агента

Личный канбан-трекер. Задачи из колонки «В работу» выполняет Claude Code через `claude -p` по подписке, без API-ключа. Пользовательское описание — в README.md, здесь только то, что нужно для работы с кодом.

## Устройство

- `server/main.py` — HTTP API + SSE на `ThreadingHTTPServer`. Эндпоинты объявляются декоратором `@route("METHOD", r"/api/...")`; группы `(\d+)` приходят в обработчик как `int` после `(h, body, query)`. Ошибки — `need(cond, msg, code)` или `HttpError`. Тут же отдача статики из `web/`, `/report/<id>` → `report.html`, приём вложений чата (лимиты `MAX_*`).
- `server/pipeline.py` — всё, что связано с логикой:
  - `Hub`/`hub` — рассылка SSE-событий (`changed`, `state`, `task_event`, `chat`, `questions`…);
  - промпты `TASK_SYSTEM`, `CHAT_SYSTEM`;
  - `Orchestrator`/`orch` — цикл `tick()` раз в 1,5 с, лимит `max_parallel`, в одном проекте одна задача за раз, пауза по лимиту подписки (`pause_until`), `recover()` после рестарта;
  - `finish_task`, `answer_question`, `on_manual_move`;
  - `Chat`/`chat` — сессия чата на проект;
  - `mcp_call`/`_mcp` — обработчики всех MCP-инструментов;
  - `import_parse` — разбор текста в задачи через `claude -p --json-schema`.
- `server/runner.py` — `ClaudeRun`: собирает команду `claude -p --output-format stream-json`, пишет MCP-конфиг в `data/runs/mcp/<key>.json`, читает stream-json в фоновом потоке и зовёт колбэки `on_text/on_tool/on_rate_limit/on_done`. Здесь же `PORT` из `PIPELINE_PORT`.
- `server/mcp_tracker.py` — stdio MCP-сервер, который запускает сам Claude Code. Тонкая прослойка: набор инструментов зависит от роли (`TASK_TOOLS` для `task`, `CHAT_TOOLS` для `chat`), каждый вызов уходит в `POST /api/mcp/call`. Исключение — `approve`: он сам опрашивает `approve_status`, пока пользователь не ответит.
- `server/db.py` — SQLite `data/tracker.db` (WAL, один connection под `RLock`). Хелперы `q/one/ex`, схема в `SCHEMA`, настройки по умолчанию в `DEFAULT_SETTINGS`, системные колонки в `SYSTEM_COLUMNS`.
- `web/` — чистые HTML/CSS/JS без сборки и фреймворков: `index.html` + `app.js` (вся SPA), `md.js` (свой рендер markdown), `report.html` (страница отчёта), `style.css`.
- `docs/screenshots/` — скриншоты для README. Снимать только на демо-данных (копия трекера во временной папке на другом порту + headless Edge `--screenshot`), не на боевой базе.

## Схема БД (кратко)

`settings(key, value)`, `projects` (path — рабочая папка Claude, `chat_session_id`), `columns` (`kind`: backlog/todo/progress/waiting/done у системных, NULL у своих), `tasks` (`status`, `session_id`, `pending_prompt`, `summary`, `report_path`, `cost_usd`…), `events` (лента задачи), `questions` (`kind`: question/permission), `chat_messages` (`attachments` — JSON).

Статусы задачи: `idle`, `queued`, `running`, `resume` (продолжить с `pending_prompt`), `waiting`, `done`, `error`, `interrupted`.

Миграции: новые таблицы — через `CREATE TABLE IF NOT EXISTS` в `SCHEMA`; новые колонки — через `ALTER TABLE` в блоке «миграции старых баз» в `db.py` (база пользователя уже существует).

## Поток данных

1. Карточка попадает в колонку `todo` → `status='queued'`.
2. `orch.tick()` берёт её (сначала `resume`, потом `todo` по позиции) → `start_task` переносит в `progress`, `status='running'`, создаёт `ClaudeRun` с `cwd` = папка проекта (отсюда же подхватывается этот CLAUDE.md).
3. `runner` запускает `claude -p` с `--mcp-config` → Claude Code поднимает `mcp_tracker.py` с env `PIPELINE_URL/ROLE/TASK_ID/PROJECT_ID`.
4. Вызовы `mcp__tracker__*` → `POST /api/mcp/call` → `pipeline._mcp`. `ask_user` переводит задачу в `waiting`; ответ пользователя → `status='resume'` → продолжение через `--resume <session_id>`. `finish_task` пишет отчёт и переносит в `done`.
5. Изменения уходят в браузер через `GET /api/events` (SSE), клиент перечитывает нужные данные.

Где что лежит (`data/` в `.gitignore`):
- `data/tracker.db` — база;
- `data/reports/task-<id>.md` — отчёты (прошлый отчёт сворачивается в `<details>`);
- `data/runs/task-<id>.jsonl`, `chat-<pid>.jsonl` — сырые логи stream-json;
- `data/runs/mcp/` — MCP-конфиги запусков;
- `data/uploads/<project_id>/` — вложения чата.

Транскрипты сессий — у самого Claude Code в `~/.claude/projects/<папка>/<session_id>.jsonl` (`pipeline.transcript_path`).

## Запуск и проверка

- `start.cmd` → `python server\main.py`, открывает http://127.0.0.1:8765. Флаг `--no-browser` — без браузера. Порт — переменная `PIPELINE_PORT`.
- Сервер, скорее всего, уже запущен, и ты работаешь внутри него. **Не перезапускай и не останавливай его** — это прервёт твою же сессию. Второй экземпляр на другом порту тоже не поднимай: он делит `data/tracker.db`, выполнит `recover()` и начнёт брать те же задачи. Изменения в `server/` вступают в силу только после перезапуска — напиши в отчёте, что он нужен.
- Проверка без запуска: `python -m py_compile server/*.py`. Отдельные функции можно проверять импортом модулей во временной копии проекта (иначе `db.py` откроет боевую базу). `web/` перечитывается браузером без кэша (`Cache-Control: no-cache`).
- Тестов и линтеров в проекте нет.
- Код лежит в приватном репозитории https://github.com/makisima3/ClaudePipeline (ветка `main`, `gh` залогинен). В git только код, README, CLAUDE.md и `docs/`; `data/` и личные данные не коммитить. Коммитить и пушить — только когда задача об этом просит.

## Соглашения

- Ноль внешних зависимостей: только stdlib Python 3.10+, без pip и npm, без CDN во фронте.
- Тексты UI, ошибки API, промпты, комментарии в коде и общение — на русском.
- Стиль как в соседних файлах: короткие функции, плотный код, комментарии редкие и по делу, docstring в одну строку.
- Новый MCP-инструмент: схема в `mcp_tracker.py` (в нужный список роли) + ветка в `pipeline._mcp`.
- Новая настройка: ключ в `db.DEFAULT_SETTINGS` (иначе `POST /api/settings` её проигнорирует) + поле в UI.
- После изменения данных на доске вызывай `changed(project_id)` из `pipeline.py` (в `main.py` — `pl.changed`), чтобы обновился браузер.

## Правило

Если задача меняет устройство проекта (новые модули, эндпоинты, таблицы и колонки БД, MCP-инструменты, файлы в `data/`), обнови этот CLAUDE.md и раздел «Файлы»/«Как устроено» в README.md в рамках той же задачи.
