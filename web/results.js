// Один системный раздел проекта. Обычные чаты и Telegram не управляют этой историей.
const R = { pid: null, items: [], more: false, loading: false, error: '', latest: 0 };
const resultsReadKey = () => `results.read.${S.state?.installation_id || 'legacy'}.${S.projectId}`;

async function loadResults(older = false) {
  const pid = S.projectId, ok = fresh('results');
  if (!pid) return;
  if (R.pid !== pid) Object.assign(R, { pid, items: [], more: false, error: '' });
  const before = older && R.items.length ? `?before=${R.items.at(-1).id}` : '';
  R.loading = true;
  if (S.view === 'results' && !R.items.length) renderResults();
  try {
    const page = await api('GET', `/api/projects/${pid}/results${before}`);
    if (!ok() || pid !== S.projectId) return;
    const items = older ? [...R.items, ...page.items.filter(x => !R.items.some(r => r.id === x.id))] :
      [...page.items, ...R.items.filter(x => x.id < (page.items.at(-1)?.id || 0))];
    const changed = JSON.stringify(items) !== JSON.stringify(R.items) || R.error;
    Object.assign(R, { items, more: older ? page.has_more : R.items.length > page.items.length ? R.more : page.has_more,
      latest: Math.max(0, ...items.map(x => x.id)), loading: false, error: '' });
    if (S.view === 'results' && (changed || !$('#results-list'))) renderResults();
    await loadResultBadge();
  } catch {
    if (!ok() || pid !== S.projectId) return;
    R.loading = false; R.error = 'Сервер недоступен. История появится после восстановления связи.';
    if (S.view === 'results') renderResults();
  }
}

async function loadResultBadge() {
  const pid = S.projectId, ok = fresh('resultBadge');
  if (!pid) { if ($('#rbadge')) $('#rbadge').innerHTML = ''; return; }
  try {
    const page = await api('GET', `/api/projects/${pid}/results?after=${ls.get(resultsReadKey(), 0)}&limit=1`);
    if (ok() && pid === S.projectId && $('#rbadge')) $('#rbadge').innerHTML = page.items.length ? '<span class="badge" aria-label="Есть непрочитанные результаты">●</span>' : '';
  } catch { /* Переподключение и периодическая сверка повторят запрос. */ }
}

function resultFiles(r) {
  return r.files.map(f => {
    const url = esc(f.url), name = esc(f.name).replace(/"/g, '&quot;'), size = f.size < 1048576 ? `${Math.ceil(f.size / 1024)} КБ` : `${(f.size / 1048576).toFixed(1)} МБ`;
    const preview = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(f.type)
      ? `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" loading="lazy" alt="${name}"></a>`
      : ['video/mp4', 'video/webm'].includes(f.type) ? `<video controls preload="metadata" playsinline src="${url}" aria-label="${name}"></video>` : '';
    return `<div class="result-file">${preview}<a href="${url}?download=1" download>${name}</a><small>${size} · скачать</small></div>`;
  }).join('');
}

function resultHtml(r, detail = false) {
  const unread = r.id > ls.get(resultsReadKey(), 0);
  return `<article class="result-entry ${unread ? 'unread' : ''}" id="result-${detail ? 'detail-' : ''}${r.id}">
    <div class="result-meta"><b>${esc(r.project_name)} #${r.task_number}</b><time>${fmtTime(r.created_at)}</time>${unread ? '<span class="result-new">Новое</span>' : ''}</div>
    <h3>${esc(r.title)}</h3><div class="md result-text">${refs(md(r.text), r.project_id)}</div>
    ${r.error ? `<p class="err-text" role="status">${esc(r.error)}</p>` : ''}
    <div class="result-files">${resultFiles(r)}</div>
    <div class="result-actions">${r.task_id ? `<button onclick="App.openTask(${r.task_id})">Карточка задачи</button>` : '<span class="muted">Карточка удалена, результат сохранён</span>'}
      <button class="ghost" onclick="App.openResult(${r.id})">Отчёт и материалы</button>
      <button class="primary discuss-result" onclick="App.discussResult(${r.id})">Обсудить</button></div></article>`;
}

function renderResults() {
  const items = R.pid === S.projectId ? R.items : [];
  const scroll = $('#view').scrollTop;
  $('#view').innerHTML = `<section class="results"><div class="results-head"><div><h2>Результаты</h2><p>Завершённые задачи проекта «${esc(project()?.name || '')}»</p></div>
    <button onclick="App.readResults()" ${!items.length ? 'disabled' : ''}>Всё прочитано</button></div>
    <p class="hint">Общая история сайта и приложения. Telegram подключается отдельно.</p>
    ${R.error ? `<p role="alert">${esc(R.error)} <button onclick="App.loadResults()">Повторить</button></p>` : ''}
    <div id="results-list">${items.map(r => resultHtml(r)).join('') || `<div class="results-empty">${R.loading ? 'Загружаю результаты…' : 'Здесь появятся итоги и материалы завершённых задач. Подключение Telegram не требуется.'}</div>`}</div>
    ${R.more ? '<button class="results-more" onclick="App.loadResults(true)">Показать предыдущие</button>' : ''}</section>`;
  $('#view').scrollTop = scroll;
}

function readResults() {
  ls.set(resultsReadKey(), R.latest); renderResults(); loadResultBadge();
}

async function openResult(id) {
  const r = await api('GET', `/api/results/${id}`);
  if (r.project_id !== S.projectId) await selectProject(r.project_id);
  setView('results');
  closeModal();
  modal(`<h3>${esc(r.project_name)} #${r.task_number}</h3>${resultHtml(r, true)}<details><summary>Полный отчёт этого завершения</summary><div class="md">${refs(md(r.report), r.project_id)}</div></details>
    <div class="actions"><button onclick="App.closeModal()">Закрыть</button></div>`);
  // В модальном окне повторный переход к тому же отчёту не нужен.
  $('#modal .result-actions .ghost')?.remove();
}

function platformSettings() {
  return window.AgentPipelineAndroid ? '<div class="section"><h3>Android-приложение</h3><p class="hint">Подключение к серверу, разрешение уведомлений и фоновая проверка результатов.</p><button onclick="AgentPipelineAndroid.settings()">Подключение и уведомления</button></div>' : '';
}

function androidBack() {
  if ($('#modal').innerHTML) { closeModal(); return true; }
  if (S.chatListOpen) { S.chatListOpen = false; renderChatHead(); return true; }
  if (S.chatOpen && matchMedia('(max-width: 900px)').matches) { toggleChat(); return true; }
  if (S.view !== 'board') { setView('board'); return true; }
  return false;
}
