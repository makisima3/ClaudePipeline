# ClaudePipeline — заметки для агента

Личный канбан-трекер. Задачи из колонки «В работу» выполняет Claude Code через `claude -p` по подписке, без API-ключа. Пользовательское описание — в README.md, здесь только то, что нужно для работы с кодом.

## Устройство

- `server/main.py` — HTTP API + SSE на `ThreadingHTTPServer`. Эндпоинты объявляются декоратором `@route("METHOD", r"/api/...")`; группы `(\d+)` приходят в обработчик как `int` после `(h, body, query)`. Ошибки — `need(cond, msg, code)` или `HttpError`. Тут же отдача статики из `web/`, `/report/<id>` → `report.html`, приём вложений чата (лимиты `MAX_*`), запуск сервера: `bind()` (с `--after <pid>` сначала ждёт выхода старого экземпляра), `spawn_successor()` для перезапуска. `Server.allow_reuse_address` в Windows выключен: иначе второй процесс молча занял бы тот же порт.
- `server/pipeline.py` — всё, что связано с логикой:
  - `Hub`/`hub` — рассылка SSE-событий (`changed`, `state`, `task_event`, `chat`/`chat_state`/`chats` — с `project_id` и `chat_id`, `questions`…);
  - промпты `TASK_SYSTEM`, `CHAT_SYSTEM`;
  - `Orchestrator`/`orch` — цикл `tick()` раз в 1,5 с, лимит `max_parallel`, в одном проекте одна задача за раз, пауза по лимиту подписки (`pause_until`), `recover()` после рестарта;
  - `finish_task`, `answer_question`, `on_manual_move`;
  - `Chat`/`chat` — чаты проекта: `runs` по `chat_id`, у каждого чата своя сессия Claude (`chats.session_id`, продолжение через `--resume`). Отвечать одновременно могут разные чаты, в одном чате — один ответ за раз. `create` (пустой безымянный чат проекта не плодит, отдаёт его), `rename`, `send` (название из первого сообщения — `chat_title`, до `CHAT_TITLE_LEN` символов), `stop`, `stop_project`, `delete` (сообщения и папка вложений чата). Сообщение в удалённый чат `db.add_chat` молча не пишет: ответ, прерванный удалением, никуда не попадает;
  - `Restart`/`restart` — отложенный перезапуск сервера (см. ниже), `BOOT_ID` — id текущего запуска, `log_restart` пишет в `data/restart.log`;
  - `mcp_call`/`_mcp` — обработчики всех MCP-инструментов;
  - `import_parse` — разбор текста в задачи через `claude -p --json-schema`.
- `server/runner.py` — `ClaudeRun`: собирает команду `claude -p --output-format stream-json`, пишет MCP-конфиг в `data/runs/mcp/<key>.json`, читает stream-json в фоновом потоке и зовёт колбэки `on_text/on_tool/on_rate_limit/on_done` (`on_done` ровно один раз). `claude -p` не выходит, пока живы его фоновые Bash/PowerShell-задачи, и по их завершении сам начинает новый ход, поэтому после `result` даётся `EXIT_GRACE` секунд на выход (новый `system/init` сбрасывает отсчёт), потом дерево гасится `_kill_tree()` (`taskkill /T /F`), а в `on_done` уходит последний result с `reaped=True`. `killed=True` — только ручной `kill()`. Здесь же `PORT` из `PIPELINE_PORT`.
- `server/updater.py` — обновление с GitHub: `Updater`/`updater`. `loop()` (поток из `main.py`) при старте и раз в `update_check_minutes` минут (0 — только вручную) делает `check()`: `git fetch origin main` и сравнение `HEAD` с `origin/main` (позади/впереди, до 50 заголовков новых коммитов). Состояние в памяти (`updater.state()`, оно же `update` в `/api/state`), каждое изменение — SSE `update`. `update()`: fetch → `blockers()` (ветка не `main`, `git status --porcelain` не пуст, свои коммиты впереди — отказ с причиной) → `git pull --ff-only origin main` → `compile()` всех `server/*.py` (не компилируется — перезапуск отменяется) → `restart.request()`. Только fast-forward: никаких reset/stash/force. `git()` — без окна, с таймаутом, `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `LC_ALL=C` (английский вывод для `HINTS`); ошибки — `GitError` с понятным текстом, в UI их видно тихо в настройках.
- `server/mcp_tracker.py` — stdio MCP-сервер, который запускает сам Claude Code. Тонкая прослойка: набор инструментов зависит от роли (`TASK_TOOLS` для `task`, `CHAT_TOOLS` для `chat`), каждый вызов уходит в `POST /api/mcp/call`. Исключение — `approve`: он сам опрашивает `approve_status`, пока пользователь не ответит.
- `server/db.py` — SQLite `data/tracker.db` (WAL, один connection под `RLock`). Хелперы `q/one/ex`, схема в `SCHEMA`, настройки по умолчанию в `DEFAULT_SETTINGS`, системные колонки в `SYSTEM_COLUMNS`.
- `web/` — чистые HTML/CSS/JS без сборки и фреймворков: `index.html` + `app.js` (вся SPA), `md.js` (свой рендер markdown), `report.html` (страница отчёта), `style.css`.
- `docs/screenshots/` — скриншоты для README. Снимать только на демо-данных (копия трекера во временной папке на другом порту + headless Edge `--screenshot`), не на боевой базе.

## Схема БД (кратко)

`settings(key, value)`, `projects` (path — рабочая папка Claude, `chat_session_id`), `columns` (`kind`: backlog/todo/progress/waiting/done у системных, NULL у своих), `tasks` (`status`, `session_id`, `pending_prompt`, `summary`, `report_path`, `cost_usd`…), `events` (лента задачи), `questions` (`kind`: question/permission), `chats` (`title` — NULL, пока не было первого сообщения; `session_id`; `updated_at` — время последнего сообщения; id `AUTOINCREMENT`, чтобы id удалённого чата не достался новому), `chat_messages` (`chat_id`, `attachments` — JSON). `projects.chat_session_id` устарел: при старте `db._migrate_chats()` переносит старую переписку проекта (сообщения без `chat_id` и `chat_session_id`) в чат «Чат от <дата>», а вложения — из `data/uploads/<pid>/` в `data/uploads/<pid>/<chat_id>/`.

Статусы задачи: `idle`, `queued`, `running`, `resume` (продолжить с `pending_prompt`), `waiting`, `done`, `error`, `interrupted`.

Миграции: новые таблицы — через `CREATE TABLE IF NOT EXISTS` в `SCHEMA`; новые колонки — через `ALTER TABLE` в блоке «миграции старых баз» в `db.py` (база пользователя уже существует).

## Поток данных

1. Карточка попадает в колонку `todo` → `status='queued'`.
2. `orch.tick()` берёт её (сначала `resume`, потом `todo` по позиции) → `start_task` переносит в `progress`, `status='running'`, создаёт `ClaudeRun` с `cwd` = папка проекта (отсюда же подхватывается этот CLAUDE.md).
3. `runner` запускает `claude -p` с `--mcp-config` → Claude Code поднимает `mcp_tracker.py` с env `PIPELINE_URL/ROLE/TASK_ID/PROJECT_ID`.
4. Вызовы `mcp__tracker__*` → `POST /api/mcp/call` → `pipeline._mcp`. `ask_user` переводит задачу в `waiting`; ответ пользователя → `status='resume'` → продолжение через `--resume <session_id>`. `finish_task` пишет отчёт и переносит в `done`.
5. Пока `_done` разбирает итог хода, задача остаётся в `orch.runs` (всё под `orch.lock`, как и `answer_question`), так что продолжение после ответа стартует только когда прошлый процесс завершён (самое позднее через `EXIT_GRACE` после `result`).
6. Перезапуск: `POST /api/restart` (кнопка в настройках) или MCP `restart_tracker` (роли task и chat) ставят `restart.since`. С этого момента `tick()` не берёт задачи, чат отвечает 409, в `orch.state()["restart"]` видно, сколько запусков ждём. `restart.check()` в цикле оркестратора ждёт, пока опустеют `orch.runs` и `chat.runs` (не дольше `RESTART_TIMEOUT` = 10 мин, потом `kill()` — задачи уйдут в `resume`), шлёт SSE `restarting` и вызывает `restart.exit` из `main.py`. Тот запускает новый экземпляр (`python main.py --no-browser --after <pid>`, `CREATE_NEW_CONSOLE`, свёрнутое окно, по возможности вне job-объекта) и гасит старый (`srv.shutdown()` → `os._exit(0)`). Новый ждёт выхода старого, занимает порт и делает `recover()`.
7. Обновление: `GET /api/update` (состояние), `POST /api/update/check` (проверить сейчас, синхронно), `POST /api/update` (обновить; отказ — 409 с причиной, она же в `update_error`). После pull перезапуск идёт по п. 6, плашка `#updbar` под шапкой показывает «Обновлено, перезапуск после завершения текущих задач…»; новая страница грузится по смене `boot_id`, миграции БД применяет `db.py` при старте. Кнопка «Позже» прячет плашку до следующего `origin/main` (`localStorage.updHidden`).
8. Чат: `GET/POST /api/projects/<pid>/chats` (список с флагом `running` / новый), `PATCH/DELETE /api/chats/<id>` (переименовать / удалить), `GET/POST /api/chats/<id>/messages` (история / отправить), `POST /api/chats/<id>/stop`, файл вложения — `GET /api/chat/<message_id>/files/<n>`. В браузере открыт один чат (`S.chat`), список — `S.chats`; событие `chat` применяется только к открытому чату с тем же `chat_id`, `chats` перечитывает список. Последний открытый чат проекта — `localStorage` `chat.<pid>`.
9. Изменения уходят в браузер через `GET /api/events` (SSE), клиент перечитывает нужные данные. Сервер слушает одна вкладка (держит Web Lock `cp-events`) и пересылает события остальным через `BroadcastChannel`. Иначе каждая вкладка держала бы своё соединение, а по HTTP/1.1 их ~6 на хост. Если очередь клиента в `Hub` переполнилась, `/api/events` обрывает поток. После (пере)подключения все вкладки делают `resync(true)`. Первое событие потока — `hello` с `boot_id` (он же есть в `/api/state`); если он отличается от запомненного при загрузке, вкладка делает `location.reload()`. Пока связи нет, висит плашка `#offline` («Трекер перезапускается…» или «Нет связи…»). Видимая вкладка сверяется с сервером раз в 20 с и при фокусе. В `app.js` ответы `loadBoard/loadState/refreshTask/renderQuestions/loadChats/openChat` проходят через `fresh(key)`: ответ, после которого ушёл более новый запрос того же вида, отбрасывается. Пока открыт не «Доска», события доску не перечитывают, поэтому `setView("board")` загружает её заново.

Где что лежит (`data/` в `.gitignore`):
- `data/tracker.db` — база;
- `data/reports/task-<id>.md` — отчёты (прошлый отчёт сворачивается в `<details>`);
- `data/runs/task-<id>.jsonl`, `chat-<pid>-<chat_id>.jsonl` — сырые логи stream-json;
- `data/runs/mcp/` — MCP-конфиги запусков;
- `data/uploads/<project_id>/<chat_id>/` — вложения чата (удаляются вместе с чатом или проектом);
- `data/restart.log` — журнал перезапусков (кто запросил, когда, pid старого и нового экземпляра, ошибки) и обновлений (`запрошен (обновление <старый> -> <новый>)`).

Транскрипты сессий — у самого Claude Code в `~/.claude/projects/<папка>/<session_id>.jsonl` (`pipeline.transcript_path`).

## Запуск и проверка

- `start.cmd` → `python server\main.py`, открывает http://127.0.0.1:8765. Флаг `--no-browser` — без браузера. Порт — переменная `PIPELINE_PORT`.
- Сервер, скорее всего, уже запущен, и ты работаешь внутри него. **Не перезапускай и не останавливай его руками** — это прервёт твою же сессию. Второй экземпляр из этой папки на другом порту тоже не поднимай: он делит `data/tracker.db`, выполнит `recover()` и начнёт брать те же задачи (для проверок — копия во временной папке на другом порту).
- **Изменения в `server/` вступают в силу только после перезапуска.** Если задача меняла `server/`, после `finish_task` вызови `mcp__tracker__restart_tracker`: трекер перезапустится сам, когда твой процесс завершится, и страница перезагрузится. В отчёте напиши, что перезапуск запрошен. Изменения только в `web/` перезапуска не требуют.
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
