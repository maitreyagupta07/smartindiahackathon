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

/** Every distinct model actually used for this task, in call order. Prefers
 *  the real models_used trace (additive backend field — see
 *  app/agent/state.py's models_used property); falls back to the single
 *  model_used field for older/demo data that doesn't have it yet. */
function modelsUsedList(task) {
  if (Array.isArray(task.models_used) && task.models_used.length) return task.models_used;
  return task.model_used ? [task.model_used] : [];
}

const TOOL_LABELS = {
  execute_code: 'Code Execution (Sandbox)',
  search_docs: 'Document Search',
  generate_file: 'Generate File',
  scan_document: 'Handwritten Document Scan (OCR)',
};

/** Every tool actually called for this task, in call order, with its real
 *  per-call status — derived from the real step trace (task.steps) when
 *  present, so this is a FACT, not a guess. Falls back to the old
 *  file-result-implies-a-tool-ran inference when steps aren't available
 *  (older/demo data). */
function toolCallsList(task) {
  if (Array.isArray(task.steps) && task.steps.length) {
    return task.steps
      .filter((s) => s.action === 'call_tool' && s.tool_name)
      .map((s) => ({ tool_name: s.tool_name, label: TOOL_LABELS[s.tool_name] || s.tool_name, status: s.status }));
  }
  const resultType = task.result && task.result.type;
  if (resultType === 'file') return [{ tool_name: 'generate_file', label: TOOL_LABELS.generate_file, status: 'ok' }];
  return [];
}

/** Every model-calling step actually executed, in order, with the real
 *  action/model/status — for the routing node's detailed per-call list. */
function modelStepsList(task) {
  if (!Array.isArray(task.steps)) return [];
  return task.steps.filter((s) => s.action === 'call_qwen' || s.action === 'call_moondream');
}

function computeGraphState(task, nowMs) {
  const startedMs = new Date(task.started_at || task.submitted_at_client).getTime();
  const elapsed = Math.max(0, nowMs - startedMs);
  const state = {
    status: task.status,
    stages: { classification: 'pending', routing: 'pending', tool: 'pending', validation: 'pending', deliverable: 'pending' },
    routingBranch: null,       // last/primary branch — kept for back-compat call sites
    routingBranches: [],       // EVERY branch actually used (multi-model tasks light up more than one)
    toolBranch: null,          // last/primary tool — kept for back-compat call sites
    toolCalls: [],             // EVERY tool call actually made, real status each
    modelSteps: [],            // EVERY model call actually made, in order
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

  const models = modelsUsedList(task);
  state.routingBranches = [...new Set(models.map(routeForModel).filter(Boolean))];
  state.routingBranch = state.routingBranches[state.routingBranches.length - 1] || null;
  state.modelSteps = modelStepsList(task);

  state.toolCalls = toolCallsList(task);
  if (state.toolCalls.length) {
    state.stages.tool = state.toolCalls.some((c) => c.status === 'error') ? 'error' : 'completed';
    state.toolBranch = state.toolCalls[state.toolCalls.length - 1].tool_name;
  } else {
    // Only genuinely honest now when task.steps isn't available at all
    // (older/demo data) — with real steps, an empty list here is a
    // confirmed fact (no tool was called), not a guess.
    state.stages.tool = 'na';
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
  wireCopyButtons();
  wireSpeakButtons();
  startWorkingTicker();
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
    return `<div class="msg-ai">${workingLineHtml()}</div>`;
  }

  // Multi-model tasks (e.g. an image described by Moondream, then reasoned
  // over by Qwen) show EVERY model actually used, in call order — not just
  // the last one — via the additive models_used trace when present.
  const modelsForChip = modelsUsedList(turn);
  const chip = modelsForChip.length
    ? `<div class="model-chip"><iconify-icon icon="lucide:terminal" style="font-size:12px"></iconify-icon><span class="mono">${escapeHtml(modelsForChip.join(' → '))}</span></div>`
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
    const plain = answerToPlainText(result.text);
    body = `<div class="ai-rich">${renderRichText(result.text)}</div>${renderSources(result.sources)}` +
      `<div class="answer-actions">` +
      `<button class="copy-answer-btn" data-copy-text="${escapeHtml(plain)}">` +
      `<iconify-icon icon="lucide:copy" style="font-size:13px"></iconify-icon><span class="label">Copy answer</span></button>` +
      `<button class="speak-answer-btn" data-speak-text="${escapeHtml(plain)}" title="Read this answer aloud">` +
      `<iconify-icon icon="lucide:volume-2" style="font-size:13px"></iconify-icon><span class="label">Listen</span></button>` +
      `</div>`;
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
 *  Token usage is the real total from Ollama's own response (prompt_eval_count
 *  + eval_count, summed across every model call this task made) — never
 *  estimated. Falls back to "not available" only for older/demo data that
 *  predates this field. */
function taskMetaRow(task) {
  const duration = timeAgoOrDuration(task.started_at, task.completed_at);
  const usage = task.token_usage;
  const hasTokens = usage && typeof usage.total_tokens === 'number';
  const tokensLabel = hasTokens
    ? `${usage.total_tokens} tok (${usage.prompt_tokens ?? 0} in / ${usage.completion_tokens ?? 0} out)`
    : 'not available';
  return `
    <div class="task-meta-row">
      <span class="task-meta-item mono" title="Task ID"><iconify-icon icon="lucide:hash" style="font-size:11px"></iconify-icon>${escapeHtml(task.task_id.slice(0, 8))}</span>
      ${modelsUsedList(task).length ? `<span class="task-meta-item mono" title="Model(s) used, in call order"><iconify-icon icon="lucide:cpu" style="font-size:11px"></iconify-icon>${escapeHtml(modelsUsedList(task).join(' → '))}</span>` : ''}
      <span class="task-meta-item mono" title="Duration"><iconify-icon icon="lucide:timer" style="font-size:11px"></iconify-icon>${duration}</span>
      <span class="task-meta-item mono ${hasTokens ? '' : 'task-meta-gap'}" title="${hasTokens ? 'Real token usage reported by Ollama for this task' : 'Not available for this task'}"><iconify-icon icon="lucide:coins" style="font-size:11px"></iconify-icon>${escapeHtml(tokensLabel)}</span>
    </div>
    ${thinkingStepsHtml(task)}`;
}

/** Collapsible "Thinking" trace — the real per-step plan→act→observe record
 *  (task.steps, from app/agent/state.py's step_summary) rendered inline
 *  under the answer, so the reasoning is visible without opening the
 *  execution graph. Absent entirely for older/demo data with no steps. */
function thinkingStepsHtml(task) {
  if (!Array.isArray(task.steps) || !task.steps.length) return '';
  const rows = task.steps.map((s, i) => {
    const isModel = s.action === 'call_qwen' || s.action === 'call_moondream';
    const label = isModel
      ? `${s.action === 'call_moondream' ? 'Vision' : 'Text'} call · ${escapeHtml(s.model_used || '')}`
      : s.action === 'call_tool'
        ? `${TOOL_LABELS[s.tool_name] || escapeHtml(s.tool_name || 'tool')}`
        : escapeHtml(s.action || 'step');
    const statusIcon = s.status === 'ok' ? 'lucide:check' : s.status === 'error' ? 'lucide:x' : 'lucide:minus';
    const statusCls = s.status === 'ok' ? 'ok' : s.status === 'error' ? 'err' : '';
    const tokens = (typeof s.prompt_tokens === 'number' || typeof s.completion_tokens === 'number')
      ? ` <span class="think-step-tokens">(${s.prompt_tokens ?? 0}+${s.completion_tokens ?? 0} tok)</span>` : '';
    return `<li class="think-step"><span class="think-step-dot ${statusCls}"><iconify-icon icon="${statusIcon}"></iconify-icon></span><span class="think-step-label">${i + 1}. ${label}${tokens}</span>${s.error ? `<span class="think-step-err">${escapeHtml(s.error)}</span>` : ''}</li>`;
  }).join('');
  return `
    <details class="think-trace">
      <summary><iconify-icon icon="lucide:brain-circuit" style="font-size:12px"></iconify-icon>Show thinking (${task.steps.length} step${task.steps.length === 1 ? '' : 's'})</summary>
      <ol class="think-step-list">${rows}</ol>
    </details>`;
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
  if ('speechSynthesis' in window) window.speechSynthesis.cancel(); // don't let a switched-away answer keep talking
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
      models_used: ['qwen2.5:1.5b-instruct'],
      steps: [{ step_number: 1, action: 'call_qwen', model_used: 'qwen2.5:1.5b-instruct', tool_name: null, status: 'ok' }],
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

  // Drag-and-drop attach anywhere over the main pane.
  const mainView = document.getElementById('main-view');
  let dragDepth = 0;
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  mainView.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth++; mainView.classList.add('drag-over');
  });
  mainView.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'copy';
  });
  mainView.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) mainView.classList.remove('drag-over');
  });
  mainView.addEventListener('drop', (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    dragDepth = 0; mainView.classList.remove('drag-over');
    pendingFile = e.dataTransfer.files[0];
    fileInput.value = '';
    syncFilePreview();
    (chatMessages.length ? dockedInput : idleInput).focus();
  });

  function setSendEnabled(on) {
    idleSend.disabled = !on; dockedSend.disabled = !on;
  }

  const KB_EXTS = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.txt', '.md'];
  const IMG_EXTS = ['.png', '.jpg', '.jpeg', '.webp'];
  function fileKind(file) {
    const n = (file.name || '').toLowerCase();
    if ((file.type || '').startsWith('image/') || IMG_EXTS.some((e) => n.endsWith(e))) return 'image';
    if (KB_EXTS.some((e) => n.endsWith(e))) return 'doc';
    return 'other';
  }

  async function handleUpload(file) {
    ensureChat();
    const file_name = file.name;
    const lname = file_name.toLowerCase();
    const file_mime_type = file.type || (lname.endsWith('.pdf') ? 'application/pdf' : null);

    if (!KB_EXTS.some((e) => lname.endsWith(e))) {
      chatMessages.push({ role: 'system', kind: 'upload-error', filename: file_name, message: 'Knowledge Base files must be PDF, Word, PowerPoint, Excel, or text. Attach images directly to a message instead.' });
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

  async function handleMessage(prompt, attachment = null) {
    ensureChat();
    const submitted_at_client = new Date().toISOString();
    if (!currentChatTitle) currentChatTitle = truncate(prompt, 40);

    chatMessages.push({ role: 'user', prompt, file_name: attachment ? attachment.file_name : undefined });
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
        file_base64: attachment ? attachment.file_base64 : null,
        file_mime_type: attachment ? attachment.file_mime_type : null,
        file_name: attachment ? attachment.file_name : null,
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
      if (file && fileKind(file) === 'image') {
        const file_base64 = await fileToBase64(file);
        await handleMessage(prompt || 'Describe this image.', {
          file_base64,
          file_mime_type: file.type || 'image/png',
          file_name: file.name,
        });
      } else {
        if (file) await handleUpload(file);
        if (prompt) await handleMessage(prompt);
      }
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
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
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
  // Always show all three known tools as pill options (dim by default) plus
  // whatever tool the backend actually reported that isn't one of the
  // three — real per-call status now, not an inferred file-result guess.
  const knownTools = [
    { key: 'search_docs', label: TOOL_LABELS.search_docs },
    { key: 'execute_code', label: TOOL_LABELS.execute_code },
    { key: 'generate_file', label: TOOL_LABELS.generate_file },
    { key: 'scan_document', label: TOOL_LABELS.scan_document },
  ];
  const usedToolKeys = new Set(state.toolCalls.map((c) => c.tool_name));
  const toolBranches = knownTools.concat(
    [...usedToolKeys].filter((k) => !knownTools.some((t) => t.key === k)).map((k) => ({ key: k, label: TOOL_LABELS[k] || k }))
  );
  if (!state.toolCalls.length) toolBranches.push({ key: 'na', label: 'No tool call — model answered directly' });

  const routingPills = routeBranches.map((b) => {
    const isActive = state.routingBranches.includes(b.key);
    const isKnown = state.routingBranches.length > 0;
    const cls = isActive ? 'active' : isKnown ? 'dim' : '';
    return `<div class="satellite-pill ${cls}" data-node="route-${b.key}" role="button" tabindex="0"><span class="dot"></span>${b.label}</div>`;
  }).join('');

  const toolPills = toolBranches.map((b) => {
    const isActive = b.key === 'na' ? !state.toolCalls.length && state.stages.tool !== 'pending' : usedToolKeys.has(b.key);
    const isKnown = state.stages.tool !== 'pending';
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
          <div class="branch-line ${state.routingBranches.length ? 'active' : ''}"></div>
          ${routingPills}
        </div>
        ${nodeCard('routing', state.stages.routing)}
      </div>
      ${connector(state.stages.routing)}
      <div class="node-wrap">
        <div class="branch-cluster bottom">
          <div class="branch-line ${state.toolCalls.length ? 'active' : ''}"></div>
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
    const isUsed = state.routingBranches.includes(branch);
    status = isUsed ? 'completed' : state.routingBranches.length ? 'na' : 'pending';
    const modelsOnBranch = state.modelSteps.filter((s) => routeForModel(s.model_used) === branch);
    if (isUsed) {
      desc = state.routingBranches.length > 1
        ? 'One of MULTIPLE models actually used for this task — see the full call order on the Routing node.'
        : 'This is the route the router actually selected for this task.';
      if (modelsOnBranch.length) {
        kv = modelsOnBranch.map((s, i) => ({ k: `Call ${i + 1}`, v: `${s.action === 'call_moondream' ? 'Vision call' : 'Text call'} · ${s.status}` }));
      }
    } else {
      desc = status === 'na' ? 'Not one of the routes taken for this task.' : 'Routing decision not yet resolved.';
    }
  } else if (nodeKey.startsWith('tool-')) {
    const branch = nodeKey.replace('tool-', '');
    const callsOnBranch = state.toolCalls.filter((c) => c.tool_name === branch);
    if (branch === 'na') {
      heading = 'No tool call';
      status = state.stages.tool === 'pending' ? 'pending' : state.toolCalls.length ? 'na' : 'completed';
      desc = status === 'completed'
        ? 'Confirmed from the real execution trace: the model answered directly, with no tool call in this task.'
        : 'A tool was actually called for this task — see the active pill(s) instead.';
    } else {
      heading = TOOL_LABELS[branch] || branch;
      status = callsOnBranch.length ? (callsOnBranch.some((c) => c.status === 'error') ? 'error' : 'completed') : (state.stages.tool === 'pending' ? 'pending' : 'na');
      if (callsOnBranch.length) {
        desc = `Called ${callsOnBranch.length} time${callsOnBranch.length === 1 ? '' : 's'} during this task's execution.`;
        kv = callsOnBranch.map((c, i) => ({ k: `Call ${i + 1}`, v: c.status === 'ok' ? 'Succeeded' : 'Failed' }));
        if (branch === 'generate_file' && t.result && t.result.file_name) kv.push({ k: 'Output File', v: t.result.file_name });
      } else {
        desc = status === 'na' ? 'This tool was not called for this task.' : '';
      }
    }
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
      const models = modelsUsedList(t);
      desc = status === 'pending' ? '' : models.length > 1
        ? `MULTIPLE models were used for this task, in this order — a real multi-step chain, not a single call.`
        : 'Selects which local model handles the request.';
      const tokenUsageLabel = (t.token_usage && typeof t.token_usage.total_tokens === 'number')
        ? `${t.token_usage.total_tokens} tok (${t.token_usage.prompt_tokens ?? 0} in / ${t.token_usage.completion_tokens ?? 0} out) — real, from Ollama`
        : 'Not available for this task';
      if (state.modelSteps.length) {
        // Real per-call trace: exact model + action + status + real per-call
        // token usage (from Ollama's own prompt_eval_count/eval_count).
        kv = state.modelSteps.map((s, i) => ({
          k: `${i + 1}. ${s.action === 'call_moondream' ? 'Vision' : 'Text'} call`,
          v: `${s.model_used} · ${s.status === 'ok' ? 'succeeded' : 'failed'}` +
            ((typeof s.prompt_tokens === 'number' || typeof s.completion_tokens === 'number') ? ` · ${s.prompt_tokens ?? 0}+${s.completion_tokens ?? 0} tok` : ''),
        }));
        kv.push({ k: 'Total Token Usage', v: tokenUsageLabel });
      } else if (models.length) {
        kv = [{ k: 'Model Selected', v: models.join(' → ') }, { k: 'Status', v: 'Resolved' }, { k: 'Total Token Usage', v: tokenUsageLabel }];
      } else if (status === 'active') kv = [{ k: 'Status', v: 'Resolving…' }];
    } else if (nodeKey === 'tool') {
      desc = status === 'na' ? 'Confirmed from the real execution trace: no tool was called for this result.' : status === 'pending' ? '' : `${state.toolCalls.length} tool call${state.toolCalls.length === 1 ? '' : 's'} made during this task, in order.`;
      if (state.toolCalls.length) {
        kv = state.toolCalls.map((c, i) => ({ k: `${i + 1}. ${c.label}`, v: c.status === 'ok' ? 'Succeeded' : 'Failed' }));
        if (t.result && t.result.file_name) kv.push({ k: 'Output File', v: t.result.file_name });
      }
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

/* ============================================================
   "Working" verbs — rotated while a task is in flight so the
   UI reads as alive and a little playful, but stays professional.
   ============================================================ */
const WORKING_PHRASES = [
  'Reading the request', 'Classifying the task', 'Choosing a model',
  'Consulting the knowledge base', 'Reasoning it through', 'Connecting the dots',
  'Drafting a response', 'Running the numbers', 'Checking the sources',
  'Tightening the wording', 'Cross-checking the details', 'Putting it together',
  'Almost there', 'Polishing the answer',
];
let _workingIdx = 0;
let _workingTimer = null;
function startWorkingTicker() {
  if (_workingTimer) return;
  _workingTimer = setInterval(() => {
    const nodes = document.querySelectorAll('[data-working-verb]');
    if (!nodes.length) return;
    _workingIdx = (_workingIdx + 1) % WORKING_PHRASES.length;
    nodes.forEach((n) => { n.textContent = WORKING_PHRASES[_workingIdx] + '…'; });
  }, 1900);
}
function workingLineHtml() {
  const verb = WORKING_PHRASES[_workingIdx] + '…';
  const demo = LIVE_BACKEND ? '' : ' <span style="opacity:.7">(demo simulation)</span>';
  return `<span class="working-line"><span class="spark"></span><span class="verb" data-working-verb>${verb}</span>${demo}</span>`;
}

/* ============================================================
   Minimal, safe Markdown -> HTML for AI answers. Escapes first,
   then re-introduces a limited, known set of tags. Gives results
   real structure (headings, lists, spacing) and pulls fenced code
   into a compact, scrollable box with its own copy button.
   ============================================================ */
function renderRichText(src) {
  const raw = String(src || '').replace(/\r\n/g, '\n');
  const codeBlocks = [];
  // Pull out ```fenced``` code first so its contents are never marked up.
  let text = raw.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const i = codeBlocks.push({ lang: (lang || '').trim(), code: code.replace(/\n$/, '') }) - 1;
    return ` CODE${i} `;
  });

  text = escapeHtml(text);

  const inline = (s) => s
    .replace(/`([^`]+)`/g, '<code class="inline">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const lines = text.split('\n');
  let html = '';
  let listType = null; // 'ul' | 'ol'
  let para = [];
  const flushPara = () => {
    if (!para.length) return;
    html += `<p>${inline(para.join(' '))}</p>`;
    para = [];
  };
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };

  for (const line of lines) {
    const codeMatch = line.match(/^ CODE(\d+) $/);
    if (codeMatch) {
      flushPara(); closeList();
      const b = codeBlocks[Number(codeMatch[1])];
      html += `<div class="code-box"><div class="code-box-head"><span>${escapeHtml(b.lang || 'code')}</span>` +
        `<button class="mini-copy" data-copy-text="${escapeHtml(b.code)}"><iconify-icon icon="lucide:copy" style="font-size:12px"></iconify-icon>Copy</button></div>` +
        `<pre><code>${escapeHtml(b.code)}</code></pre></div>`;
      continue;
    }
    const t = line.trim();
    if (!t) { flushPara(); closeList(); continue; }

    let m;
    if ((m = t.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); closeList();
      const level = m[1].length <= 2 ? 3 : 4;
      html += `<h${level}>${inline(m[2])}</h${level}>`;
    } else if (/^([-*_])\1{2,}$/.test(t)) {
      flushPara(); closeList(); html += '<hr>';
    } else if ((m = t.match(/^[-*•]\s+(.*)$/))) {
      flushPara();
      if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = t.match(/^\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = t.match(/^>\s?(.*)$/))) {
      flushPara(); closeList();
      html += `<blockquote>${inline(m[1])}</blockquote>`;
    } else {
      if (listType) closeList();
      para.push(t);
    }
  }
  flushPara(); closeList();

  // Lead-in emphasis on the opening paragraph for an intro/body/conclusion feel.
  html = html.replace(/^<p>/, '<p class="lead">');
  return html;
}

/** Clean plain-text version of an answer for the "Copy" button. */
function answerToPlainText(src) {
  return String(src || '')
    .replace(/\r\n/g, '\n')
    .replace(/```[\w+-]*\n?([\s\S]*?)```/g, (_, code) => '\n' + code.replace(/\n$/, '') + '\n')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1$2')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*•]\s+/gm, '• ')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '$1 ($2)')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch { return false; }
  }
}

/** Wire up every copy control currently in the conversation DOM. */
function wireCopyButtons() {
  document.querySelectorAll('.copy-answer-btn, .mini-copy').forEach((btn) => {
    if (btn._wired) return;
    btn._wired = true;
    btn.addEventListener('click', async () => {
      const ok = await copyToClipboard(btn.dataset.copyText || '');
      const label = btn.querySelector('.label');
      btn.classList.toggle('copied', ok);
      if (label) { const prev = label.textContent; label.textContent = ok ? 'Copied' : 'Press ⌘C'; setTimeout(() => { label.textContent = prev; btn.classList.remove('copied'); }, 1600); }
      else { setTimeout(() => btn.classList.remove('copied'), 1600); }
      if (!ok) toast('Could not access the clipboard — select and copy manually.', true);
    });
  });
}

/** Text-to-speech for assistant answers — reads the exact plain-text
 *  answer shown in the chat aloud, via the browser's built-in Web Speech
 *  API (window.speechSynthesis). Fully local to the browser, no network
 *  call — consistent with the air-gapped/sovereign story. Only one
 *  utterance plays at a time: starting a new one, or re-clicking the same
 *  button, stops whatever was already speaking. */
let currentUtteranceBtn = null;
function speakText(text, btn) {
  if (!('speechSynthesis' in window)) {
    toast('Text-to-speech is not supported in this browser.', true);
    return;
  }
  const wasThisButton = currentUtteranceBtn === btn;
  window.speechSynthesis.cancel(); // stop whatever was speaking, if anything
  if (currentUtteranceBtn) setSpeakButtonState(currentUtteranceBtn, false);
  currentUtteranceBtn = null;
  if (wasThisButton) return; // clicking the currently-speaking button just stops it

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'en-US';
  utterance.onend = () => { setSpeakButtonState(btn, false); currentUtteranceBtn = null; };
  utterance.onerror = () => { setSpeakButtonState(btn, false); currentUtteranceBtn = null; };
  currentUtteranceBtn = btn;
  setSpeakButtonState(btn, true);
  window.speechSynthesis.speak(utterance);
}
function setSpeakButtonState(btn, speaking) {
  btn.classList.toggle('speaking', speaking);
  const icon = btn.querySelector('iconify-icon');
  const label = btn.querySelector('.label');
  if (icon) icon.setAttribute('icon', speaking ? 'lucide:square' : 'lucide:volume-2');
  if (label) label.textContent = speaking ? 'Stop' : 'Listen';
}

/** Wire up every speaker control currently in the conversation DOM. */
function wireSpeakButtons() {
  document.querySelectorAll('.speak-answer-btn').forEach((btn) => {
    if (btn._wired) return;
    btn._wired = true;
    btn.addEventListener('click', () => speakText(btn.dataset.speakText || '', btn));
  });
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
