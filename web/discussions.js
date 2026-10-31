// Черновик принадлежит установке сервера и конкретному диалогу. File сохраняется в IndexedDB.
const D = { key: null, value: {}, memory: new Map(), db: null, warning: false };
const draftEmpty = () => ({ text: '', atts: [], context: null, request_id: null, pending: false });
const draftKey = (pid, cid) => `${S.state.installation_id}/${pid}/${S.state.projects.find(p => p.id === pid)?.created_at || 'legacy'}/${cid || 'new'}`;

function draftDB() {
  if (!D.db) D.db = new Promise(resolve => {
    const req = indexedDB.open('agentpipeline-drafts', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('drafts');
    req.onsuccess = () => resolve(req.result);
    req.onerror = req.onblocked = () => resolve(null);
  }).catch(() => null);
  return D.db;
}

async function draftStore(key, value) {
  try {
    const db = await draftDB();
    if (!db) throw Error();
    await new Promise((resolve, reject) => {
      const tx = db.transaction('drafts', 'readwrite');
      tx.objectStore('drafts').put(value, key);
      tx.oncomplete = resolve; tx.onerror = tx.onabort = reject;
    });
  } catch {
    if (!D.warning) toast('Черновик сохранён только в памяти и текстовом резерве. Файлы могут не восстановиться после закрытия.');
    D.warning = true;
  }
}

function draftSave() {
  if (!D.key) return;
  D.value = { ...D.value, text: $('#chatInput').value, atts: S.atts.map(a => ({ file: a.file, name: a.name })) };
  D.memory.set(D.key, D.value);
  ls.set('draft.' + D.key, { ...D.value, atts: [], filesMissing: D.value.filesMissing || D.value.atts.length });
  draftStore(D.key, D.value);
}

async function draftSwitch(pid, cid, ok, adopt = false) {
  const key = draftKey(pid, cid);
  if (D.key === key) return true;
  draftSave();
  let value = D.memory.get(key);
  if (!value) {
    const db = await draftDB();
    value = db && await new Promise(resolve => {
      const tx = db.transaction('drafts');
      const req = tx.objectStore('drafts').get(key);
      req.onsuccess = () => resolve(req.result); req.onerror = () => resolve(null);
    });
    value ||= ls.get('draft.' + key, null);
  }
  if (!ok()) return false;
  draftSave(); // Во время чтения пользователь мог продолжить набор.
  if (adopt && D.key === draftKey(pid, null)) {
    value = D.value;
    D.memory.set(D.key, draftEmpty()); ls.set('draft.' + D.key, draftEmpty()); draftStore(D.key, draftEmpty());
  }
  S.atts.forEach(a => a.preview && URL.revokeObjectURL(a.preview));
  D.key = key; D.value = { ...draftEmpty(), ...value };
  $('#chatInput').value = D.value.text;
  S.atts = D.value.atts.map(a => ({ ...a, preview: isImage(a.file.type) ? URL.createObjectURL(a.file) : null }));
  renderAtts(); renderDraftContext();
  return true;
}

function contextHtml(c, removable = false) {
  if (!c) return '';
  return `<div class="task-context"><div class="grow"><b>${esc(c.project_name)} #${c.task_number}</b><span>${esc(c.title)}</span>
    <small>Результат от ${fmtTime(c.created_at)} · №${c.result_id}${c.task_available === false ? ' · карточка удалена' : ''}</small>
    <div class="context-links">${c.task_available !== false ? `<button class="ghost" onclick="App.openContextTask(${c.task_id},${c.project_id},${c.task_number})">Карточка</button>` : ''}
    <button class="ghost" onclick="App.openResult(${c.result_id})">Результат и отчёт</button></div></div>
    ${removable ? '<button class="ghost context-remove" onclick="App.removeContext()" aria-label="Убрать прикреплённую задачу">✕</button>' : ''}</div>`;
}

async function openContextTask(id, pid, number) {
  const t = await api('GET', `/api/tasks/${id}`);
  if (t.project_id !== pid || t.task_number !== number) return toast('Исходная карточка удалена. Исторический результат сохранён.');
  await openTask(id);
}

function renderDraftContext() {
  const c = D.value.context, pending = D.value.pending, locked = pending || S.chatSending;
  $('#chatContext').classList.toggle('hidden', !c);
  $('#chatContext').innerHTML = contextHtml(c, true);
  $('#draftNotice').innerHTML = pending ? 'Отправка не подтверждена. Нажмите ↑ для безопасного повтора; текст и файлы сохранены.' :
    D.value.filesMissing ? 'Файлы черновика не восстановились. Прикрепите их заново или <button class="ghost" onclick="App.dismissLostFiles()">Продолжить без файлов</button>.' : '';
  $('#chatInput').disabled = !!locked;
  $('#chatFile').disabled = !!locked;
  document.querySelectorAll('#chatAtts button, #chatContext .context-remove, .clip').forEach(b => b.disabled = !!locked);
}

function removeContext() {
  if (D.value.pending || S.chatSending) return;
  D.value.context = null; draftSave(); renderDraftContext();
}

async function discussResult(id) {
  if (D.discussing) return;
  D.discussing = true;
  try { await attachResultToDraft(id); }
  finally { D.discussing = false; }
}

async function attachResultToDraft(id) {
  if (S.chatSending) return toast('Дождитесь завершения отправки.');
  const pid = S.projectId, cid = S.chat.pid === pid ? S.chat.id : null;
  const r = await api('GET', `/api/results/${id}`);
  if (S.projectId !== pid || r.project_id !== pid) return toast('Проект изменился. Выберите результат в нужном проекте заново.');
  await loadChat();
  if (cid && cid !== S.chat.id) return toast('Диалог изменился. Нажмите «Обсудить» ещё раз для выбранного чата.');
  if (!S.chat.id) await openChat((await api('POST', `/api/projects/${r.project_id}/chats`)).id, true);
  if (S.projectId !== r.project_id || S.chat.pid !== r.project_id) return;
  if (D.value.pending) return toast('Сначала подтвердите отправку черновика повтором.');
  if (D.value.context && D.value.context.result_id !== id && !confirm('В черновике уже прикреплён другой результат. Заменить прикрепление? Текст и файлы сохранятся.')) return;
  D.value.context = { task_id: r.source_task_id, result_id: r.id, project_id: r.project_id,
    project_name: r.project_name, task_number: r.task_number, title: r.title, created_at: r.created_at, task_available: !!r.task_id };
  draftSave(); renderDraftContext(); closeModal();
  S.chatOpen = true; ls.set('chatOpen', true); setView('board');
  $('#chatInput').focus();
}

function draftEdited() {
  if (D.value.pending) return;
  D.value.request_id = null; draftSave();
}

function dismissLostFiles() { D.value.filesMissing = 0; draftSave(); renderDraftContext(); }

window.addEventListener('pagehide', draftSave);

function updateChatViewport() {
  const height = window.visualViewport?.height || innerHeight;
  const baseline = Math.max(innerHeight, screen.height - 80);
  document.documentElement.classList.toggle('keyboard-open', document.activeElement?.id === 'chatInput'
    && matchMedia('(max-width: 900px)').matches && height < baseline * .75);
}
window.visualViewport?.addEventListener('resize', updateChatViewport);
window.addEventListener('resize', updateChatViewport);
document.addEventListener('focusin', updateChatViewport);
document.addEventListener('focusout', () => setTimeout(updateChatViewport, 0));
