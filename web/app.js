// AgentPipeline — клиент трекера (без фреймворков).
const $ = s => document.querySelector(s);
const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } },
};

function telegramDefault() {
  const saved = ls.get("telegram.results", null);
  if (typeof saved === "boolean") return saved;
  return navigator.userAgentData?.mobile === true || /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function telegramDeviceControl() {
  const ready = S.state?.telegram?.auth === "ready";
  return `<label class="telegram-choice"><input type="checkbox" ${telegramDefault() ? "checked" : ""}
    onchange="App.setTelegramDefault(this.checked)"> Результаты в Telegram</label>
    ${!ready ? '<button class="ghost" onclick="App.openTelegramSettings()">Подключить Telegram</button>' : ""}`;
}

function setTelegramDefault(value) {
  ls.set("telegram.results", value);
  document.querySelectorAll('.telegram-device .telegram-choice input').forEach(el => el.checked = value);
  if (value && S.state?.telegram?.auth !== "ready") openTelegramSettings();
}

function telegramTaskChoice(id, checked) {
  return `<label class="telegram-choice"><input type="checkbox" id="${id}" ${checked ? "checked" : ""}
    ${id === "tt-telegram" ? 'onchange="App.setTaskTelegram(this.checked)"' : ""}> Отправить результат в Telegram</label>
    ${S.state?.telegram?.auth !== "ready" ? '<div class="hint">Нужно подключить свой аккаунт. <a href="/?setup=telegram" target="_blank" rel="noopener">Настроить Telegram →</a></div>' : ""}`;
}

const STATUS = {
  idle: null, queued: ["в очереди", "st-queued"], running: ["выполняется", "st-running"], resume: ["продолжит", "st-resume"],
  waiting: ["ждёт ответа", "st-waiting"], done: ["готово", "st-done"], error: ["ошибка", "st-error"],
  interrupted: ["прервано", "st-interrupted"],
};
let MODELS = [["", "по умолчанию"], ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"], ["fable", "Fable"]];
let EFFORTS = [["", "по умолчанию"], ["low", "low"], ["medium", "medium"], ["high", "high"], ["xhigh", "xhigh"], ["max", "max"]];
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
// #12 в уже экранированном тексте -> ссылка, открывающая карточку задачи
const refs = html => html.replace(/(^|[\s(>«,;:])#(\d+)\b/g, (_, pre, id) => `${pre}<a href="#" class="tref" onclick="event.preventDefault(); App.openTask(${id})">#${id}</a>`);

// ---------------- загрузка ----------------
// Запросы одного вида не упорядочены: ответ, после которого ушёл более новый запрос, устарел и отбрасывается.
const SEQ = {};
const fresh = k => { const n = SEQ[k] = (SEQ[k] || 0) + 1; return () => SEQ[k] === n; };

function effortList(model, useDefault = false) {
  if (S.state?.agent !== "codex") return EFFORTS;
  const models = S.state.agent_info?.models || [];
  const selected = useDefault ? model : model || S.state.settings.model;
  const m = selected ? models.find(m => (m.model || m.id) === selected) : models.find(m => m.isDefault);
  return [["", "по умолчанию"], ...(m?.supportedReasoningEfforts || []).map(e => [e.reasoningEffort, e.reasoningEffort])];
}
function taskModelChanged(prefix) {
  const effort = $(`#${prefix}-effort`);
  effort.innerHTML = opts(effortList($(`#${prefix}-model`).value), effort.value);
}

async function loadState() {
  const ok = fresh("state");
  const st = await api("GET", "/api/state");
  if (!ok()) return;
  if (bootChanged(st.boot_id)) return;
  const telegramChanged = JSON.stringify(S.state?.telegram) !== JSON.stringify(st.telegram);
  S.state = st;
  document.documentElement.dataset.agent = st.agent === "codex" ? "codex" : "claude";
  if (st.agent_info?.models?.length) {
    MODELS = [["", "по умолчанию"], ...st.agent_info.models.filter(m => !m.hidden).map(m => [m.model || m.id, m.displayName || m.model || m.id])];
  }
  EV_NAMES.text = "Агент";  // история может содержать ответы обоих исполнителей
  document.title = "AgentPipeline — " + (st.agent_name || "Claude");
  const logo = document.querySelector(".logo");
  if (logo) logo.innerHTML = '<span class="mark">≡</span>AgentPipeline · ' + esc(st.agent_name || "Claude");
  if (!project() && S.state.projects.length) S.projectId = S.state.projects[0].id;
  renderHeader();
  renderWelcome();
  if (telegramChanged && S.view === "settings") renderTelegramSettings();
  if (telegramChanged && S.view === "onboarding" && onboardingStep === 2) refreshOnboardingTelegram();
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
  document.querySelectorAll(".telegram-device").forEach(el => el.innerHTML = telegramDeviceControl());
  const pl = st.pipeline, tg = $("#ptoggle");
  tg.classList.toggle("on", pl.enabled === "1");
  tg.classList.toggle("stopping", pl.enabled === "stopping");
  let text = "Выключен";
  if (pl.restart) text = `Перезапуск трекера${pl.restart.busy ? ` · ждёт завершения: ${pl.restart.busy}` : "…"}`;
  else if (pl.enabled === "stopping") text = `Доделывает (${pl.running.length}) и выключится`;
  else if (pl.enabled === "1") {
    if (pl.pause_until * 1000 > Date.now()) text = `Пауза по лимиту ${pl.pause_estimated ? "(повторная попытка) " : ""}до ${new Date(pl.pause_until * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
    else text = pl.running.length ? `Работает · задач: ${pl.running.length}` : "Включён · ждёт задач";
  }
  $("#pstate").textContent = text;
  renderUpdate();
}

function render() {
  renderHeader();
  renderWelcome();
  $("#chat").classList.toggle("hidden", !S.chatOpen || !S.projectId || S.view === "onboarding");
  if (S.view === "board") renderBoard();
  else if (S.view === "questions") renderQuestions();
  else if (S.view === "onboarding") renderOnboarding();
  else renderSettings();
}

// ---------------- доска ----------------
function renderBoard() {
  const v = $("#view");
  if (!S.state.projects.length) {
    v.innerHTML = `<div class="empty-board"><h2>Проектов пока нет</h2><p>Проект — это папка, в которой ${S.state.agent_name} будет работать над задачами.</p>
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
      <div class="telegram-device">${telegramDeviceControl()}</div>
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
      ${t.model ? `<span>${esc(t.model)}</span>` : ""}
      ${t.links ? `<span class="links-n" title="Связанных задач: ${t.links}">🔗${t.links}</span>` : ""}</div>
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
      <div class="row"><div class="grow"><label>Модель</label><select id="nt-model" onchange="App.taskModelChanged('nt')">${opts(MODELS)}</select></div>
        <div class="grow"><label>Effort</label><select id="nt-effort">${opts(effortList())}</select></div></div>
    </div>
    ${telegramTaskChoice("nt-telegram", telegramDefault())}
    <div class="actions"><button onclick="App.closeModal()">Отмена</button><button class="primary" onclick="App.createTask()">Создать</button></div>`);
  setTimeout(() => $("#nt-title").focus(), 0);
}

async function createTask() {
  const title = $("#nt-title").value.trim();
  if (!title) return toast("Нужен заголовок");
  await api("POST", `/api/projects/${S.projectId}/tasks`, {
    title, description: $("#nt-desc").value, column_id: +$("#nt-col").value, model: $("#nt-model").value, effort: $("#nt-effort").value,
    telegram_notify: $("#nt-telegram").checked,
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
    <label>Описание</label><textarea id="tt-desc" rows="6" oninput="App.renderRefs()">${esc(t.description || "")}</textarea>
    <div class="refs muted" id="tt-refs"></div>
    ${telegramTaskChoice("tt-telegram", !!t.telegram_notify)}
    <div class="row" style="align-items:flex-end">
      <div class="grow"><label>Модель</label><select id="tt-model" onchange="App.taskModelChanged('tt')">${opts(MODELS, t.model)}</select></div>
      <div class="grow"><label>Effort</label><select id="tt-effort">${opts(effortList(t.model), t.effort)}</select></div>
      <button onclick="App.saveTask()">Сохранить</button>
    </div>
    <div id="tt-dyn"></div>
    <label>Связанные задачи <span class="muted">— агент увидит их итоги и пути к отчётам</span></label>
    <div class="links" id="tt-links"></div>
    <div class="link-add"><input id="tt-link" placeholder="Связать с задачей: номер или часть заголовка" autocomplete="off"
      oninput="App.linkSearch()" onblur="$('#tt-link-res').classList.add('hidden')" onkeydown="if(event.key==='Enter'){event.preventDefault();App.linkPick(0)}"><div class="link-res hidden" id="tt-link-res"></div></div>
    <label>Ход работы</label>
    <div class="timeline" id="tt-tl">${t.events.map(evHtml).join("") || '<div class="muted">Пока пусто</div>'}</div>
    <div class="row" style="margin-top:8px"><input id="tt-comment" placeholder="Комментарий к задаче (агент увидит при следующем запуске)" onkeydown="if(event.key==='Enter')App.comment()"><button onclick="App.comment()">Добавить</button></div>
    <div class="actions" id="tt-actions"></div>`, "wide");
  renderTaskDyn(); renderRefs();
  const tl = $("#tt-tl"); tl.scrollTop = tl.scrollHeight;
}

// номера задач, упомянутые в описании: textarea ссылки не покажет, выводим их строкой под ним
function renderRefs() {
  const el = $("#tt-refs");
  if (!el) return;
  const ids = [...new Set([...($("#tt-desc").value.matchAll(/(?:^|[\s(«,;:])#(\d+)\b/g))].map(m => m[1]))];
  el.innerHTML = ids.length ? "Упомянуты: " + refs(ids.map(i => "#" + i).join(" ")) : "";
}

function renderLinks() {
  const t = S.task, el = $("#tt-links");
  if (!t || !el) return;
  el.innerHTML = t.links.map(l => `<div class="link-item">
      <a href="#" onclick="event.preventDefault(); App.openTask(${l.id})"><span class="muted">#${l.id}</span> ${esc(l.title)}</a>
      <span class="muted">${esc(l.column_name)}${l.project_id !== t.project_id ? ` · ${esc(l.project_name)}` : ""}</span>${pill(l.status)}
      <span class="grow"></span><button class="ghost" title="Убрать связь" onclick="App.unlinkTask(${l.id})">✕</button>
    </div>`).join("") || '<div class="muted">Нет</div>';
}

const linkSearch = debounce(async () => {
  const inp = $("#tt-link"), box = $("#tt-link-res"), t = S.task;
  if (!inp || !t) return;
  const q = inp.value.trim(), ok = fresh("linksearch");
  if (!q) { S.linkRes = []; return box.classList.add("hidden"); }
  const skip = [t.id, ...t.links.map(l => l.id)].join(",");
  const res = await api("GET", `/api/tasks?q=${encodeURIComponent(q)}&exclude=${skip}`).catch(() => []);
  if (!ok()) return;
  S.linkRes = res;
  box.innerHTML = res.map((r, i) => `<div class="link-opt" onmousedown="event.preventDefault(); App.linkPick(${i})">
      <span class="muted">#${r.id}</span> ${esc(r.title)} <span class="muted">· ${esc(r.column_name)}${r.project_id !== t.project_id ? ` · ${esc(r.project_name)}` : ""}</span></div>`).join("")
    || '<div class="muted" style="padding:6px 10px">Ничего не нашлось</div>';
  box.classList.remove("hidden");
}, 200);

async function linkPick(i) {
  const r = S.linkRes?.[i], t = S.task;
  if (!r || !t) return;
  await api("POST", `/api/tasks/${t.id}/links`, { task_id: r.id });
  $("#tt-link").value = ""; S.linkRes = []; $("#tt-link-res").classList.add("hidden");
  refreshTask();
}

async function unlinkTask(id) { await api("DELETE", `/api/tasks/${S.task.id}/links/${id}`); refreshTask(); }

function renderTaskDyn() {
  const t = S.task;
  if (!t || !$("#tt-dyn")) return;
  $("#tt-line").innerHTML = `${t.project_id !== S.projectId ? `${esc(t.project_name)} · ` : ""}${esc(t.column_name)} ${pill(t.status)} ${t.cost_usd ? `· ≈$${t.cost_usd.toFixed(2)} (эквивалент API)` : ""}
    ${t.session_id ? `· сессия <code title="${S.state.agent === 'codex' ? 'codex resume' : 'claude --resume'} ${t.session_id}">${t.session_id.slice(0, 8)}</code>` : ""}`;
  const open = t.questions.filter(q => q.status === "open");
  let html = "";
  if (t.status === "error" && t.error) html += `<div class="callout err">${esc(t.error)}</div>`;
  if (t.summary) html += `<div class="callout ok"><b>Итог:</b> ${esc(t.summary)} ${t.report_path ? ` · <a href="/report/${t.id}" target="_blank">подробный отчёт →</a>` : ""}</div>`;
  const d = t.telegram_delivery;
  if (d) {
    const names = {pending: "в очереди", sending: "отправляется", sent: "отправлено в Избранное", error: "ошибка доставки", uncertain: "нужно проверить Избранное"};
    html += `<div class="callout ${["error", "uncertain"].includes(d.state) ? "err" : ""}"><b>Telegram:</b> ${names[d.state] || esc(d.state)}
      ${!t.telegram_notify && d.state === "pending" ? " · отправка выключена в задаче" : ""}
      ${d.error ? `<div>${esc(d.error)}</div>` : ""}
      ${d.state === "error" ? `<button onclick="App.retryTelegram()">Повторить доставку</button>` : ""}
      ${d.state === "uncertain" ? `<div class="hint">Проверь часть ${d.next_part + 1} из ${d.parts} с меткой <code>${esc(d.marker || "AP")}</code> в Избранном. Подтверждённые ранее части повторно не отправляются.</div>
        <button onclick="App.retryTelegram('received')">Последняя часть получена</button>
        <button onclick="App.retryTelegram('resend')">Её нет — повторить</button>` : ""}</div>`;
  }
  if (open.length) html += `<label>Ждёт ответа</label>` + open.map(q => questionHtml(q, false)).join("");
  $("#tt-dyn").innerHTML = html;
  renderLinks();
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
  const body = e.kind === "text" ? `<div class="body md">${refs(md(e.text))}</div>` : `<span class="body">${refs(esc(e.text))}</span>`;
  return `<div class="ev ${e.kind}"><span class="t">${fmtTime(e.ts)}</span>${EV_NAMES[e.kind] ? `<span class="k">${EV_NAMES[e.kind]}</span>` : ""}${body}</div>`;
}

async function saveTask() {
  const t = S.task;
  await api("PATCH", `/api/tasks/${t.id}`, { title: $("#tt-title").value, description: $("#tt-desc").value, model: $("#tt-model").value, effort: $("#tt-effort").value });
  toast("Сохранено"); loadBoard(); await refreshTask();
  if ($("#tt-telegram")) $("#tt-telegram").checked = !!S.task.telegram_notify;
}

async function setTaskTelegram(value) {
  const t = S.task;
  try {
    await api("PATCH", `/api/tasks/${t.id}`, {telegram_notify: value});
    await refreshTask();
    if (value && S.state?.telegram?.auth !== "ready") toast("Флаг сохранён. Подключи Telegram в настройках.");
  } catch {
    if ($("#tt-telegram")) $("#tt-telegram").checked = !!t.telegram_notify;
  }
}

async function retryTelegram(resolution) {
  if (resolution === "resend" && !confirm("Последняя часть точно отсутствует в Избранном? Повтор может создать дубль, если Telegram уже её принял.")) return;
  await api("POST", `/api/tasks/${S.task.id}/telegram/retry`, {resolution});
  await refreshTask();
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
  if ($("#tt-telegram")) $("#tt-telegram").checked = !!t.telegram_notify;
}

async function comment() {
  const inp = $("#tt-comment"), text = inp.value.trim();
  if (!text) return;
  await api("POST", `/api/tasks/${S.task.id}/comment`, { text });
  inp.value = "";
}

async function toTodo(id) {
  // карточка могла открыться по ссылке из другого проекта — колонки берём с её доски
  const pid = S.task?.id === id ? S.task.project_id : S.projectId;
  const b = pid === S.board.project.id ? S.board : await api("GET", `/api/projects/${pid}/board`);
  const col = b.columns.find(c => c.kind === "todo");
  await api("POST", `/api/tasks/${id}/move`, { column_id: col.id, order: [...b.tasks.filter(t => t.column_id === col.id).map(t => t.id), id] });
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

// ---------------- первое знакомство ----------------
let onboardingStep = 0, onboardingActive = false;

function renderWelcome() {
  const box = $("#welcome"), pending = S.state?.settings.onboarding_status === "pending";
  box.classList.toggle("hidden", !pending || onboardingActive || S.view === "onboarding");
  box.innerHTML = pending ? `<span><b>Первый запуск?</b> Три коротких шага: работа с задачами, Git и Telegram. Интеграции необязательны.</span>
    <div><button onclick="App.openOnboarding()">Открыть мастер</button><button class="ghost" onclick="App.finishOnboarding('skipped')">Пропустить мастер</button></div>` : "";
}

async function openOnboarding(resume = false) {
  await loadState();
  if (!resume) onboardingStep = 0;
  onboardingActive = true;
  setView("onboarding");
}

function onboardingMove(step) {
  onboardingStep = Math.max(0, Math.min(2, step));
  renderOnboarding();
}

async function finishOnboarding(status) {
  await api("POST", "/api/settings", {onboarding_status: status});
  onboardingActive = false;
  await loadState();
  setView("board");
  toast(status === "completed" ? "Знакомство завершено. Мастер доступен в настройках." : "Мастер пропущен. Вернуться можно из настроек.");
}

function renderOnboarding() {
  const p = project(), s = S.state.settings, step = onboardingStep;
  const titles = ["От задачи до результата", "Сохраняй изменения в Git", "Получай результаты в Telegram"];
  const codex = S.state.agent === "codex";
  const intro = `<p>Проект — папка на ПК с трекером. Создай задачу с ожидаемым результатом, перенеси в «В работу» и включи пайплайн в шапке.</p>
    <p>Вопросы агента — во «Вопросах»: ответь, чтобы продолжить. Итог и отчёт — в карточке «Готово».</p>
    <div class="onboarding-status"><b>Исполнитель: ${esc(S.state.agent_name)}</b><br>${codex
      ? (S.state.agent_info?.version ? `Codex CLI ${esc(S.state.agent_info.version)}: вход ChatGPT проверен при запуске сервера.` : "Вход ChatGPT на этом экране не проверялся.")
      : "Вход Claude Code на этом экране не проверялся."}
      <p class="hint">${codex ? "Codex CLI ≥ 0.166.0: codex login → ChatGPT." : "Claude Code: вход по подписке через claude."} Вход — на ПК с трекером; API-ключ не нужен. Исполнителя выбирает файл запуска.</p>
      ${p ? `Выбран проект «${esc(p.name)}»: <span class="onboarding-path">${esc(p.path)}</span>` : "Проект ещё не создан."}</div>
    <button onclick="App.newProject()">${p ? "Добавить проект" : "Создать первый проект"}</button>`;
  const git = `<p>Коммит сохраняет изменения задачи в истории проекта. Push отправляет коммиты в удалённый репозиторий, например на GitHub.</p>
    <label for="onboarding-git-mode">Общий режим для проектов</label>
    <select id="onboarding-git-mode" onchange="App.setSetting('git_mode', this.value)">${opts([
      ["off", "Без Git"], ["commit", "Только коммит"], ["commit_push", "Коммит и push"]], s.git_mode)}</select>
    <div class="onboarding-status">${gitReadinessPanel(p, s.git_mode, true)}</div>
    <p class="hint">GitHub CLI необязателен. Существующий SSH/HTTPS-доступ сохраняется. Проверка не делает push и не подтверждает право записи.</p>
    <button onclick="App.openGitSettings()">Настройка и проверка Git</button>`;
  const telegram = `<p>Итог и материалы задачи придут в «Избранное» твоего аккаунта. Для этого на ПК с трекером нужны дополнительные Playwright и Chromium, вход по QR и тестовое сообщение.</p>
    <div class="onboarding-status" id="onboarding-telegram" role="status">Читаю состояние подключения…</div>
    <p>Переключатель «Результаты в Telegram» запоминается на устройстве: на телефоне по умолчанию включён, на ПК выключен. Он задаёт начальный флаг новым задачам; в каждой карточке флаг можно изменить отдельно.</p>
    <p class="hint">Telegram необязателен. Установка компонентов, вход, проверка сессии и тест в «Избранное» доступны в общих настройках.</p>
    <button onclick="App.openTelegramSettings()">Настроить Telegram</button>`;
  $("#view").innerHTML = `<section class="onboarding">
    <div class="onboarding-head"><span>Первое знакомство · ${step + 1} из 3</span><button class="ghost" onclick="App.finishOnboarding('skipped')">Пропустить мастер</button></div>
    <ol class="onboarding-steps" aria-label="Шаги мастера">${["Трекер", "Git", "Telegram"].map((t, i) => `<li><button ${i === step ? 'aria-current="step"' : ""} onclick="App.onboardingMove(${i})">${i + 1}. ${t}</button></li>`).join("")}</ol>
    <h2 id="onboarding-title" tabindex="-1">${titles[step]}</h2>
    <div class="onboarding-body">${[intro, git, telegram][step]}</div>
    <div class="onboarding-actions">${step ? `<button onclick="App.onboardingMove(${step - 1})">Назад</button>` : ""}
      <button class="ghost" onclick="${step < 2 ? `App.onboardingMove(${step + 1})` : "App.finishOnboarding('skipped')"}">Пропустить шаг</button>
      <button class="primary" onclick="${step < 2 ? `App.onboardingMove(${step + 1})` : "App.finishOnboarding('completed')"}">${step < 2 ? "Далее" : "Завершить"}</button></div>
    <p class="hint">Можно работать сразу, а к настройкам вернуться позже.</p>
  </section>`;
  $("#view").scrollTop = 0;
  $("#onboarding-title").focus({preventScroll: true});
  if (step === 1) checkGitReadiness();
  if (step === 2) refreshOnboardingTelegram();
}

async function refreshOnboardingTelegram() {
  const box = $("#onboarding-telegram");
  if (!box) return;
  try {
    const t = await api("GET", "/api/telegram");
    if (box !== $("#onboarding-telegram")) return;
    const names = {ready: "Авторизация проверена сервисом", unconfigured: "Не подключён", unknown: "Нужна проверка сессии",
      checking: "Проверяем сессию…", login: "Ожидаем вход…", auth_required: "Нужно войти заново", error: "Нужна проверка подключения", uncertain: "Проверь тестовое сообщение в Избранном"};
    box.textContent = `${names[t.auth] || "Состояние неизвестно"}. ${t.test_at ? `Последний успешный тест: ${fmtTime(t.test_at)}.` : "Успешного теста ещё не было."}`;
  } catch {
    if (box === $("#onboarding-telegram")) box.textContent = "Не удалось получить состояние. Открой настройки и повтори проверку сессии.";
  }
}

// ---------------- настройки ----------------
function renderSettings() {
  const s = S.state.settings, p = project();
  $("#view").innerHTML = `<div class="pad">
    <h2>Настройки · ${esc(S.state.agent_name)}</h2>
    <div class="section" id="onboarding-settings"><h3>Первое знакомство</h3>
      <p class="hint">Работа с задачами и необязательные интеграции. Просмотр шагов не подключает сервисы.</p>
      <button onclick="App.openOnboarding(${onboardingActive})">${onboardingActive ? "Вернуться к мастеру" : "Открыть мастер"}</button></div>
    <div class="section" id="telegram-settings"></div>
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
        ["bypassPermissions", "Без вопросов — полный доступ"],
        ["acceptEdits", "Правки проекта сами, остальное — через запрос разрешения"],
        ["default", "Чтение свободно, изменения — через запрос разрешения"]], s.permission_mode)}</select>
      <div class="hint">Запросы разрешений приходят на страницу «Вопросы» как тикеты «Разрешить / Запретить».</div>
      <div class="row">
        <div class="grow"><label>Модель для задач</label><select onchange="App.setSetting('model', this.value)">${opts(MODELS, s.model)}</select></div>
        <div class="grow"><label>Effort для задач</label><select onchange="App.setSetting('effort', this.value)">${opts(effortList(s.model, true), s.effort)}</select></div>
        <div class="grow"><label>Модель для чата и импорта</label><select onchange="App.setSetting('chat_model', this.value)">${opts(MODELS, s.chat_model)}</select></div>
      </div>
      <div id="git-settings"><label>Git в конце задачи (если папка проекта — git-репозиторий)</label>
      <select onchange="App.setSetting('git_mode', this.value)">${opts([
        ["commit_push", "Коммит и push — по коммиту на задачу, push без force"],
        ["commit", "Только коммит — push делаю сам"],
        ["off", "Ничего — агент git не трогает"]], s.git_mode)}</select>
      <div class="hint">Общий режим для всех проектов. Агент коммитит только изменения своей задачи с сообщением «#id заголовок». Доступ к репозиторию трекера и к репозиториям проектов настраивается отдельно. В папке трекера неотправленные коммиты блокируют его обновление.</div>
      ${gitReadinessPanel(p, s.git_mode)}</div>
      <label>Дополнительные указания всем агентам задач</label>
      <textarea rows="4" placeholder="Например: «Код Unity проверяй через UnityMCP»" onchange="App.setSetting('extra_prompt', this.value)">${esc(s.extra_prompt)}</textarea>
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
  renderTelegramSettings();
  if (s.git_mode === "commit_push" && p) checkGitReadiness();
}

let telegramPoll = null;
function renderTelegramSettings() {
  const box = $("#telegram-settings");
  if (!box) return;
  const t = S.state.telegram || {auth: "unconfigured"};
  const names = {unconfigured: "Не подключён", unknown: "Нужна проверка сессии", checking: "Проверяем сессию…", login: "Ожидаем вход…",
    ready: "Авторизация проверена · получатель: Избранное", auth_required: "Нужно войти заново", error: "Подключение требует внимания", uncertain: "Проверь тестовое сообщение в Избранном"};
  box.innerHTML = `<h3>Telegram → Избранное</h3>
    <div class="telegram-device">${telegramDeviceControl()}</div>
    <p class="hint">Выбор сохраняется на этом устройстве и задаёт начальный флаг только новым задачам, в том числе из чата и импорта. На телефоне по умолчанию включён, на ПК выключен. В карточке задачи свой флаг.</p>
    <ol class="telegram-steps">
      <li><a href="/api/telegram/instructions" target="_blank">Инструкция по установке и входу</a>. На ПК с трекером: <code>python -m pip install playwright</code>, затем <code>python -m playwright install chromium</code>.</li>
      <li>Войди в свой Telegram по QR ниже или на ПК командой <code>python tools/telegram_web.py login --show</code>. Для пароля 2FA используй окно на ПК.</li>
      <li>Проверь сессию и отправь тест в своё «Избранное».</li>
    </ol>
    <p id="telegram-status"><b>${esc(names[t.auth] || "Нужна настройка")}</b>${t.error ? `<br>${esc(t.error)}` : ""}</p>
    ${t.test_at ? `<p class="hint">Последний успешный тест в Избранное: ${fmtTime(t.test_at)}</p>` : ""}
    <div class="telegram-actions">
      <button ${t.busy ? "disabled" : ""} onclick="App.telegramAction('login')">${t.auth === "auth_required" ? "Войти заново" : "Войти по QR"}</button>
      <button ${t.busy ? "disabled" : ""} onclick="App.telegramAction('check')">Проверить сессию</button>
      <button ${t.busy || t.auth !== "ready" ? "disabled" : ""} onclick="App.telegramAction('test')">Тест в Избранное</button>
      ${t.operation === "login" ? '<button onclick="App.telegramAction(\'cancel\')">Отменить вход</button>' : ""}
    </div>
    ${t.operation === "login" ? `<p class="hint">Telegram на телефоне → Настройки → Устройства → Подключить устройство. Если трекер открыт на том же телефоне, открой эту страницу на ПК, чтобы отсканировать QR.</p>
      ${t.qr ? `<img class="telegram-qr" src="/api/telegram/qr?t=${Date.now()}" alt="QR для входа в Telegram">` : '<p class="muted">Ждём QR от Telegram Web…</p>'}` : ""}
    <p class="hint">Профиль и материалы хранятся только на ПК с трекером в data/telegram/. Вкладку на телефоне можно закрыть; сервер трекера должен работать.</p>`;
  clearTimeout(telegramPoll);
  if (S.view === "settings" && t.busy) telegramPoll = setTimeout(refreshTelegram, 1500);
}

async function refreshTelegram() {
  if (S.view !== "settings") return;
  const ok = fresh("telegram");
  const state = await api("GET", "/api/telegram");
  if (!ok() || S.view !== "settings") return;
  S.state.telegram = state;
  renderTelegramSettings();
}

async function telegramAction(operation) {
  S.state.telegram = await api("POST", `/api/telegram/${operation}`, {});
  renderTelegramSettings();
}

function openTelegramSettings() {
  if (S.chatOpen && matchMedia("(max-width: 900px)").matches) toggleChat();
  setView("settings");
  $("#telegram-settings")?.scrollIntoView({block: "start"});
  refreshTelegram();
}

function gitReadinessPanel(p, mode, compact = false) {
  if (mode === "off") return `<p class="hint">Git выключен. Для обычной работы трекера авторизация GitHub не нужна.</p>`;
  if (!p) return `<p class="hint">Добавь и выбери проект, чтобы проверить его Git-репозиторий.</p>`;
  const auth = ls.get(`gitAuth.${p.id}`, "existing");
  if (compact) return `<p>Проект «${esc(p.name)}»</p><button id="git-check" onclick="App.checkGitReadiness()">Проверить ещё раз</button>
    <div id="git-result" data-compact="true" role="status" aria-live="polite">Проверка ещё не выполнена.</div>`;
  return `<div class="git-readiness">
    <p><b>Проверка проекта «${esc(p.name)}»</b></p>
    ${mode === "commit_push" ? `<label for="git-auth">Способ доступа для проверки</label>
      <select id="git-auth" onchange="App.gitAuthChanged(this.value)">${opts([
        ["existing", "Существующий SSH / HTTPS / менеджер учётных данных"],
        ["gh", "GitHub CLI (gh) для HTTPS"]], auth)}</select>` : `<p class="hint">Локальным коммитам не нужны remote и вход в GitHub.</p>`}
    <p><button id="git-check" onclick="App.checkGitReadiness()">Проверить готовность</button></p>
    <div id="git-result" role="status" aria-live="polite"></div>
    <p class="hint">Проверка не создаёт коммиты, не делает push и не меняет Git-конфигурацию.
      ${mode === "commit_push" ? "Локальные настройки и вход gh не гарантируют право записи: доступ, защита ветки и ошибки сети выяснятся при реальном push. Его отказ агент сохранит в отчёте без force/reset/rebase." : ""}
      Инструкция: README → «Git и GitHub: настройка проектов».</p>
  </div>`;
}

async function checkGitReadiness() {
  const box = $("#git-result"), button = $("#git-check"), pid = S.projectId, mode = S.state.settings.git_mode;
  if (!box || !pid || mode === "off") return;
  const ok = fresh("git-readiness"), auth = $("#git-auth")?.value || ls.get(`gitAuth.${pid}`, "existing");
  button.disabled = true;
  box.textContent = "Проверяю…";
  try {
    const r = await api("GET", `/api/projects/${pid}/git-readiness?mode=${encodeURIComponent(mode)}&auth=${encodeURIComponent(auth)}`);
    if (!ok() || box !== $("#git-result") || pid !== S.projectId) return;
    const title = {needs_setup: "Нужна настройка", review: "Есть пункты для проверки", local_ready: "Локальные предпосылки настроены"}[r.status];
    box.innerHTML = `<b>${esc(title || "Состояние неизвестно")}</b>` + (box.dataset.compact ? `<p class="hint">${esc(r.checks.find(c => c.status !== "ok")?.message || "Проверка локальных условий завершена.")}</p>`
      : `<ul>${r.checks.map(c => `<li class="git-${esc(c.status)}"><b>${{ok: "✓", warning: "Проверь:", error: "Нужно:"}[c.status]}</b> ${esc(c.message)}</li>`).join("")}</ul>`);
  } catch (e) {
    if (ok() && box === $("#git-result")) box.textContent = "Не удалось проверить Git. Повтори проверку после восстановления связи с трекером.";
  } finally {
    if (ok()) button.disabled = false;
  }
}

function gitAuthChanged(value) {
  ls.set(`gitAuth.${S.projectId}`, value);
  checkGitReadiness();
}

async function openGitSettings(pid = S.projectId) {
  if (pid && pid !== S.projectId) await selectProject(pid);
  if (S.chatOpen && matchMedia("(max-width: 900px)").matches) toggleChat();
  setView("settings");
  $("#git-settings")?.scrollIntoView({block: "start"});
}

async function setSetting(k, v) {
  const changes = { [k]: v };
  if (k === "model" && S.state.agent === "codex" && !effortList(v, true).some(([e]) => e === S.state.settings.effort)) changes.effort = "";
  await api("POST", "/api/settings", changes); await loadState();
  if (S.view === "settings" && (k === "model" || k === "git_mode")) renderSettings();
  if (S.view === "onboarding" && k === "git_mode") renderOnboarding();
  toast("Сохранено");
}
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
    <label>Папка проекта — здесь ${S.state.agent_name} будет работать</label>
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
  await loadState(); if (S.view !== "onboarding") S.view = "board"; render(); loadBoard(); loadChat();
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
  if (!["board", "onboarding"].includes(S.view)) { S.view = "board"; ls.set("view", "board"); }
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
    <p class="muted">Вставь текст из другого трекера, письма или заметок — одну задачу или сразу пачку. ${S.state.agent_name} разберёт его на карточки, ты проверишь перед созданием.</p>
    <textarea id="im-text" rows="12" placeholder="Вставь сюда…"></textarea>
    ${telegramTaskChoice("im-telegram", telegramDefault())}
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
      <div><input class="im-title" value="${esc(t.title)}" style="font-weight:600;margin-bottom:6px"><textarea class="im-desc" rows="3">${esc(t.description)}</textarea>
      <label class="telegram-choice"><input class="im-notify" type="checkbox" ${t.telegram_notify || $("#im-telegram").checked ? "checked" : ""}> Результат в Telegram</label></div></div>`).join("");
  $("#im-actions").innerHTML = `<select id="im-col" style="width:auto">${cols.map(c => `<option value="${c.id}" ${c.kind === "backlog" ? "selected" : ""}>в «${esc(c.name)}»</option>`).join("")}</select>
    <button onclick="App.closeModal()">Отмена</button><button id="im-go" onclick="App.importParse()">Разобрать заново</button>
    <button class="primary" onclick="App.importCommit()">Создать задачи</button>`;
}

async function importCommit() {
  const tasks = [...document.querySelectorAll(".import-item")].filter(el => el.querySelector("input[type=checkbox]").checked)
    .map(el => ({ title: el.querySelector(".im-title").value, description: el.querySelector(".im-desc").value, telegram_notify: el.querySelector(".im-notify").checked }));
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
      <small>${c.running ? "отвечает · " : ""}${c.titling ? "придумываю название · " : ""}${fmtTime(c.updated_at)} · сообщений: ${c.messages}</small></div>
      <button class="ghost" onclick="event.stopPropagation(); App.retitleChat(${c.id})" title="Придумать название заново" ${c.titling ? "disabled" : ""}>✨</button>
      <button class="ghost" onclick="event.stopPropagation(); App.renameChat(${c.id})" title="Переименовать">✎</button>
      <button class="ghost" onclick="event.stopPropagation(); App.deleteChat(${c.id})" title="Удалить чат">🗑</button></div>`).join("") ||
    `<div class="muted" style="padding:12px">Чатов пока нет — просто напиши сообщение.</div>`;
}

function renderChat(toBottom) {
  renderChatHead();
  const box = $("#msgs");
  const atBottom = toBottom || box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = S.chat.messages.map(msgHtml).join("") ||
    `<div class="muted" style="padding:20px;text-align:center">Это чат с ${S.state.agent_name} внутри трекера. Он видит доску и может создавать и двигать задачи, отвечать про статус, включать пайплайн. Можно вставить сюда текст задач — он их заведёт.</div>`;
  $("#typing").classList.toggle("hidden", !S.chat.running);
  $("#chatStop").classList.toggle("hidden", !S.chat.running);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

const msgHtml = m => {
  if (m.role === "assistant") return `<div class="msg assistant md">${refs(md(m.text))}</div>`;
  const atts = m.attachments?.length ? `<div class="msg-atts">${m.attachments.map((a, i) => attHtml(m.id, a, i)).join("")}</div>` : "";
  return `<div class="msg ${m.role}">${m.text ? refs(esc(m.text)) : ""}${atts}</div>`;
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
    await api("POST", `/api/chats/${S.chat.id}/messages`, { text, attachments, telegram_notify: telegramDefault() });
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

async function retitleChat(id) {
  await api("POST", `/api/chats/${id}/title`);
  toast("Придумываю название…"); loadChats();
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
  const bc = window.BroadcastChannel && navigator.locks ? new BroadcastChannel("agentpipeline-events") : null;
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
  navigator.locks.request("agentpipeline-events", () => { listen(); return new Promise(() => { }); });
}

// подстраховка: видимая вкладка сверяется с сервером раз в 20 с и сразу, когда на неё вернулись
setInterval(() => { if (document.visibilityState === "visible") resync(); }, 20000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") resyncD(); });
window.addEventListener("focus", resyncD);

// пока открыт другой вид, события доску не перечитывают — при возврате берём свежую
function setView(v) { S.view = v; ls.set("view", v === "onboarding" ? "board" : v); render(); if (v === "board") loadBoard(); }
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
  openOnboarding, onboardingMove, finishOnboarding,
  setTelegramDefault, openTelegramSettings, telegramAction, setTaskTelegram, retryTelegram,
  taskModelChanged, setView, selectProject, newProject, createProject, pickFolder, updateProject, setProjectActive, deleteProject,
  newTask, createTask, openTask, saveTask, comment, toTodo, stopTask, resetTask, deleteTask, closeModal,
  renderRefs, linkSearch, linkPick, unlinkTask,
  answer, answerText, toggleAnswered, setSetting, clearPause, restartTracker, addColumn, columnMenu,
  checkUpdate, applyUpdate, toggleUpdList, hideUpdate,
  checkGitReadiness, gitAuthChanged, openGitSettings,
  importDialog, importParse, importCommit, toggleChat, chatSend, chatStop, chatNew, toggleChatList, openChat, renameChat, retitleChat, deleteChat, removeAtt, togglePipeline,
};

(async () => {
  await loadState();
  render();
  if (new URLSearchParams(location.search).get("setup") === "telegram") openTelegramSettings();
  connect();  // подписываемся до загрузки доски и чата, чтобы не потерять события, пришедшие во время неё
  await loadBoard();
  if (S.chatOpen) loadChat();
})();
