// ClaudePipeline — клиент трекера (без фреймворков).
const $ = s => document.querySelector(s);
const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } },
};

const STATUS = {
  idle: null, queued: ["в очереди", "st-queued"], running: ["выполняется", "st-running"], resume: ["продолжит", "st-resume"],
  waiting: ["ждёт ответа", "st-waiting"], done: ["готово", "st-done"], error: ["ошибка", "st-error"],
  interrupted: ["прервано", "st-interrupted"],
};
const MODELS = [["", "по умолчанию"], ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"], ["fable", "Fable"]];
const EFFORTS = [["", "по умолчанию"], ["low", "low"], ["medium", "medium"], ["high", "high"], ["xhigh", "xhigh"], ["max", "max"]];
const COLORS = ["#c8642c", "#3b6fd8", "#2f8a57", "#9b4dca", "#d64571", "#b07a12", "#1f8f99", "#6f6c64"];

const S = {
  state: null, view: ls.get("view", "board"), projectId: ls.get("project", null), board: null,
  chatOpen: ls.get("chatOpen", true), chat: { id: null, pid: null, messages: [], running: false }, chats: [], task: null, showAnswered: false,
};

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || `Ошибка ${r.status}`); throw new Error(d.error); }
  return d;
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast"; el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

const debounce = (fn, ms = 150) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const fmtTime = ts => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const opts = (list, val) => list.map(([v, t]) => `<option value="${v}" ${v === (val || "") ? "selected" : ""}>${t}</option>`).join("");
const pill = st => STATUS[st] ? `<span class="pill ${STATUS[st][1]}">${st === "running" ? '<span class="pulse"></span>' : ""}${STATUS[st][0]}</span>` : "";
const project = () => S.state?.projects.find(p => p.id === S.projectId);

// ---------------- загрузка ----------------
// Запросы одного вида не упорядочены: ответ, после которого ушёл более новый запрос, устарел и отбрасывается.
const SEQ = {};
const fresh = k => { const n = SEQ[k] = (SEQ[k] || 0) + 1; return () => SEQ[k] === n; };

async function loadState() {
  const ok = fresh("state");
  const st = await api("GET", "/api/state");
  if (!ok()) return;
  if (bootChanged(st.boot_id)) return;
  S.state = st;
  if (!project() && S.state.projects.length) S.projectId = S.state.projects[0].id;
  renderHeader();
}

async function loadBoard() {
  const ok = fresh("board");
  if (!S.projectId) { S.board = null; return render(); }
  const b = await api("GET", `/api/projects/${S.projectId}/board`);
  if (!ok()) return;
  const same = S.board && JSON.stringify(b) === JSON.stringify(S.board);
  S.board = b;
  if (S.view === "board" && !same) renderBoard();  // без изменений не перерисовываем: не сбиваем перетаскивание
}

// список чатов проекта; открыт последний открытый в этом проекте (localStorage chat.<pid>) или самый свежий
async function loadChats() {
  const ok = fresh("chats"), pid = S.projectId;
  if (!pid) { S.chats = []; return; }
  const r = await api("GET", `/api/projects/${pid}/chats`);
  if (!ok() || pid !== S.projectId) return;
  S.chats = r.chats;
  renderChatHead();
}

async function loadChat() {
  if (!S.projectId) return;
  await loadChats();
  const has = id => id && S.chats.some(c => c.id === id);
  let id = S.chat.pid === S.projectId ? S.chat.id : ls.get(`chat.${S.projectId}`, null);
  if (!has(id)) id = S.chats[0]?.id || null;
  await openChat(id);
}

async function openChat(id) {
  const ok = fresh("chat"), pid = S.projectId;
  S.chatListOpen = false;
  if (!id) { S.chat = { id: null, pid, messages: [], running: false }; return renderChat(); }
  const r = await api("GET", `/api/chats/${id}/messages`);
  if (!ok() || pid !== S.projectId) return;
  const switched = S.chat.id !== id;
  S.chat = { id, pid, messages: r.messages, running: r.running };
  const i = S.chats.findIndex(x => x.id === id);
  if (i >= 0) S.chats[i] = r.chat; else S.chats.unshift(r.chat);
  ls.set(`chat.${pid}`, id);
  renderChat(switched);
}

// ---------------- шапка ----------------
function renderHeader() {
  const st = S.state;
  $("#projects").innerHTML = st.projects.map(p =>
    `<button class="proj ${p.id === S.projectId ? "active" : ""} ${p.active ? "" : "off"}" onclick="App.selectProject(${p.id})" title="${esc(p.path)}">
      <span class="dot" style="background:${p.color}"></span>${esc(p.name)}</button>`).join("") +
    `<button class="ghost" onclick="App.newProject()" title="Новый проект">＋ проект</button>`;
  document.querySelectorAll(".nav [data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === S.view));
  $("#qbadge").innerHTML = st.open_questions ? `<span class="badge">${st.open_questions}</span>` : "";
  $("#chatBtn").classList.toggle("active", S.chatOpen);
  const pl = st.pipeline, tg = $("#ptoggle");
  tg.classList.toggle("on", pl.enabled === "1");
  tg.classList.toggle("stopping", pl.enabled === "stopping");
  let text = "Выключен";
  if (pl.restart) text = `Перезапуск трекера${pl.restart.busy ? ` · ждёт завершения: ${pl.restart.busy}` : "…"}`;
  else if (pl.enabled === "stopping") text = `Доделывает (${pl.running.length}) и выключится`;
  else if (pl.enabled === "1") {
    if (pl.pause_until * 1000 > Date.now()) text = `Пауза по лимиту до ${new Date(pl.pause_until * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
    else text = pl.running.length ? `Работает · задач: ${pl.running.length}` : "Включён · ждёт задач";
  }
  $("#pstate").textContent = text;
  renderUpdate();
}

function render() {
  renderHeader();
  $("#chat").classList.toggle("hidden", !S.chatOpen || !S.projectId);
  if (S.view === "board") renderBoard();
  else if (S.view === "questions") renderQuestions();
  else renderSettings();
}

// ---------------- доска ----------------
function renderBoard() {
  const v = $("#view");
  if (!S.state.projects.length) {
    v.innerHTML = `<div class="empty-board"><h2>Проектов пока нет</h2><p>Проект — это папка, в которой Claude будет работать над задачами.</p>
      <button class="primary" onclick="App.newProject()">Создать первый проект</button></div>`;
    return;
  }
  if (!S.board || S.board.project.id !== S.projectId) { v.innerHTML = ""; return; }
  const { project: p, columns, tasks } = S.board;
  v.innerHTML = `
    <div class="board-bar">
      <span class="dot" style="background:${p.color}"></span><h2>${esc(p.name)}</h2>
      <span class="path">${esc(p.path)}</span>
      <span class="grow"></span>
      <label class="row" style="margin:0;color:var(--text);font-size:13px" title="Брать ли задачи этого проекта в работу">
        <input type="checkbox" style="width:auto" ${p.active ? "checked" : ""} onchange="App.setProjectActive(this.checked)"> участвует в пайплайне</label>
      <button onclick="App.importDialog()">Импорт текстом</button>
      <button class="primary" onclick="App.newTask()">＋ Задача</button>
    </div>
    <div class="board">
      ${columns.map(c => {
        const list = tasks.filter(t => t.column_id === c.id);
        return `<div class="column ${c.kind || ""}" data-col="${c.id}">
          <div class="col-head">
            <span>${esc(c.name)}</span><span class="count">${list.length}</span>
            ${c.kind === "todo" ? '<span class="tag" title="Задачи отсюда берутся в работу, когда пайплайн включён">▶ авто</span>' : ""}
            <span class="grow"></span>
            ${["progress", "waiting", "done"].includes(c.kind) ? "" : `<button class="ghost" title="Добавить задачу" onclick="App.newTask(${c.id})">＋</button>`}
            ${c.kind ? "" : `<button class="ghost" title="Колонка" onclick="App.columnMenu(${c.id})">⋯</button>`}
          </div>
          <div class="col-body" data-col="${c.id}">${list.map(cardHtml).join("")}</div>
        </div>`;
      }).join("")}
      <div class="add-col"><button class="ghost" onclick="App.addColumn()">＋ колонка</button></div>
    </div>`;
  bindDnD();
}

function cardHtml(t) {
  return `<div class="card" draggable="${t.status !== "running"}" data-id="${t.id}" onclick="App.openTask(${t.id})">
    <div class="title">${esc(t.title)}</div>
    <div class="meta"><span>#${t.id}</span>${pill(t.status)}
      ${t.open_q ? `<span class="pill st-waiting">вопросов: ${t.open_q}</span>` : ""}
      ${t.model ? `<span>${esc(t.model)}</span>` : ""}</div>
    ${t.summary && t.status === "done" ? `<div class="summary">${esc(t.summary)}</div>` : ""}
    ${t.status === "error" && t.error ? `<div class="summary" style="color:var(--err)">${esc(t.error.slice(0, 200))}</div>` : ""}
  </div>`;
}

function bindDnD() {
  let dragId = null;
  document.querySelectorAll(".card").forEach(c => {
    c.addEventListener("dragstart", e => { dragId = +c.dataset.id; c.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
    c.addEventListener("dragend", () => { c.classList.remove("dragging"); document.querySelectorAll(".column.drop").forEach(x => x.classList.remove("drop")); });
  });
  document.querySelectorAll(".column").forEach(col => {
    col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("drop"); });
    col.addEventListener("dragleave", e => { if (!col.contains(e.relatedTarget)) col.classList.remove("drop"); });
    col.addEventListener("drop", async e => {
      e.preventDefault(); col.classList.remove("drop");
      if (!dragId) return;
      const body = col.querySelector(".col-body");
      const cards = [...body.querySelectorAll(".card")].filter(c => +c.dataset.id !== dragId);
      let idx = cards.findIndex(c => e.clientY < c.getBoundingClientRect().top + c.offsetHeight / 2);
      if (idx < 0) idx = cards.length;
      const order = cards.map(c => +c.dataset.id);
      order.splice(idx, 0, dragId);
      const id = dragId; dragId = null;
      await api("POST", `/api/tasks/${id}/move`, { column_id: +col.dataset.col, order }).catch(() => { });
      loadBoard();
    });
  });
}

// ---------------- задача ----------------
function modal(html, cls = "") {
  $("#modal").innerHTML = `<div class="overlay" onmousedown="if(event.target===this)App.closeModal()"><div class="modal ${cls}">${html}</div></div>`;
}

function closeModal() { $("#modal").innerHTML = ""; S.task = null; }

function newTask(columnId) {
  const cols = S.board.columns.filter(c => !["progress", "waiting", "done"].includes(c.kind));
  const def = columnId || S.board.columns.find(c => c.kind === "backlog").id;
  modal(`<h3>Новая задача</h3>
    <label>Заголовок</label><input id="nt-title" autofocus>
    <label>Описание — контекст, что сделать, критерии готовности</label><textarea id="nt-desc" rows="8"></textarea>
    <div class="task-grid">
      <div><label>Колонка</label><select id="nt-col">${cols.map(c => `<option value="${c.id}" ${c.id === def ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></div>
      <div class="row"><div class="grow"><label>Модель</label><select id="nt-model">${opts(MODELS)}</select></div>
        <div class="grow"><label>Effort</label><select id="nt-effort">${opts(EFFORTS)}</select></div></div>
    </div>
    <div class="actions"><button onclick="App.closeModal()">Отмена</button><button class="primary" onclick="App.createTask()">Создать</button></div>`);
  setTimeout(() => $("#nt-title").focus(), 0);
}

async function createTask() {
  const title = $("#nt-title").value.trim();
  if (!title) return toast("Нужен заголовок");
  await api("POST", `/api/projects/${S.projectId}/tasks`, {
    title, description: $("#nt-desc").value, column_id: +$("#nt-col").value, model: $("#nt-model").value, effort: $("#nt-effort").value,
  });
  closeModal(); loadBoard();
}

async function openTask(id) {
  const ok = fresh("task");
  const t = await api("GET", `/api/tasks/${id}`);
  if (!ok()) return;
  S.task = t;
  modal(`
    <div class="row"><input class="title-input grow" id="tt-title" value="${esc(t.title)}"><span class="muted">#${t.id}</span></div>
    <div class="row muted" style="font-size:12px;margin:2px 4px 0" id="tt-line"></div>
    <label>Описание</label><textarea id="tt-desc" rows="6">${esc(t.description || "")}</textarea>
    <div class="row" style="align-items:flex-end">
      <div class="grow"><label>Модель</label><select id="tt-model">${opts(MODELS, t.model)}</select></div>
      <div class="grow"><label>Effort</label><select id="tt-effort">${opts(EFFORTS, t.effort)}</select></div>
      <button onclick="App.saveTask()">Сохранить</button>
    </div>
    <div id="tt-dyn"></div>
    <label>Ход работы</label>
    <div class="timeline" id="tt-tl">${t.events.map(evHtml).join("") || '<div class="muted">Пока пусто</div>'}</div>
    <div class="row" style="margin-top:8px"><input id="tt-comment" placeholder="Комментарий к задаче (агент увидит при следующем запуске)" onkeydown="if(event.key==='Enter')App.comment()"><button onclick="App.comment()">Добавить</button></div>
    <div class="actions" id="tt-actions"></div>`, "wide");
  renderTaskDyn();
  const tl = $("#tt-tl"); tl.scrollTop = tl.scrollHeight;
}

function renderTaskDyn() {
  const t = S.task;
  if (!t || !$("#tt-dyn")) return;
  $("#tt-line").innerHTML = `${esc(t.column_name)} ${pill(t.status)} ${t.cost_usd ? `· ≈$${t.cost_usd.toFixed(2)} (эквивалент API)` : ""}
    ${t.session_id ? `· сессия <code title="claude --resume ${t.session_id}">${t.session_id.slice(0, 8)}</code>` : ""}`;
  const open = t.questions.filter(q => q.status === "open");
  let html = "";
  if (t.status === "error" && t.error) html += `<div class="callout err">${esc(t.error)}</div>`;
  if (t.summary) html += `<div class="callout ok"><b>Итог:</b> ${esc(t.summary)} ${t.report_path ? ` · <a href="/report/${t.id}" target="_blank">подробный отчёт →</a>` : ""}</div>`;
  if (open.length) html += `<label>Ждёт ответа</label>` + open.map(q => questionHtml(q, false)).join("");
  $("#tt-dyn").innerHTML = html;
  $("#tt-actions").innerHTML = `
    <button class="danger" onclick="App.deleteTask(${t.id})">Удалить</button>
    ${t.session_id && !t.running ? `<button onclick="App.resetTask(${t.id})" title="Следующий запуск начнётся с чистой сессии">Сбросить сессию</button>` : ""}
    ${t.events.length ? `<a href="/api/tasks/${t.id}/rawlog" target="_blank"><button>Сырой лог</button></a>` : ""}
    <span class="grow"></span>
    ${t.running || ["running", "resume"].includes(t.status) ? `<button onclick="App.stopTask(${t.id})">Остановить</button>` : ""}
    ${!t.running && t.column_kind !== "todo" && t.status !== "resume" ? `<button class="primary" onclick="App.toTodo(${t.id})">${t.session_id ? "Продолжить / вернуть в работу" : "В работу"}</button>` : ""}`;
}

const EV_NAMES = { text: "Claude", tool: "", progress: "Прогресс", question: "Вопрос", answer: "Ответ", comment: "Ты", system: "Система", finish: "Готово", error: "Ошибка" };
function evHtml(e) {
  const body = e.kind === "text" ? `<div class="body md">${md(e.text)}</div>` : `<span class="body">${esc(e.text)}</span>`;
  return `<div class="ev ${e.kind}"><span class="t">${fmtTime(e.ts)}</span>${EV_NAMES[e.kind] ? `<span class="k">${EV_NAMES[e.kind]}</span>` : ""}${body}</div>`;
}

async function saveTask() {
  const t = S.task;
  await api("PATCH", `/api/tasks/${t.id}`, { title: $("#tt-title").value, description: $("#tt-desc").value, model: $("#tt-model").value, effort: $("#tt-effort").value });
  toast("Сохранено"); loadBoard();
}

async function refreshTask() {
  if (!S.task) return;
  const ok = fresh("task"), id = S.task.id;
  const t = await api("GET", `/api/tasks/${id}`).catch(() => null);
  if (!ok() || S.task?.id !== id) return;
  if (!t) return closeModal();
  const same = JSON.stringify({ ...t, events: 0 }) === JSON.stringify({ ...S.task, events: 0 });
  S.task = t;
  if (!same) renderTaskDyn();  // перерисовка стёрла бы набранный в карточке ответ
}

async function comment() {
  const inp = $("#tt-comment"), text = inp.value.trim();
  if (!text) return;
  await api("POST", `/api/tasks/${S.task.id}/comment`, { text });
  inp.value = "";
}

async function toTodo(id) {
  const col = S.board.columns.find(c => c.kind === "todo");
  await api("POST", `/api/tasks/${id}/move`, { column_id: col.id, order: [...S.board.tasks.filter(t => t.column_id === col.id).map(t => t.id), id] });
  loadBoard(); refreshTask();
}

async function stopTask(id) { await api("POST", `/api/tasks/${id}/stop`); toast("Останавливаю…"); }
async function resetTask(id) { if (confirm("Забыть сессию? Следующий запуск начнёт задачу заново, без памяти о прошлой работе.")) { await api("POST", `/api/tasks/${id}/reset`); refreshTask(); } }
async function deleteTask(id) { if (confirm("Удалить задачу?")) { await api("DELETE", `/api/tasks/${id}`); closeModal(); loadBoard(); } }

// ---------------- вопросы ----------------
function questionHtml(q, withTask = true) {
  const perm = q.kind === "permission";
  const answered = q.status !== "open";
  return `<div class="qcard ${answered ? "answered" : ""}">
    ${withTask ? `<div class="qhead"><span class="dot" style="background:${q.project_color}"></span>${esc(q.project_name)} ·
      <a onclick="App.openTask(${q.task_id})">#${q.task_id} ${esc(q.task_title)}</a><span class="grow"></span>${fmtTime(q.created_at)}</div>` : ""}
    <div class="qtext">${perm ? "🔐 " : ""}${esc(q.text)}</div>
    ${perm && q.payload?.input ? `<pre class="md" style="font-size:12px;max-height:200px;overflow:auto;background:var(--col);padding:8px;border-radius:6px">${esc(JSON.stringify(q.payload.input, null, 2))}</pre>` : ""}
    ${answered ? `<div class="muted">${q.status === "answered" ? "Ответ: " + esc(q.answer) : "Отменён"}</div>` : `
      ${q.options.length ? `<div class="opts">${q.options.map((o, i) => `<button class="${perm && i === 0 ? "primary" : ""}" onclick="App.answer(${q.id}, ${esc(JSON.stringify(o)).replace(/"/g, "&quot;")})">${esc(o)}</button>`).join("")}</div>` : ""}
      ${perm ? "" : `<div class="row"><input id="ans-${q.id}" placeholder="Свой ответ…" onkeydown="if(event.key==='Enter')App.answerText(${q.id})"><button onclick="App.answerText(${q.id})">Ответить</button></div>`}`}
  </div>`;
}

async function renderQuestions() {
  const ok = fresh("questions");
  const list = await api("GET", `/api/questions?status=${S.showAnswered ? "all" : "open"}`);
  if (!ok() || S.view !== "questions") return;
  $("#view").innerHTML = `<div class="pad">
    <div class="row"><h2 class="grow" style="margin:0">Вопросы агентов</h2>
      <label class="row" style="margin:0;color:var(--text)"><input type="checkbox" style="width:auto" ${S.showAnswered ? "checked" : ""} onchange="App.toggleAnswered(this.checked)"> показать закрытые</label></div>
    <p class="muted">Здесь задачи, которым нужно твоё решение. После ответа агент продолжит работу сам.</p>
    ${list.map(q => questionHtml(q)).join("") || '<div class="empty-board">Открытых вопросов нет 🎉</div>'}</div>`;
}

async function answer(qid, text) {
  await api("POST", `/api/questions/${qid}/answer`, { answer: text });
  toast("Ответ отправлен");
  if (S.view === "questions") renderQuestions();
  refreshTask();
}
function answerText(qid) { const v = $(`#ans-${qid}`).value.trim(); if (v) answer(qid, v); }

// ---------------- настройки ----------------
function renderSettings() {
  const s = S.state.settings, p = project();
  $("#view").innerHTML = `<div class="pad">
    <h2>Настройки</h2>
    <div class="section"><h3>Выключение пайплайна</h3>
      <label class="radio"><input type="radio" name="stop" value="graceful" ${s.stop_mode === "graceful" ? "checked" : ""} onchange="App.setSetting('stop_mode','graceful')">
        <span><b>Доделать текущие задачи</b> и выключиться<div class="hint">Новые задачи не берутся, начатые завершаются.</div></span></label>
      <label class="radio"><input type="radio" name="stop" value="immediate" ${s.stop_mode === "immediate" ? "checked" : ""} onchange="App.setSetting('stop_mode','immediate')">
        <span><b>Прервать сразу</b><div class="hint">Процессы останавливаются, сессии сохраняются — при следующем включении задачи продолжатся с того же места.</div></span></label>
    </div>
    <div class="section"><h3>Выполнение</h3>
      <label>Сколько задач выполнять одновременно (в одном проекте — всегда по одной)</label>
      <input type="number" min="1" max="10" value="${s.max_parallel}" style="width:120px" onchange="App.setSetting('max_parallel', Math.max(1, +this.value || 1))">
      <label>Режим разрешений</label>
      <select onchange="App.setSetting('permission_mode', this.value)">${opts([
        ["bypassPermissions", "Без вопросов (bypassPermissions) — агент делает всё сам"],
        ["acceptEdits", "Правки файлов сами, остальное — спрашивать в «Вопросах»"],
        ["default", "Спрашивать разрешение на всё, кроме чтения"]], s.permission_mode)}</select>
      <div class="hint">Запросы разрешений приходят на страницу «Вопросы» как тикеты «Разрешить / Запретить».</div>
      <div class="row">
        <div class="grow"><label>Модель для задач</label><select onchange="App.setSetting('model', this.value)">${opts(MODELS, s.model)}</select></div>
        <div class="grow"><label>Effort для задач</label><select onchange="App.setSetting('effort', this.value)">${opts(EFFORTS, s.effort)}</select></div>
        <div class="grow"><label>Модель для чата и импорта</label><select onchange="App.setSetting('chat_model', this.value)">${opts(MODELS, s.chat_model)}</select></div>
      </div>
      <label>Дополнительные указания всем агентам задач</label>
      <textarea rows="4" placeholder="Например: «Не делай git commit», «Код Unity проверяй через UnityMCP»" onchange="App.setSetting('extra_prompt', this.value)">${esc(s.extra_prompt)}</textarea>
      ${+s.pause_until * 1000 > Date.now() ? `<p>Сейчас пауза по лимиту. <button onclick="App.clearPause()">Снять паузу</button></p>` : ""}
    </div>
    <div class="section" id="upd-sec"></div>
    <div class="section"><h3>Сервер трекера</h3>
      <div class="hint">Перезапуск нужен, чтобы вступили в силу изменения в коде трекера. Он дождётся, пока доработают выполняющиеся задачи и ответ чата
        (не дольше 10 минут, потом прервёт их — они продолжатся после старта), новые задачи до этого не берутся. Страница перезагрузится сама.</div>
      <p>${S.state.pipeline.restart
        ? `<span class="muted">Перезапуск запрошен${S.state.pipeline.restart.busy ? `, ждёт завершения запусков: ${S.state.pipeline.restart.busy}` : "…"}</span>`
        : `<button onclick="App.restartTracker()">Перезапустить трекер</button>`}</p>
    </div>
    ${p ? `<div class="section"><h3>Проект «${esc(p.name)}»</h3>
      <label>Название</label><input id="ps-name" value="${esc(p.name)}">
      <label>Папка</label><div class="row"><input id="ps-path" value="${esc(p.path)}"><button onclick="App.pickFolder('#ps-path')">Обзор…</button></div>
      <label>Цвет</label><div class="row">${COLORS.map(c => `<button class="ghost" style="padding:4px" onclick="App.updateProject({color:'${c}'})"><span class="dot" style="background:${c};width:18px;height:18px;display:block;${c === p.color ? "outline:2px solid var(--text);outline-offset:2px" : ""}"></span></button>`).join("")}</div>
      <div class="actions"><button class="danger" onclick="App.deleteProject()">Удалить проект</button><span class="grow"></span>
        <button class="primary" onclick="App.updateProject({name: $('#ps-name').value, path: $('#ps-path').value})">Сохранить</button></div>
    </div>` : ""}
  </div>`;
  renderUpdateSection();
}

async function setSetting(k, v) { await api("POST", "/api/settings", { [k]: v }); await loadState(); toast("Сохранено"); }
async function restartTracker() {
  if (!confirm("Перезапустить трекер? Он дождётся завершения выполняющихся задач и ответа чата (не дольше 10 минут), потом перезапустится, а страница перезагрузится.")) return;
  S.state.pipeline = await api("POST", "/api/restart");
  renderHeader(); if (S.view === "settings") renderSettings();
  toast(S.state.pipeline.restart?.busy ? "Перезапущу, когда доработают текущие запуски" : "Перезапускаю…");
}
async function clearPause() { await api("POST", "/api/settings", { clear_pause: true }); await loadState(); renderSettings(); }

// ---------------- обновления ----------------
const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
const changesText = n => `${n} ${plural(n, "изменение", "изменения", "изменений")}`;
const commitsHtml = u => `<ul class="commits">${u.commits.map(c => `<li><code>${esc(c.hash)}</code> ${esc(c.subject)}
  <span class="muted">· ${esc(c.author)}, ${fmtTime(c.ts)}</span></li>`).join("")}${u.behind > u.commits.length ? `<li class="muted">… и ещё ${u.behind - u.commits.length}</li>` : ""}</ul>`;

// плашка под шапкой: есть новая версия / обновляю / обновлено, ждём перезапуска
function renderUpdate() {
  const u = S.state?.update, r = S.state?.pipeline?.restart, bar = $("#updbar");
  let html = "", cls = "";
  if (u?.updated && (r || S.restarting)) {
    cls = "ok"; html = `<span class="spinner"></span><b>Обновлено до ${esc(u.updated.to)}</b>
      <span>перезапуск после завершения текущих задач…${r?.busy ? ` (ждёт: ${r.busy})` : ""}</span>`;
  } else if (u?.updated?.broken) {
    cls = "err"; html = `<b>Код обновлён до ${esc(u.updated.to)}, но не компилируется — перезапуск отменён, работает прежняя версия.</b>
      <div class="full">${esc(u.updated.broken)}. Исправь ошибку (или откати: <code>git reset --hard ${esc(u.updated.from)}</code>) и перезапусти трекер.</div>`;
  } else if (u?.updated) {
    cls = "err"; html = `<b>Код обновлён до ${esc(u.updated.to)}, но трекер не перезапустился.</b><span class="grow"></span>
      <button onclick="App.restartTracker()">Перезапустить</button>`;
  } else if (u?.updating) {
    html = `<span class="spinner"></span><b>Обновляю трекер…</b>`;
  } else if (u?.behind > 0 && ls.get("updHidden", "") !== u.remote) {
    html = `<b>Доступна новая версия (${changesText(u.behind)})</b>
      <button class="ghost" onclick="App.toggleUpdList()">${S.updOpen ? "скрыть список ▴" : "что нового ▾"}</button>
      <span class="grow"></span>
      <button class="ghost" onclick="App.hideUpdate()" title="Не показывать до следующей новой версии">Позже</button>
      <button class="primary" onclick="App.applyUpdate()">Обновить</button>
      ${S.updOpen ? `<div class="full">${commitsHtml(u)}</div>` : ""}
      ${u.update_error ? `<div class="full callout err">${esc(u.update_error)}</div>` : ""}`;
  }
  bar.className = `updbar ${cls} ${html ? "" : "hidden"}`;
  bar.innerHTML = html;
  if (S.view === "settings") renderUpdateSection();
}

function renderUpdateSection() {
  const box = $("#upd-sec"), u = S.state.update;
  if (!box || !u) return;
  const s = S.state.settings;
  let status;
  if (u.checking) status = `<span class="spinner"></span> Проверяю…`;
  else if (u.error) status = `<span class="err-text">${esc(u.error)}</span>`;
  else if (!u.checked_at) status = `<span class="muted">Ещё не проверялось</span>`;
  else if (u.behind) status = `<b>Доступна новая версия: ${changesText(u.behind)}</b>`;
  else status = "Установлена последняя версия";
  box.innerHTML = `<h3>Обновления</h3>
    <div class="hint">Трекер сравнивает свою папку с веткой main на GitHub. «Обновить» делает <code>git pull --ff-only</code>
      и перезапускает трекер, когда доработают текущие задачи. Если в папке есть незакоммиченные изменения или свои коммиты, обновление
      откажет и объяснит, что сделать: локальная работа не теряется.</div>
    <p>${status}</p>
    ${u.head ? `<div class="hint">Версия: ветка <code>${esc(u.branch)}</code>, коммит <code>${esc(u.head)}</code>${u.remote ? `, на GitHub — <code>${esc(u.remote)}</code>` : ""}${u.ahead ? ` · локальных коммитов, которых нет на GitHub: ${u.ahead}` : ""}${u.checked_at ? ` · проверено ${fmtTime(u.checked_at)}` : ""}</div>` : ""}
    ${u.behind && u.commits.length ? commitsHtml(u) : ""}
    ${u.update_error ? `<div class="callout err">${esc(u.update_error)}</div>` : ""}
    <label>Проверять раз в, минут (0 — только вручную)</label>
    <div class="row"><input type="number" min="0" value="${esc(s.update_check_minutes)}" style="width:120px" onchange="App.setSetting('update_check_minutes', Math.max(0, +this.value || 0))">
      <button onclick="App.checkUpdate()" ${u.checking || u.updating ? "disabled" : ""}>Проверить обновления</button>
      ${u.behind && !u.updated ? `<button class="primary" onclick="App.applyUpdate()" ${u.updating ? "disabled" : ""}>Обновить</button>` : ""}</div>`;
}

async function checkUpdate() {
  S.state.update = { ...S.state.update, checking: true }; renderUpdate();
  S.state.update = await api("POST", "/api/update/check").catch(() => S.state.update);
  renderUpdate();
  const u = S.state.update;
  toast(u.error ? "Не удалось проверить обновления" : u.behind ? `Доступна новая версия: ${changesText(u.behind)}` : "Установлена последняя версия");
}

async function applyUpdate() {
  if (!confirm("Обновить трекер? Новая версия скачается сразу, а перезапуск будет, когда доработают текущие задачи и ответ чата. Страница перезагрузится сама.")) return;
  S.updOpen = false;
  const r = await api("POST", "/api/update").catch(() => null);
  if (!r) return;
  S.state.update = r.update; S.state.pipeline = r.pipeline;
  renderHeader(); toast(r.message);
}

function toggleUpdList() { S.updOpen = !S.updOpen; renderUpdate(); }
function hideUpdate() { ls.set("updHidden", S.state.update.remote); renderUpdate(); }

// ---------------- проекты ----------------
function newProject() {
  modal(`<h3>Новый проект</h3>
    <label>Название</label><input id="np-name" placeholder="Например: Моя игра">
    <label>Папка проекта — здесь Claude будет работать</label>
    <div class="row"><input id="np-path" placeholder="C:\\Projects\\MyGame"><button onclick="App.pickFolder('#np-path')">Обзор…</button></div>
    <div class="actions"><button onclick="App.closeModal()">Отмена</button><button class="primary" onclick="App.createProject()">Создать</button></div>`);
  setTimeout(() => $("#np-name").focus(), 0);
}

async function pickFolder(sel) {
  toast("Открываю выбор папки…");
  const r = await api("POST", "/api/pick-folder");
  if (r.path) {
    $(sel).value = r.path;
    const name = $("#np-name");
    if (name && !name.value) name.value = r.path.split(/[\\/]/).pop();
  }
}

async function createProject() {
  const color = COLORS[(S.state.projects.length) % COLORS.length];
  const p = await api("POST", "/api/projects", { name: $("#np-name").value, path: $("#np-path").value, color });
  closeModal();
  S.projectId = p.id; ls.set("project", p.id);
  await loadState(); S.view = "board"; render(); loadBoard(); loadChat();
}

async function updateProject(fields) { await api("PATCH", `/api/projects/${S.projectId}`, fields); await loadState(); renderSettings(); toast("Сохранено"); }
async function setProjectActive(on) { await api("PATCH", `/api/projects/${S.projectId}`, { active: on ? 1 : 0 }); await loadState(); }
async function deleteProject() {
  const p = project();
  if (!confirm(`Удалить проект «${p.name}» со всеми задачами? Файлы в папке проекта не трогаются.`)) return;
  await api("DELETE", `/api/projects/${p.id}`);
  S.projectId = null; await loadState(); S.view = "board"; render(); loadBoard(); loadChat();
}

async function selectProject(id) {
  if (id !== S.projectId) clearAtts();
  S.projectId = id; ls.set("project", id);
  if (S.view === "settings") renderSettings();
  if (S.view !== "board") { S.view = "board"; ls.set("view", "board"); }
  render(); loadBoard(); loadChat();
}

// ---------------- колонки ----------------
async function addColumn() {
  const name = prompt("Название колонки");
  if (name?.trim()) { await api("POST", `/api/projects/${S.projectId}/columns`, { name }); loadBoard(); }
}

async function columnMenu(cid) {
  const c = S.board.columns.find(x => x.id === cid);
  const name = prompt(`Новое имя колонки «${c.name}» (пусто — удалить колонку, задачи уйдут в бэклог)`, c.name);
  if (name === null) return;
  if (!name.trim()) { if (confirm("Удалить колонку?")) await api("DELETE", `/api/columns/${cid}`); }
  else await api("PATCH", `/api/columns/${cid}`, { name });
  loadBoard();
}

// ---------------- импорт ----------------
function importDialog() {
  modal(`<h3>Импорт задач текстом</h3>
    <p class="muted">Вставь текст из другого трекера, письма или заметок — одну задачу или сразу пачку. Claude разберёт его на карточки, ты проверишь перед созданием.</p>
    <textarea id="im-text" rows="12" placeholder="Вставь сюда…"></textarea>
    <div id="im-result"></div>
    <div class="actions" id="im-actions"><button onclick="App.closeModal()">Отмена</button><button class="primary" id="im-go" onclick="App.importParse()">Разобрать</button></div>`, "wide");
  setTimeout(() => $("#im-text").focus(), 0);
}

async function importParse() {
  const text = $("#im-text").value.trim();
  if (!text) return;
  $("#im-go").disabled = true;
  $("#im-go").innerHTML = '<span class="spinner"></span> Разбираю…';
  let r;
  try { r = await api("POST", `/api/projects/${S.projectId}/import`, { text }); }
  finally { const b = $("#im-go"); if (b) { b.disabled = false; b.textContent = "Разобрать заново"; } }
  if (!r.tasks.length) return toast("Задач не нашлось");
  const cols = S.board.columns.filter(c => !["progress", "waiting", "done"].includes(c.kind));
  $("#im-result").innerHTML = `<label>Найдено задач: ${r.tasks.length}. Проверь и поправь:</label>` +
    r.tasks.map((t, i) => `<div class="import-item"><input type="checkbox" checked data-i="${i}">
      <div><input class="im-title" value="${esc(t.title)}" style="font-weight:600;margin-bottom:6px"><textarea class="im-desc" rows="3">${esc(t.description)}</textarea></div></div>`).join("");
  $("#im-actions").innerHTML = `<select id="im-col" style="width:auto">${cols.map(c => `<option value="${c.id}" ${c.kind === "backlog" ? "selected" : ""}>в «${esc(c.name)}»</option>`).join("")}</select>
    <button onclick="App.closeModal()">Отмена</button><button id="im-go" onclick="App.importParse()">Разобрать заново</button>
    <button class="primary" onclick="App.importCommit()">Создать задачи</button>`;
}

async function importCommit() {
  const tasks = [...document.querySelectorAll(".import-item")].filter(el => el.querySelector("input[type=checkbox]").checked)
    .map(el => ({ title: el.querySelector(".im-title").value, description: el.querySelector(".im-desc").value }));
  await api("POST", `/api/projects/${S.projectId}/import/commit`, { tasks, column_id: +$("#im-col").value });
  closeModal(); toast(`Создано задач: ${tasks.length}`); loadBoard();
}

// ---------------- чат ----------------
const chatName = c => c?.title || "Новый чат";

function renderChatHead() {
  const cur = S.chats.find(c => c.id === S.chat.id), p = project();
  const others = S.chats.filter(c => c.running && c.id !== S.chat.id).length;
  $("#chatTitle").textContent = cur ? chatName(cur) : "Чат";
  $("#chatPick").title = `Чаты проекта «${p?.name || ""}»` + (others ? ` · отвечает в другом чате: ${others}` : "");
  $("#chatOthers").classList.toggle("hidden", !others);
  const list = $("#chatList");
  list.classList.toggle("hidden", !S.chatListOpen);
  if (!S.chatListOpen) return;
  list.innerHTML = S.chats.map(c => `<div class="chat-item ${c.id === S.chat.id ? "active" : ""}" onclick="App.openChat(${c.id})">
      <div class="grow"><div class="ci-title">${c.running ? '<span class="pulse"></span>' : ""}${esc(chatName(c))}</div>
      <small>${c.running ? "отвечает · " : ""}${fmtTime(c.updated_at)} · сообщений: ${c.messages}</small></div>
      <button class="ghost" onclick="event.stopPropagation(); App.renameChat(${c.id})" title="Переименовать">✎</button>
      <button class="ghost" onclick="event.stopPropagation(); App.deleteChat(${c.id})" title="Удалить чат">🗑</button></div>`).join("") ||
    `<div class="muted" style="padding:12px">Чатов пока нет — просто напиши сообщение.</div>`;
}

function renderChat(toBottom) {
  renderChatHead();
  const box = $("#msgs");
  const atBottom = toBottom || box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = S.chat.messages.map(msgHtml).join("") ||
    `<div class="muted" style="padding:20px;text-align:center">Это чат с Claude внутри трекера. Он видит доску и может создавать и двигать задачи, отвечать про статус, включать пайплайн. Можно вставить сюда текст задач — он их заведёт.</div>`;
  $("#typing").classList.toggle("hidden", !S.chat.running);
  $("#chatStop").classList.toggle("hidden", !S.chat.running);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

const msgHtml = m => {
  if (m.role === "assistant") return `<div class="msg assistant md">${md(m.text)}</div>`;
  const atts = m.attachments?.length ? `<div class="msg-atts">${m.attachments.map((a, i) => attHtml(m.id, a, i)).join("")}</div>` : "";
  return `<div class="msg ${m.role}">${m.text ? esc(m.text) : ""}${atts}</div>`;
};

const isImage = type => /^image\/(png|jpeg|gif|webp|bmp)$/.test(type || "");
const fmtSize = n => n < 1024 ? `${n} Б` : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} КБ` : `${(n / 1024 / 1024).toFixed(1)} МБ`;

function attHtml(mid, a, i) {
  const url = `/api/chat/${mid}/files/${i}`;
  return isImage(a.type)
    ? `<a class="att-img" href="${url}" target="_blank" title="${esc(a.name)}"><img src="${url}" loading="lazy" alt="${esc(a.name)}"></a>`
    : `<a class="att-file" href="${url}" target="_blank" title="${esc(a.name)}">📄 <span>${esc(a.name)}</span> <small>${fmtSize(a.size)}</small></a>`;
}

// ---------- вложения до отправки ----------
const MAX_FILE = 20 * 1024 * 1024, MAX_FILES = 10, MAX_TOTAL = 100 * 1024 * 1024;
S.atts = [];  // [{file, name, preview}]

function addFiles(files) {
  for (let file of files) {
    if (S.atts.length >= MAX_FILES) { toast(`Не больше ${MAX_FILES} вложений в одном сообщении`); break; }
    if (file.size > MAX_FILE) { toast(`«${file.name}» больше 20 МБ (${fmtSize(file.size)}) — не прикрепляю`); continue; }
    if (S.atts.reduce((s, a) => s + a.file.size, 0) + file.size > MAX_TOTAL) { toast("Вложения вместе больше 100 МБ"); break; }
    let name = file.name || "file";
    if (/^image\.\w+$/.test(name)) {  // скриншот из буфера приходит как image.png
      const d = new Date(), p = n => String(n).padStart(2, "0");
      name = `screenshot-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}${S.atts.length ? "-" + S.atts.length : ""}.${name.split(".").pop()}`;
    }
    S.atts.push({ file, name, preview: isImage(file.type) ? URL.createObjectURL(file) : null });
  }
  renderAtts();
}

function removeAtt(i) {
  const [a] = S.atts.splice(i, 1);
  if (a?.preview) URL.revokeObjectURL(a.preview);
  renderAtts();
}

function clearAtts() { S.atts.forEach(a => a.preview && URL.revokeObjectURL(a.preview)); S.atts = []; renderAtts(); }

function renderAtts() {
  const box = $("#chatAtts");
  box.classList.toggle("hidden", !S.atts.length);
  box.innerHTML = S.atts.map((a, i) => `<div class="att-chip" title="${esc(a.name)}">
    ${a.preview ? `<img src="${a.preview}" alt="">` : "📄"}<span class="name">${esc(a.name)}</span><small>${fmtSize(a.file.size)}</small>
    <button class="ghost" onclick="App.removeAtt(${i})" title="Убрать">✕</button></div>`).join("");
}

const fileBase64 = file => new Promise((ok, fail) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",", 2)[1] || "");
  r.onerror = () => fail(r.error);
  r.readAsDataURL(file);
});

async function chatSend() {
  const inp = $("#chatInput"), text = inp.value.trim();
  if ((!text && !S.atts.length) || !S.projectId || S.chatSending) return;
  if (S.chat.running) return toast("Подожди, я ещё отвечаю в этом чате");
  S.chatSending = true;
  try {
    const attachments = await Promise.all(S.atts.map(async a => ({ name: a.name, type: a.file.type, data: await fileBase64(a.file) })));
    inp.value = "";
    if (!S.chat.id) await openChat((await api("POST", `/api/projects/${S.projectId}/chats`)).id);  // первое сообщение в проекте
    await api("POST", `/api/chats/${S.chat.id}/messages`, { text, attachments });
    clearAtts();
  } catch (e) {
    if (!inp.value) inp.value = text;
  } finally {
    S.chatSending = false;
  }
}

async function chatStop() { if (S.chat.id) await api("POST", `/api/chats/${S.chat.id}/stop`); }

// «Новый» ничего не удаляет: старые чаты остаются в списке
async function chatNew() {
  const c = await api("POST", `/api/projects/${S.projectId}/chats`);
  await loadChats(); await openChat(c.id);
  $("#chatInput").focus();
}

function toggleChatList() {
  S.chatListOpen = !S.chatListOpen; renderChatHead();
  if (S.chatListOpen) loadChats().catch(() => { });  // даты и счётчики свежие
}

async function renameChat(id) {
  const c = S.chats.find(x => x.id === id);
  const title = prompt("Название чата", chatName(c));
  if (!title?.trim()) return;
  await api("PATCH", `/api/chats/${id}`, { title });
  loadChats();
}

async function deleteChat(id) {
  const c = S.chats.find(x => x.id === id);
  if (!confirm(`Удалить чат «${chatName(c)}»? Его сообщения и вложения будут стёрты, остальные чаты не тронутся.`)) return;
  await api("DELETE", `/api/chats/${id}`);
  S.chats = S.chats.filter(x => x.id !== id);
  if (id === S.chat.id) await openChat(S.chats[0]?.id || null);
  S.chatListOpen = true; renderChatHead();
}
function toggleChat() { S.chatOpen = !S.chatOpen; ls.set("chatOpen", S.chatOpen); render(); if (S.chatOpen) loadChat(); }

// ---------------- пайплайн ----------------
async function togglePipeline() {
  const en = S.state.pipeline.enabled;
  const turnOn = en !== "1";
  if (turnOn && !S.state.projects.length) return toast("Сначала создай проект");
  S.state.pipeline = await api("POST", "/api/pipeline", { enabled: turnOn });
  renderHeader();
  if (!turnOn) toast(S.state.settings.stop_mode === "immediate" ? "Пайплайн остановлен" : "Доделаю текущие задачи и выключусь");
}

// ---------------- перезапуск сервера ----------------
// boot_id меняется при каждом старте сервера: другой — значит, трекер перезапустился и, возможно, обновился
function bootChanged(id) {
  if (!id) return false;
  if (!S.bootId) S.bootId = id;
  if (id === S.bootId) return false;
  location.reload();
  return true;
}

function setOffline(on) {
  const el = $("#offline");
  el.textContent = S.restarting || S.state?.pipeline?.restart ? "Трекер перезапускается…" : "Нет связи с трекером, переподключаюсь…";
  el.classList.toggle("hidden", !on);
}

// ---------------- события сервера ----------------
const refreshBoard = debounce(() => { if (S.view === "board") loadBoard(); });
const refreshState = debounce(() => loadState());
const refreshTaskD = debounce(() => refreshTask(), 250);
const refreshQuestions = debounce(() => { if (S.view === "questions") renderQuestions(); });
const refreshChats = debounce(() => loadChats().catch(() => { }));

// сверка с сервером; full — ещё чат и вопросы (после переподключения, когда события могли потеряться)
async function resync(full) {
  await loadState().catch(() => { });
  loadBoard().catch(() => { }); refreshTask();
  if (full) { loadChat().catch(() => { }); refreshQuestions(); }
}
const resyncD = debounce(() => resync(), 300);

function onEvent(m) {
  if (m.type === "resync") {
    setOffline(false); resync(true);
  } else if (m.type === "hello") {
    bootChanged(m.boot_id);
  } else if (m.type === "restarting") {
    S.restarting = true; setOffline(true);
  } else if (m.type === "offline") {
    setOffline(true);
  } else if (m.type === "changed") {
    if (!m.project_id || m.project_id === S.projectId) refreshBoard();
    if (!m.project_id) refreshState();
    refreshTaskD(); refreshQuestions();
  } else if (m.type === "state") {
    const wasRestart = !!S.state.pipeline.restart;
    S.state.pipeline = m.state; renderHeader(); refreshBoard();
    if (S.restarting && !m.state.restart) { S.restarting = false; setOffline(false); }  // перезапуск не удался, работаем дальше
    if (S.view === "settings" && wasRestart !== !!m.state.restart) renderSettings();
  } else if (m.type === "update") {
    if (S.state) { S.state.update = m.update; renderUpdate(); }
  } else if (m.type === "questions") {
    refreshState(); refreshQuestions(); refreshTaskD();
  } else if (m.type === "task_event") {
    if (S.task && S.task.id === m.task_id) {
      S.task.events.push(m.event);
      const tl = $("#tt-tl");
      if (tl) {
        const atBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 60;
        if (S.task.events.length === 1) tl.innerHTML = "";
        tl.insertAdjacentHTML("beforeend", evHtml(m.event));
        if (atBottom) tl.scrollTop = tl.scrollHeight;
      }
    }
  } else if (m.type === "chat" && m.chat_id === S.chat.id) {  // сообщения других чатов сюда не попадают
    if (!S.chat.messages.some(x => x.id === m.message.id)) { S.chat.messages.push(m.message); renderChat(); }
  } else if (m.type === "chat_state" && m.project_id === S.projectId) {
    const c = S.chats.find(x => x.id === m.chat_id);
    if (c) c.running = m.running; else refreshChats();
    if (m.chat_id === S.chat.id) { S.chat.running = m.running; renderChat(); } else renderChatHead();
  } else if (m.type === "chats" && m.project_id === S.projectId) {
    if (m.deleted && m.chat_id === S.chat.id) { S.chat = { id: null, pid: null, messages: [], running: false }; loadChat().catch(() => { }); }
    else refreshChats();
  }
}

// Одно SSE-соединение на все вкладки: по HTTP/1.1 браузер держит к хосту ~6 соединений, и вкладки, у каждой из
// которых свой EventSource, быстро их съедают. Сервер слушает вкладка, взявшая Web Lock, остальным пересылает
// события через BroadcastChannel. Закрылась она — замок переходит к следующей.
function connect() {
  const bc = window.BroadcastChannel && navigator.locks ? new BroadcastChannel("cp-events") : null;
  const listen = () => {
    const es = new EventSource("/api/events");
    es.onmessage = e => { const m = JSON.parse(e.data); onEvent(m); bc?.postMessage(m); };
    // после (пере)подключения перечитываем всё во всех вкладках: пока связи не было, события терялись
    es.onopen = () => { setOffline(false); resync(true); bc?.postMessage({ type: "resync" }); };
    es.onerror = () => {
      setOffline(true); bc?.postMessage({ type: "offline" });
      if (es.readyState === EventSource.CLOSED) setTimeout(listen, 3000);
    };
  };
  if (!bc) return listen();
  bc.onmessage = e => onEvent(e.data);
  navigator.locks.request("cp-events", () => { listen(); return new Promise(() => { }); });
}

// подстраховка: видимая вкладка сверяется с сервером раз в 20 с и сразу, когда на неё вернулись
setInterval(() => { if (document.visibilityState === "visible") resync(); }, 20000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") resyncD(); });
window.addEventListener("focus", resyncD);

// пока открыт другой вид, события доску не перечитывают — при возврате берём свежую
function setView(v) { S.view = v; ls.set("view", v); render(); if (v === "board") loadBoard(); }
function toggleAnswered(on) { S.showAnswered = on; renderQuestions(); }

$("#chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatSend(); }
});
// Ctrl+V со скриншотом или скопированными файлами; обычный текст вставляется как раньше
$("#chatInput").addEventListener("paste", e => {
  const dt = e.clipboardData;
  if (!dt?.files.length || dt.types.includes("text/plain")) return;
  e.preventDefault();
  addFiles(dt.files);
});
$("#chatFile").addEventListener("change", e => { addFiles(e.target.files); e.target.value = ""; });
// перетаскивание файлов в панель чата
{
  const chatEl = $("#chat"), hint = $("#dropHint");
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
  chatEl.addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; hint.classList.remove("hidden"); });
  chatEl.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  chatEl.addEventListener("dragleave", e => { if (hasFiles(e) && --depth <= 0) { depth = 0; hint.classList.add("hidden"); } });
  chatEl.addEventListener("drop", e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; hint.classList.add("hidden");
    addFiles(e.dataTransfer.files);
  });
  // файл, брошенный мимо чата, не должен открываться вместо трекера
  document.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener("drop", e => { if (hasFiles(e)) e.preventDefault(); });
}
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if ($("#modal").innerHTML) closeModal();
  else if (S.chatListOpen) { S.chatListOpen = false; renderChatHead(); }
});
// список чатов закрывается кликом мимо него
document.addEventListener("click", e => {
  if (S.chatListOpen && !e.target.closest("#chatList, #chatPick")) { S.chatListOpen = false; renderChatHead(); }
});

window.App = {
  setView, selectProject, newProject, createProject, pickFolder, updateProject, setProjectActive, deleteProject,
  newTask, createTask, openTask, saveTask, comment, toTodo, stopTask, resetTask, deleteTask, closeModal,
  answer, answerText, toggleAnswered, setSetting, clearPause, restartTracker, addColumn, columnMenu,
  checkUpdate, applyUpdate, toggleUpdList, hideUpdate,
  importDialog, importParse, importCommit, toggleChat, chatSend, chatStop, chatNew, toggleChatList, openChat, renameChat, deleteChat, removeAtt, togglePipeline,
};

(async () => {
  await loadState();
  render();
  connect();  // подписываемся до загрузки доски и чата, чтобы не потерять события, пришедшие во время неё
  await loadBoard();
  if (S.chatOpen) loadChat();
})();
