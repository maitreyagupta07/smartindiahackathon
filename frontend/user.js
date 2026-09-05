/* User workspace: task sidebar, composer (centered idle / docked active),
   task lifecycle state machine, minimal live-state strip, maximized
   execution graph overlay, node detail drawer, notifications, and the
   gradient color picker. Backend contract only (§2.3) — no parallel APIs.
   When the real backend isn't reachable, tasks run through an honestly
   labeled client-side Demo simulation instead. */

const PHASES = ['classification', 'routing', 'tool', 'validation', 'deliverable'];
const PHASE_LABEL = {
  classification: 'Classifying',
  routing: 'Routing',
  tool: 'Processing',
  validation: 'Validating',
  deliverable: 'Completing',
};

let LIVE_BACKEND = false;
let currentTask = null; // the in-flight / most-recent assistant turn (drives live strip + graph)
let pollHandle = null;

/* ============================================================
   Chat state — the active conversation. chat_id is generated
   client-side (Store.newChatId), is the isolation key, and is
   sent on every upload and every message. Switching chats
   switches it; "New Chat" starts a fresh one.
   ============================================================ */
let currentChatId = null;
let currentChatTitle = null;
let chatMessages = []; // ordered turns: {role:'user'|'assistant'|'system', ...}

function persistCurrentChat() {
  if (!currentChatId) return;
  Store.saveChatMessages(currentChatId, chatMessages);
  Store.upsertChat({
    chat_id: currentChatId,
    title: currentChatTitle || firstPromptTitle() || 'New Chat',
    updated_at: new Date().toISOString(),
  });
}

function firstPromptTitle() {
  const firstUser = chatMessages.find((m) => m.role === 'user' && m.prompt);
  return firstUser ? truncate(firstUser.prompt, 40) : null;
}

function ensureChat() {
  if (!currentChatId) {
    currentChatId = Store.newChatId();
    currentChatTitle = null;
    chatMessages = [];
  }
  return currentChatId;
}

/* ============================================================
   State machine: turns raw backend fields into honest UI state
   ============================================================ */
function computeGraphState(task, nowMs) {
  const startedMs = new Date(task.started_at || task.submitted_at_client).getTime();
  const elapsed = Math.max(0, nowMs - startedMs);
  const state = {
    status: task.status,
    stages: { classification: 'pending', routing: 'pending', tool: 'pending', validation: 'pending', deliverable: 'pending' },
    routingBranch: null,
    toolBranch: null,
    error: task.error || null,
  };

  if (task.status === 'queued' || task.status === 'processing') {
    const idx = elapsed < 1500 ? 0 : elapsed < 3500 ? 1 : 2;
    ['classification', 'routing', 'tool'].forEach((stage, i) => {
      state.stages[stage] = i < idx ? 'completed' : i === idx ? 'active' : 'pending';
    });
    return state;
  }

  // completed or failed — reconcile with real fields, never guess beyond them
  state.stages.classification = 'completed';
  state.stages.routing = 'completed';
  state.routingBranch = routeForModel(task.model_used);

  const resultType = task.result && task.result.type;
  if (resultType === 'file') {
    state.stages.tool = 'completed';
    state.toolBranch = 'file';
  } else {
    state.stages.tool = 'na'; // honest: the contract doesn't expose which/whether a tool ran for text results
    state.toolBranch = 'na';
  }

  if (task.status === 'failed') {
    state.stages.validation = 'error';
    state.stages.deliverable = 'error';
  } else {
    state.stages.validation = 'completed';
    state.stages.deliverable = 'completed';
  }
  return state;
}

function routeLabelFor(branch) {
  return { text: 'Text · qwen2.5-1.5b', vision: 'Vision · moondream', lora: 'Approval-Note LoRA' }[branch] || null;
}

/* ============================================================
   View switching: idle (centered composer) vs active conversation
   ============================================================ */
function updateViewMode() {
  const active = chatMessages.length > 0;
  document.getElementById('idle-view').hidden = active;
  document.getElementById('conversation-scroll').hidden = !active;
  document.getElementById('composer-dock').hidden = !active;
}

/* ============================================================
   Minimal live strip (above the docked composer only)
   ============================================================ */
function renderLiveStrip(state) {
  const strip = document.getElementById('live-strip');
  if (!strip) return;
  const idle = !currentTask || currentTask.status === 'completed' || currentTask.status === 'failed';
  strip.classList.toggle('visible', !!currentTask && !idle);
  if (!currentTask) return;

  const html = PHASES.slice(0, 4).map((phase, i) => {
    const st = state.stages[phase];
    const cls = st === 'completed' ? 'done' : st === 'active' ? 'active' : '';
    const glyph = st === 'completed'
      ? '<iconify-icon icon="lucide:check" class="glyph" style="font-size:11px"></iconify-icon>'
      : st === 'active'
        ? '<span class="dot"></span>'
        : '<span class="ring"></span>';
    const connector = i > 0 ? `<span class="live-connector ${state.stages[PHASES[i - 1]] === 'completed' || state.stages[PHASES[i - 1]] === 'active' ? 'on' : ''}"></span>` : '';
    return `${connector}<span class="live-step ${cls}">${glyph}<span>${PHASE_LABEL[phase]}</span></span>`;
  }).join('');

  const lastConnectorOn = state.stages.validation !== 'pending';
  const completingState = state.stages.validation;
  const completingCls = completingState === 'completed' || completingState === 'error' ? 'done' : completingState === 'active' ? 'active' : '';
  const completingGlyph = completingCls === 'done'
    ? '<iconify-icon icon="lucide:check" class="glyph" style="font-size:11px"></iconify-icon>'
    : '<span class="ring"></span>';

  strip.innerHTML = html +
    `<span class="live-connector ${lastConnectorOn ? 'on' : ''}"></span>` +
    `<span class="live-step ${completingCls}">${completingGlyph}<span>${PHASE_LABEL.deliverable}</span></span>` +
    `<a href="#" class="live-expand" id="expand-from-strip" title="Expand execution graph"><iconify-icon icon="lucide:maximize-2" style="font-size:12px"></iconify-icon></a>`;

  const expandBtn = document.getElementById('expand-from-strip');
  if (expandBtn) expandBtn.addEventListener('click', (e) => { e.preventDefault(); openGraph(); });
}

/* ============================================================
   Conversation rendering
   ============================================================ */
function renderConversation() {
  updateViewMode();
  const titleEl = document.getElementById('topbar-task-title');
  const idEl = document.getElementById('topbar-task-id');

  if (!chatMessages.length) {
    titleEl.textContent = 'Workspace';
    idEl.textContent = '';
    return;
  }
  titleEl.textContent = currentChatTitle || firstPromptTitle() || 'Chat';
  idEl.textContent = currentChatId ? `#${String(currentChatId).slice(0, 8).toUpperCase()}` : '';

  const inner = document.getElementById('conversation-inner');
  inner.innerHTML = chatMessages.map(renderTurn).join('');
  const scroller = document.getElementById('conversation-scroll');
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
}

function renderTurn(turn) {
  if (turn.role === 'user') {
    return `
      <div class="msg-user">
        <div class="bubble">${escapeHtml(turn.prompt || '')}</div>
        ${turn.file_name ? `<div class="file-chip"><iconify-icon icon="${fileTypeIcon(turn.file_name)}"></iconify-icon>${escapeHtml(turn.file_name)}</div>` : ''}
      </div>`;
  }

  if (turn.role === 'system') {
    if (turn.kind === 'upload') {
      return `
        <div class="msg-system">
          <div class="kb-upload-chip">
            <iconify-icon icon="lucide:file-text"></iconify-icon>
            <div class="kb-upload-body">
              <div class="kb-upload-name mono">${escapeHtml(turn.filename || 'document.pdf')}</div>
              <div class="kb-upload-status"><span class="status-dot ok"></span>Added to Knowledge Base ✓${turn.chunks ? ` · ${turn.chunks} chunk${turn.chunks === 1 ? '' : 's'}` : ''}</div>
            </div>
          </div>
        </div>`;
    }
    // upload error
    return `
      <div class="msg-system">
        <div class="kb-upload-chip error">
          <iconify-icon icon="lucide:alert-triangle"></iconify-icon>
          <div class="kb-upload-body">
            <div class="kb-upload-name mono">${escapeHtml(turn.filename || 'upload')}</div>
            <div class="kb-upload-status err">${escapeHtml(turn.message || 'Upload failed.')}</div>
          </div>
        </div>
      </div>`;
  }

  // assistant turn
  if (turn.status === 'failed') {
    return `<div class="msg-ai"><div class="error-banner"><iconify-icon icon="lucide:alert-triangle" style="font-size:16px;flex-shrink:0;margin-top:1px"></iconify-icon><div>${escapeHtml(turn.error || 'The request failed.')}</div></div>${turn.task_id ? taskMetaRow(turn) : ''}</div>`;
  }
  if (turn.status !== 'completed') {
    return `<div class="msg-ai"><p class="summary-text" style="color:var(--text-muted)">Working on it${LIVE_BACKEND ? '' : ' (demo simulation)'}…</p></div>`;
  }

  const chip = turn.model_used
    ? `<div class="model-chip"><iconify-icon icon="lucide:terminal" style="font-size:12px"></iconify-icon><span class="mono">${escapeHtml(turn.model_used)}</span></div>`
    : '';
  const result = turn.result || {};
  let body = '';
  if (result.type === 'file') {
    const fn = result.file_name || 'deliverable';
    const url = result.file_url || '#';
    body = `
      <div class="deliverable-card">
        <div class="left">
          <div class="file-icon"><iconify-icon icon="${fileTypeIcon(fn)}"></iconify-icon></div>
          <div style="min-width:0">
            <div class="name mono">${escapeHtml(fn)}</div>
            <div class="status-line"><span class="status-dot ok"></span>Ready</div>
          </div>
        </div>
        <div class="actions">
          <a class="btn-ghost" href="${url}" target="_blank" rel="noopener">Open</a>
          <a class="btn-icon-accent" href="${url}" download><iconify-icon icon="lucide:download" style="font-size:16px"></iconify-icon></a>
        </div>
      </div>`;
  } else if (result.text) {
    body = `<p class="summary-text">${escapeHtml(result.text)}</p>${renderSources(result.sources)}`;
  } else {
    body = `<p class="summary-text" style="color:var(--text-muted)">Completed with no returned content.</p>`;
  }
  return `<div class="msg-ai">${chip}${body}${turn.task_id ? taskMetaRow(turn) : ''}</div>`;
}

function renderSources(sources) {
  if (!Array.isArray(sources) || !sources.length) return '';
  const items = sources.map((s) => {
    const name = s.filename || 'document';
    const page = (s.page !== null && s.page !== undefined) ? `, page ${s.page}` : '';
    return `<li><iconify-icon icon="lucide:file-text" style="font-size:12px"></iconify-icon>${escapeHtml(name + page)}</li>`;
  }).join('');
  return `<div class="sources-block"><div class="sources-label">Sources</div><ul class="sources-list">${items}</ul></div>`;
}

/** Task ID / model / duration / token-usage row shown under a finished task.
 *  Token usage is intentionally always "not exposed" — see README's Backend
 *  Contract Gap note. This never fabricates a number. */
function taskMetaRow(task) {
  const duration = timeAgoOrDuration(task.started_at, task.completed_at);
  return `
    <div class="task-meta-row">
      <span class="task-meta-item mono" title="Task ID"><iconify-icon icon="lucide:hash" style="font-size:11px"></iconify-icon>${escapeHtml(task.task_id.slice(0, 8))}</span>
      ${task.model_used ? `<span class="task-meta-item mono" title="Model used"><iconify-icon icon="lucide:cpu" style="font-size:11px"></iconify-icon>${escapeHtml(task.model_used)}</span>` : ''}
      <span class="task-meta-item mono" title="Duration"><iconify-icon icon="lucide:timer" style="font-size:11px"></iconify-icon>${duration}</span>
      <span class="task-meta-item mono task-meta-gap" title="The current backend contract does not expose per-task token usage — see README."><iconify-icon icon="lucide:coins" style="font-size:11px"></iconify-icon>Tokens: not exposed</span>
    </div>`;
}

/* ============================================================
   Task sidebar (history) + Files & Deliverables
   ============================================================ */
function renderTaskSidebar() {
  const chats = Store.getChats();
  const listEl = document.getElementById('task-sidebar-list');
  const emptyEl = document.getElementById('task-sidebar-empty');
  emptyEl.hidden = chats.length > 0;
  listEl.innerHTML = chats.map((c) => chatSidebarItemHtml(c)).join('');
  listEl.querySelectorAll('[data-open-chat]').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      openChat(el.dataset.openChat);
    });
  });

  // Files & Deliverables — file results + KB uploads in the ACTIVE chat.
  const files = [];
  chatMessages.forEach((m) => {
    if (m.role === 'assistant' && m.result && m.result.type === 'file' && m.result.file_name) {
      files.push({ name: m.result.file_name, url: m.result.file_url || '#' });
    }
    if (m.role === 'system' && m.kind === 'upload' && m.filename) {
      files.push({ name: m.filename, url: null, kb: true });
    }
  });
  const filesListEl = document.getElementById('files-sidebar-list');
  const filesEmptyEl = document.getElementById('files-sidebar-empty');
  filesEmptyEl.hidden = files.length > 0;
  filesListEl.innerHTML = files.map((f) => `
    <a href="${f.url || '#'}" class="task-sidebar-item" ${f.url ? 'target="_blank" rel="noopener"' : 'onclick="return false"'}>
      <iconify-icon icon="${f.kb ? 'lucide:book-marked' : fileTypeIcon(f.name)}" class="task-sidebar-item-icon"></iconify-icon>
      <div class="task-sidebar-item-body">
        <div class="task-sidebar-item-title mono">${escapeHtml(truncate(f.name, 24))}</div>
      </div>
    </a>
  `).join('');
}

function chatSidebarItemHtml(c) {
  const isActive = currentChatId && currentChatId === c.chat_id;
  return `
    <a href="#" class="task-sidebar-item ${isActive ? 'active' : ''}" data-open-chat="${c.chat_id}">
      <iconify-icon icon="lucide:message-square" class="task-sidebar-item-icon"></iconify-icon>
      <div class="task-sidebar-item-body">
        <div class="task-sidebar-item-title">${escapeHtml(truncate(c.title || 'New Chat', 28))}</div>
      </div>
    </a>`;
}

function openChat(chatId) {
  if (!chatId || chatId === currentChatId) return;
  stopPolling();
  currentTask = null;
  currentChatId = chatId;
  chatMessages = Store.getChatMessages(chatId);
  const meta = Store.getChats().find((c) => c.chat_id === chatId);
  currentChatTitle = meta ? meta.title : null;
  // Any turn left mid-flight in a previous session is stale — settle it.
  chatMessages.forEach((m) => {
    if (m.role === 'assistant' && m.status !== 'completed' && m.status !== 'failed') {
      m.status = 'failed';
      m.error = 'This response was interrupted. Ask the question again.';
    }
  });
  renderConversation();
  renderTaskSidebar();
}

/* ============================================================
   Polling / lifecycle
   ============================================================ */
function stopPolling() { if (pollHandle) { clearInterval(pollHandle); pollHandle = null; } }

function tick() {
  if (!currentTask) return;
  const state = computeGraphState(currentTask, Date.now());
  renderLiveStrip(state);
  if (graphOpen) renderGraph(state);
}

function activeAssistantTurn() {
  for (let i = chatMessages.length - 1; i >= 0; i--) {
    if (chatMessages[i].role === 'assistant') return chatMessages[i];
  }
  return null;
}

async function pollReal(taskId) {
  try {
    const data = await Api.getTaskStatus(taskId);
    const turn = activeAssistantTurn();
    const wasSettled = turn && (turn.status === 'completed' || turn.status === 'failed');
    if (turn) Object.assign(turn, data);
    if (currentTask) Object.assign(currentTask, data);
    persistCurrentChat();
    renderConversation();
    renderTaskSidebar();
    if ((data.status === 'completed' || data.status === 'failed') && !wasSettled) {
      stopPolling();
      if (turn) Notifications.notifyTaskSettled({ task_id: taskId, status: data.status, prompt: currentChatTitle || firstPromptTitle() || 'Chat', error: data.error });
    }
    tick();
  } catch (err) {
    toast(`Status check failed: ${err.message}`, true);
  }
}

function startPollingReal(taskId) {
  stopPolling();
  pollHandle = setInterval(() => pollReal(taskId), 1200);
  pollReal(taskId);
}

/* Demo simulation — clearly labeled, purely client-side, exercises the exact
   same state machine/UI so the product can be demoed without the full stack. */
function runDemoSimulation(task) {
  stopPolling();
  const totalMs = 3200;
  const tId = setInterval(() => tick(), 200);
  setTimeout(() => {
    clearInterval(tId);
    const turn = activeAssistantTurn();
    const completed_at = new Date().toISOString();
    const patch = {
      status: 'completed',
      model_used: 'qwen2.5:1.5b-instruct',
      completed_at,
      error: null,
      result: {
        type: 'text',
        text: 'This is a demo-simulated response — no real backend was reached, so this content is illustrative only. Run the backend, agent and tools services to get real chat-scoped Knowledge Base answers.',
        file_url: null, file_name: null, sources: null,
      },
    };
    if (turn) Object.assign(turn, patch);
    if (currentTask) Object.assign(currentTask, patch);
    persistCurrentChat();
    renderConversation();
    renderTaskSidebar();
    tick();
  }, totalMs);
}

/* ============================================================
   Composer (two instances: centered-idle and docked-active,
   sharing one submit pipeline)
   ============================================================ */
let pendingFile = null;

function initComposer() {
  const fileInput = document.getElementById('file-input');
  const idleInput = document.getElementById('composer-input-idle');
  const idleSend = document.getElementById('composer-send-idle');
  const idleAttach = document.getElementById('composer-attach-idle');
  const idlePreview = document.getElementById('file-preview-idle');
  const dockedInput = document.getElementById('composer-input');
  const dockedSend = document.getElementById('composer-send');
  const dockedAttach = document.getElementById('composer-attach');
  const dockedPreview = document.getElementById('file-preview');

  function syncFilePreview() {
    [idlePreview, dockedPreview].forEach((el) => {
      if (!pendingFile) { el.classList.remove('show'); return; }
      el.querySelector('.name').textContent = pendingFile.name;
      el.classList.add('show');
    });
  }
  [idlePreview, dockedPreview].forEach((el) => {
    el.querySelector('button').addEventListener('click', () => {
      pendingFile = null; fileInput.value = ''; syncFilePreview();
    });
  });
  [idleAttach, dockedAttach].forEach((btn) => btn.addEventListener('click', () => fileInput.click()));
  fileInput.addEventListener('change', () => {
    pendingFile = fileInput.files[0] || null;
    syncFilePreview();
  });

  function setSendEnabled(on) {
    idleSend.disabled = !on; dockedSend.disabled = !on;
  }

  async function handleUpload(file) {
    ensureChat();
    const file_name = file.name;
    const file_mime_type = file.type || (file_name.toLowerCase().endsWith('.pdf') ? 'application/pdf' : null);

    if (file_mime_type !== 'application/pdf' && !file_name.toLowerCase().endsWith('.pdf')) {
      chatMessages.push({ role: 'system', kind: 'upload-error', filename: file_name, message: 'Only PDF files can be added to the Knowledge Base.' });
      persistCurrentChat(); renderConversation(); renderTaskSidebar();
      return;
    }

    if (!LIVE_BACKEND) {
      chatMessages.push({ role: 'system', kind: 'upload', filename: file_name, chunks: 0, demo: true });
      persistCurrentChat(); renderConversation(); renderTaskSidebar();
      toast('Demo mode — the PDF was not actually indexed.');
      return;
    }

    const chat_title = currentChatTitle || firstPromptTitle() || null;
    try {
      const file_base64 = await fileToBase64(file);
      const res = await Api.chatUpload(currentChatId, {
        user_id: Store.USER_ID, file_base64, file_name, file_mime_type, chat_title,
      });
      chatMessages.push({ role: 'system', kind: 'upload', filename: res.filename || file_name, chunks: res.chunks || 0, document_id: res.document_id });
      toast(`${res.filename || file_name} added to the Knowledge Base.`);
    } catch (err) {
      chatMessages.push({ role: 'system', kind: 'upload-error', filename: file_name, message: err.message });
      toast(`Upload failed: ${err.message}`, true);
    }
    persistCurrentChat(); renderConversation(); renderTaskSidebar();
  }

  async function handleMessage(prompt) {
    ensureChat();
    const submitted_at_client = new Date().toISOString();
    if (!currentChatTitle) currentChatTitle = truncate(prompt, 40);

    chatMessages.push({ role: 'user', prompt });
    const assistantTurn = {
      role: 'assistant', status: 'processing', task_id: null,
      model_used: null, started_at: submitted_at_client, completed_at: null,
      result: { type: null, text: null, file_url: null, file_name: null, sources: null }, error: null,
    };
    chatMessages.push(assistantTurn);
    persistCurrentChat();
    renderConversation();
    renderTaskSidebar();

    currentTask = {
      task_id: null, status: 'queued', prompt,
      model_used: null, started_at: null, completed_at: null,
      result: { type: null, text: null, file_url: null, file_name: null },
      error: null, submitted_at_client, demo: !LIVE_BACKEND,
    };
    tick();

    if (!LIVE_BACKEND) {
      assistantTurn.status = 'processing';
      currentTask.status = 'processing';
      currentTask.started_at = submitted_at_client;
      runDemoSimulation(currentTask);
      return;
    }

    try {
      const res = await Api.chatMessage(currentChatId, {
        user_id: Store.USER_ID, prompt, chat_title: currentChatTitle,
      });
      assistantTurn.task_id = res.task_id;
      currentTask.task_id = res.task_id;
      currentTask.status = res.status || 'queued';
      persistCurrentChat();
      startPollingReal(res.task_id);
    } catch (err) {
      assistantTurn.status = 'failed';
      assistantTurn.error = err.message;
      currentTask = null;
      persistCurrentChat();
      renderConversation();
      toast(`Couldn't send message: ${err.message}`, true);
    }
  }

  async function submit(fromInput) {
    const prompt = fromInput.value.trim();
    const file = pendingFile;
    if (!prompt && !file) return;

    setSendEnabled(false);
    idleInput.value = ''; dockedInput.value = '';
    fileInput.value = '';
    pendingFile = null;
    syncFilePreview();

    try {
      if (file) await handleUpload(file);
      if (prompt) await handleMessage(prompt);
    } catch (err) {
      toast(`Something went wrong: ${err.message}`, true);
    } finally {
      setSendEnabled(true);
    }
  }

  idleSend.addEventListener('click', () => submit(idleInput));
  idleInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(idleInput); });
  dockedSend.addEventListener('click', () => submit(dockedInput));
  dockedInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(dockedInput); });

  document.getElementById('nav-new-task').addEventListener('click', (e) => {
    e.preventDefault();
    stopPolling();
    currentTask = null;
    currentChatId = null;
    currentChatTitle = null;
    chatMessages = [];
    renderConversation();
    renderTaskSidebar();
    renderLiveStrip({ stages: {} });
    idleInput.focus();
  });
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

/* ============================================================
   Maximized execution graph
   ============================================================ */
let graphOpen = false;

const NODE_META = {
  task: { icon: 'lucide:file-input', label: 'Task', title: () => truncate(currentTask ? currentTask.prompt : '', 26) },
  classification: { icon: 'lucide:tags', label: 'Classification', title: () => 'Task Type Detection' },
  routing: { icon: 'lucide:route', label: 'Routing', title: () => 'Model & Compute Logic' },
  tool: { icon: 'lucide:cpu', label: 'Knowledge / Tool', title: () => 'Tool & Knowledge Activity' },
  validation: { icon: 'lucide:shield-check', label: 'Validation', title: () => 'Result Validation' },
  deliverable: { icon: 'lucide:package-check', label: 'Result', title: () => 'Deliverable' },
};

function openGraph() {
  graphOpen = true;
  document.getElementById('graph-overlay').classList.add('open');
  const subtitle = document.getElementById('graph-subtitle');
  const footerTask = document.getElementById('graph-footer-task');
  if (currentTask) {
    const tid = currentTask.task_id || 'pending';
    subtitle.textContent = `TASK-${tid.slice(0, 8).toUpperCase()}${currentTask.demo ? ' · DEMO' : ''}`;
    footerTask.textContent = `TASK: ${tid.slice(0, 12).toUpperCase()}`;
    renderGraph(computeGraphState(currentTask, Date.now()));
  } else {
    subtitle.textContent = 'No active task';
    footerTask.textContent = 'TASK: —';
    renderGraphEmpty();
  }
}
function closeGraph() {
  graphOpen = false;
  document.getElementById('graph-overlay').classList.remove('open');
  closeDrawer();
}

function renderGraphEmpty() {
  document.getElementById('graph-canvas').innerHTML = `
    <div style="text-align:center;color:var(--text-muted)">
      <iconify-icon icon="lucide:workflow" style="font-size:32px;opacity:.5;margin-bottom:12px;display:block"></iconify-icon>
      <p style="font-size:13px">No task in flight. Submit a task to see its execution graph.</p>
    </div>`;
}

function nodeCard(key, status, extra = '') {
  const meta = NODE_META[key];
  const stateClass = status === 'completed' ? 'completed' : status === 'active' ? 'active' : status === 'error' ? 'error' : status === 'na' ? 'na' : 'pending';
  const icon = status === 'completed' ? 'lucide:check' : status === 'error' ? 'lucide:x' : status === 'na' ? 'lucide:minus' : meta.icon;
  const pulse = status === 'active' ? '<span class="node-pulse" style="position:absolute;top:10px;right:10px"></span>' : '';
  return `
    <button class="node-card ${stateClass}" data-node="${key}" ${extra}>
      ${pulse}
      <div class="node-label">
        <span class="node-icon"><iconify-icon icon="${icon}"></iconify-icon></span>
        <span class="node-eyebrow">${meta.label}</span>
      </div>
      <div class="node-title">${meta.title()}</div>
    </button>`;
}

function connector(status) {
  const cls = status === 'completed' ? 'complete' : status === 'active' ? 'active' : '';
  return `<div class="dash-connector ${cls}"></div>`;
}

function renderGraph(state) {
  const canvas = document.getElementById('graph-canvas');
  const routeBranches = [
    { key: 'text', label: 'Text → qwen2.5-1.5b' },
    { key: 'vision', label: 'Vision → moondream' },
    { key: 'lora', label: 'Approval-Note LoRA' },
  ];
  const toolBranches = [
    { key: 'file', label: 'Generate File' },
    { key: 'na', label: 'No tool call reported' },
  ];

  const routingPills = routeBranches.map((b) => {
    const isActive = state.routingBranch === b.key;
    const isKnown = !!state.routingBranch;
    const cls = isActive ? 'active' : isKnown ? 'dim' : '';
    return `<div class="satellite-pill ${cls}" data-node="route-${b.key}" role="button" tabindex="0"><span class="dot"></span>${b.label}</div>`;
  }).join('');

  const toolPills = toolBranches.map((b) => {
    const isActive = state.toolBranch === b.key;
    const isKnown = !!state.toolBranch;
    const cls = isActive ? 'active' : isKnown ? 'dim' : '';
    return `<div class="satellite-pill ${cls}" data-node="tool-${b.key}" role="button" tabindex="0"><span class="dot"></span>${b.label}</div>`;
  }).join('');

  canvas.innerHTML = `
    <div class="graph-chain">
      <div class="node-wrap">${nodeCard('task', 'completed')}</div>
      ${connector('completed')}
      <div class="node-wrap">${nodeCard('classification', state.stages.classification)}</div>
      ${connector(state.stages.classification)}
      <div class="node-wrap">
        <div class="branch-cluster top">
          <div class="branch-line ${state.routingBranch ? 'active' : ''}"></div>
          ${routingPills}
        </div>
        ${nodeCard('routing', state.stages.routing)}
      </div>
      ${connector(state.stages.routing)}
      <div class="node-wrap">
        <div class="branch-cluster bottom">
          <div class="branch-line ${state.toolBranch ? 'active' : ''}"></div>
          ${toolPills}
        </div>
        ${nodeCard('tool', state.stages.tool)}
      </div>
      ${connector(state.stages.tool === 'na' ? state.stages.validation : state.stages.tool)}
      <div class="node-wrap">${nodeCard('validation', state.stages.validation)}</div>
      ${connector(state.stages.validation === 'error' ? 'error' : state.stages.validation)}
      <div class="node-wrap">${nodeCard('deliverable', state.stages.deliverable)}</div>
    </div>`;

  canvas.querySelectorAll('[data-node]').forEach((el) => {
    el.addEventListener('click', () => openDrawerFor(el.dataset.node, state));
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawerFor(el.dataset.node, state); }
    });
  });
}

/* ============================================================
   Drawer
   ============================================================ */
function openDrawerFor(nodeKey, state) {
  const drawer = document.getElementById('drawer');
  const overlay = document.getElementById('drawer-overlay');
  const title = document.getElementById('drawer-title');
  const statusPill = document.getElementById('drawer-status');
  const body = document.getElementById('drawer-body');

  const t = currentTask;
  let heading = nodeKey, status = 'pending', desc = '', kv = [];

  const setPill = (s) => {
    statusPill.className = `status-pill ${s}`;
    statusPill.textContent = s.toUpperCase();
  };

  if (nodeKey.startsWith('route-')) {
    const branch = nodeKey.replace('route-', '');
    heading = routeLabelFor(branch) || branch;
    status = state.routingBranch === branch ? 'completed' : state.routingBranch ? 'na' : 'pending';
    desc = status === 'completed' ? 'This is the route the router actually selected for this task.' : status === 'na' ? 'Not the route taken for this task.' : 'Routing decision not yet resolved.';
  } else if (nodeKey.startsWith('tool-')) {
    const branch = nodeKey.replace('tool-', '');
    heading = branch === 'file' ? 'Generate File' : 'No tool call reported';
    status = state.toolBranch === branch ? (branch === 'na' ? 'na' : 'completed') : state.toolBranch ? 'na' : 'pending';
    desc = branch === 'file'
      ? (status === 'completed' ? `A file was generated for this task: ${t.result && t.result.file_name ? t.result.file_name : ''}` : 'This task did not produce a file result.')
      : 'The task-status API does not report which (if any) tool ran for a text result — shown honestly as unconfirmed rather than guessed.';
  } else {
    const s = state.stages[nodeKey];
    heading = NODE_META[nodeKey].title();
    status = s === 'active' ? 'active' : s === 'completed' ? 'completed' : s === 'error' ? 'error' : s === 'na' ? 'na' : 'pending';

    if (nodeKey === 'task') {
      status = 'completed';
      desc = 'The original request submitted by the user.';
      kv = [
        { k: 'Task ID', v: t.task_id },
        { k: 'Submitted', v: new Date(t.submitted_at_client).toLocaleString() },
        { k: 'Prompt', v: truncate(t.prompt, 80) },
        t.file_name ? { k: 'Attachment', v: t.file_name } : null,
      ].filter(Boolean);
    } else if (nodeKey === 'classification') {
      desc = status === 'pending' ? '' : 'The system determined how this request should be handled.';
      if (status !== 'pending') kv = [{ k: 'Status', v: status === 'active' ? 'In progress' : 'Resolved' }];
    } else if (nodeKey === 'routing') {
      desc = status === 'pending' ? '' : 'Selects which local model handles the request.';
      if (t.model_used) kv = [{ k: 'Model Selected', v: t.model_used }, { k: 'Status', v: 'Resolved' }, { k: 'Token Usage', v: 'Not exposed by current backend contract' }];
      else if (status === 'active') kv = [{ k: 'Status', v: 'Resolving…' }];
    } else if (nodeKey === 'tool') {
      desc = status === 'na' ? 'No confirmed tool/knowledge activity for this result type.' : status === 'pending' ? '' : 'Local sandboxed tool or knowledge-base activity, when applicable.';
      if (status === 'completed' && t.result && t.result.file_name) kv = [{ k: 'Output File', v: t.result.file_name }];
    } else if (nodeKey === 'validation') {
      desc = status === 'error' ? 'The task returned an error.' : status === 'completed' ? 'Result returned without a reported error.' : '';
      if (t.error) kv = [{ k: 'Error', v: t.error }];
    } else if (nodeKey === 'deliverable') {
      if (status === 'completed' && t.result) {
        desc = t.result.type === 'file' ? 'A generated file is ready.' : 'A text result was returned.';
        kv = [
          { k: 'Type', v: t.result.type || '—' },
          t.result.file_name ? { k: 'File', v: t.result.file_name } : null,
          { k: 'Duration', v: timeAgoOrDuration(t.started_at, t.completed_at) },
        ].filter(Boolean);
      } else if (status === 'error') {
        desc = 'No deliverable — the task failed.';
      }
    }
  }

  setPill(status);
  title.textContent = heading;

  if (status === 'pending' || (status === 'na' && kv.length === 0 && !desc)) {
    body.innerHTML = `<div class="drawer-empty"><iconify-icon icon="lucide:lock"></iconify-icon><p>Not yet reached in this task's execution.</p></div>`;
  } else {
    body.innerHTML = `
      ${desc ? `<p class="drawer-desc">${escapeHtml(desc)}</p>` : ''}
      ${kv.map((row) => `<div class="kv-row"><span class="k">${escapeHtml(row.k)}</span><span class="v">${escapeHtml(row.v)}</span></div>`).join('')}
    `;
  }

  drawer.classList.add('open');
  overlay.classList.add('open');
}
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
}

/* ============================================================
   Notifications — browser Notification API only, gated on the
   existing task-status flow. No backend notification system.
   ============================================================ */
const Notifications = {
  PREF_KEY: 'sovereign-notify-enabled',
  supported() { return typeof window !== 'undefined' && 'Notification' in window; },
  enabled() { return localStorage.getItem(this.PREF_KEY) === 'true'; },
  permission() { return this.supported() ? Notification.permission : 'unsupported'; },

  async enable() {
    if (!this.supported()) { toast('Browser notifications are not supported here.', true); return; }
    if (Notification.permission === 'denied') {
      toast('Notifications are blocked for this site in your browser settings.', true);
      return;
    }
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    if (perm === 'granted') {
      localStorage.setItem(this.PREF_KEY, 'true');
      toast('You will be notified when a task finishes.');
    } else {
      toast('Notification permission was not granted.', true);
    }
    this.renderPop();
  },
  disable() {
    localStorage.setItem(this.PREF_KEY, 'false');
    this.renderPop();
  },

  notifyTaskSettled(task) {
    if (!this.supported() || !this.enabled() || Notification.permission !== 'granted') return;
    const title = task.status === 'completed' ? 'Task completed' : 'Task failed';
    const body = task.status === 'completed'
      ? truncate(task.prompt, 90)
      : (task.error ? truncate(task.error, 90) : truncate(task.prompt, 90));
    try {
      const n = new Notification(title, { body, tag: task.task_id });
      n.onclick = () => { window.focus(); };
    } catch { /* some browsers restrict Notification outside a user gesture context; fail silently */ }
  },

  renderPop() {
    const pop = document.getElementById('notif-pop');
    const bell = document.getElementById('notif-bell');
    if (!this.supported()) {
      pop.innerHTML = `<p class="notif-pop-text">Browser notifications aren't supported in this browser.</p>`;
      bell.classList.add('disabled');
      return;
    }
    const perm = Notification.permission;
    const on = this.enabled() && perm === 'granted';
    bell.classList.toggle('active', on);
    bell.classList.remove('disabled');
    if (perm === 'denied') {
      pop.innerHTML = `<p class="notif-pop-text">Notifications are blocked in your browser's site settings.</p>`;
      return;
    }
    pop.innerHTML = `
      <div class="notif-pop-row">
        <span>Notify me when a task finishes</span>
        <label class="switch">
          <input type="checkbox" id="notif-toggle" ${on ? 'checked' : ''}>
          <span class="switch-track"></span>
        </label>
      </div>
      <p class="notif-pop-text">Uses your browser's own notification permission — no separate backend notification system.</p>`;
    document.getElementById('notif-toggle').addEventListener('change', (e) => {
      if (e.target.checked) this.enable(); else this.disable();
    });
  },
};

function initNotifications() {
  const bell = document.getElementById('notif-bell');
  const pop = document.getElementById('notif-pop');
  Notifications.renderPop();
  bell.addEventListener('click', (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
  });
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== bell) pop.hidden = true;
  });
}

/* ============================================================
   Gradient color picker — recolors ONLY the atmospheric wash,
   leaving the graphite base, amber accent, and status colors intact.
   ============================================================ */
const GradientPicker = {
  KEY: 'sovereign-gradient-hue',
  PRESETS: [
    { name: 'Ember (default)', hue: null },
    { name: 'Amber', hue: 38 },
    { name: 'Rose', hue: 350 },
    { name: 'Violet', hue: 265 },
    { name: 'Azure', hue: 205 },
    { name: 'Emerald', hue: 150 },
  ],
  currentHue: null,
  apply(hue) {
    this.currentHue = hue;
    const root = document.documentElement;
    if (hue === null || hue === undefined) {
      root.style.removeProperty('--grad-1');
      root.style.removeProperty('--grad-2');
      root.style.removeProperty('--grad-3');
      return;
    }
    // Inline styles win over any stylesheet rule regardless of [data-theme],
    // so pick stops tuned for whichever theme is active right now — otherwise
    // a custom hue would carry dark-mode intensity into light mode or vice
    // versa. reapplyForTheme() re-runs this whenever the theme toggles.
    const isLight = root.getAttribute('data-theme') === 'light';
    if (isLight) {
      root.style.setProperty('--grad-1', `hsla(${hue}, 55%, 55%, 0.42)`);
      root.style.setProperty('--grad-2', `hsla(${hue}, 55%, 60%, 0.20)`);
      root.style.setProperty('--grad-3', `hsla(${hue}, 55%, 65%, 0.06)`);
    } else {
      root.style.setProperty('--grad-1', `hsla(${hue}, 62%, 39%, 0.65)`);
      root.style.setProperty('--grad-2', `hsla(${hue}, 60%, 27%, 0.38)`);
      root.style.setProperty('--grad-3', `hsla(${hue}, 55%, 20%, 0.12)`);
    }
  },
  reapplyForTheme() { if (this.currentHue !== null) this.apply(this.currentHue); },
  init() {
    const stored = localStorage.getItem(this.KEY);
    const hue = stored === null ? null : Number(stored);
    this.apply(hue);
    document.querySelectorAll('[data-theme-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => this.reapplyForTheme());
    });

    const toggle = document.getElementById('gradient-picker-toggle');
    const pop = document.getElementById('gradient-picker-pop');
    const row = document.getElementById('gradient-swatch-row');
    row.innerHTML = this.PRESETS.map((p) => {
      const previewHue = p.hue === null ? 14 : p.hue;
      const isActive = (hue === null && p.hue === null) || hue === p.hue;
      return `<button class="gradient-swatch ${isActive ? 'active' : ''}" data-hue="${p.hue === null ? '' : p.hue}" title="${p.name}" style="background:hsl(${previewHue},60%,42%)"></button>`;
    }).join('');
    row.querySelectorAll('.gradient-swatch').forEach((btn) => {
      btn.addEventListener('click', () => {
        const v = btn.dataset.hue;
        const newHue = v === '' ? null : Number(v);
        if (newHue === null) localStorage.removeItem(this.KEY); else localStorage.setItem(this.KEY, String(newHue));
        this.apply(newHue);
        row.querySelectorAll('.gradient-swatch').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });
    toggle.addEventListener('click', (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; });
    document.addEventListener('click', (e) => {
      if (!pop.hidden && !pop.contains(e.target) && e.target !== toggle) pop.hidden = true;
    });
  },
};

/* ---------- Helpers ---------- */
function truncate(str, n) { return !str ? '' : str.length > n ? str.slice(0, n - 1) + '…' : str; }
function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------- Init ---------- */
document.addEventListener('DOMContentLoaded', async () => {
  initComposer();
  // Resume the most recent chat, if any.
  const chats = Store.getChats();
  if (chats.length) {
    currentChatId = chats[0].chat_id;
    currentChatTitle = chats[0].title || null;
    chatMessages = Store.getChatMessages(currentChatId);
    chatMessages.forEach((m) => {
      if (m.role === 'assistant' && m.status !== 'completed' && m.status !== 'failed') {
        m.status = 'failed';
        m.error = 'This response was interrupted. Ask the question again.';
      }
    });
  }
  renderConversation();
  renderTaskSidebar();
  GradientPicker.init();
  initNotifications();

  document.getElementById('maximize-activity').addEventListener('click', (e) => { e.preventDefault(); openGraph(); });
  document.getElementById('graph-close').addEventListener('click', closeGraph);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  document.getElementById('drawer-overlay').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeDrawer(); if (graphOpen && !document.getElementById('drawer').classList.contains('open')) closeGraph(); }
  });

  LIVE_BACKEND = await Api.probe();
  const badge = document.getElementById('demo-badge');
  if (badge) badge.hidden = LIVE_BACKEND;
  if (!LIVE_BACKEND) toast('Backend not detected — running in demo simulation mode.');
});
