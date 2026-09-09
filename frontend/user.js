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
  generate_image: 'Image Generation (SD Turbo)',
  forecast_timeseries: 'Time-Series Forecast (MOMENT-1-small)',
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
  return task.steps.filter((s) =>
    s.action === 'call_qwen' || s.action === 'call_moondream' ||
    // generate_image/forecast_timeseries are call_tool steps under the
    // hood (see app/agent/loop.py's tool_dispatch) but are genuinely
    // backed by their own real model (SD Turbo / MOMENT-1-small) — the
    // backend tags model_used on these two specifically (unlike the other
    // tools), so they belong in the model-call trace too.
    (s.action === 'call_tool' && (s.tool_name === 'generate_image' || s.tool_name === 'forecast_timeseries'))
  );
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
  return {
    text: 'Text · qwen3-1.7b',
    vision: 'Vision · moondream',
    lora: 'Approval-Note LoRA',
    image: 'Image · SD Turbo',
    forecast: 'Forecast · MOMENT-1-small',
  }[branch] || null;
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
  wirePreviewButtons();
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
    // A single request can now name multiple deliverables (e.g. "...word
    // doc on X then excel of Y...") and result.files holds every one, in
    // order — file_url/file_name alone are always just the first, same as
    // a plain single-deliverable result always looked like, so this only
    // needs to branch when there's genuinely more than one to show.
    const files = Array.isArray(result.files) && result.files.length > 1
      ? result.files
      : [{ file_url: result.file_url, file_name: result.file_name }];
    body = files.map((f) => {
      const fn = f.file_name || 'deliverable';
      const url = f.file_url || '#';
      return `
        <div class="deliverable-card">
          <div class="left">
            <div class="file-icon"><iconify-icon icon="${fileTypeIcon(fn)}"></iconify-icon></div>
            <div style="min-width:0">
              <div class="name mono">${escapeHtml(fn)}</div>
              <div class="status-line"><span class="status-dot ok"></span>Ready</div>
            </div>
          </div>
          <div class="actions">
            ${f.file_url ? `<button class="btn-ghost preview-file-btn" data-preview-url="${escapeHtml(f.file_url)}" data-preview-name="${escapeHtml(fn)}">Preview</button>` : ''}
            <a class="btn-ghost" href="${url}" target="_blank" rel="noopener">Open</a>
            <a class="btn-icon-accent" href="${url}" download><iconify-icon icon="lucide:download" style="font-size:16px"></iconify-icon></a>
          </div>
        </div>`;
    }).join('<div style="height:8px"></div>');
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
      const rf = Array.isArray(m.result.files) && m.result.files.length > 1
        ? m.result.files
        : [{ file_name: m.result.file_name, file_url: m.result.file_url }];
      rf.forEach((f) => { if (f.file_name) files.push({ name: f.file_name, url: f.file_url || '#' }); });
    }
    if (m.role === 'system' && m.kind === 'upload' && m.filename) {
      files.push({ name: m.filename, url: null, kb: true });
    }
  });
  const filesListEl = document.getElementById('files-sidebar-list');
  const filesEmptyEl = document.getElementById('files-sidebar-empty');
  filesEmptyEl.hidden = files.length > 0;
  filesListEl.innerHTML = files.map((f) => {
    // Generated deliverables open the in-page preview panel; chat-scoped KB
    // upload chips just mark that a file is attached to this chat.
    if (!f.kb && f.url && f.url !== '#') {
      return `
        <button class="task-sidebar-item preview-file-btn" data-preview-url="${escapeHtml(f.url)}" data-preview-name="${escapeHtml(f.name)}">
          <iconify-icon icon="${fileTypeIcon(f.name)}" class="task-sidebar-item-icon"></iconify-icon>
          <div class="task-sidebar-item-body">
            <div class="task-sidebar-item-title mono">${escapeHtml(truncate(f.name, 24))}</div>
          </div>
        </button>`;
    }
    return `
      <div class="task-sidebar-item" style="cursor:default">
        <iconify-icon icon="${f.kb ? 'lucide:book-marked' : fileTypeIcon(f.name)}" class="task-sidebar-item-icon"></iconify-icon>
        <div class="task-sidebar-item-body">
          <div class="task-sidebar-item-title mono">${escapeHtml(truncate(f.name, 24))}</div>
        </div>
      </div>`;
  }).join('');
  wirePreviewButtons();
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
      model_used: 'qwen3:1.7b',
      models_used: ['qwen3:1.7b'],
      steps: [{ step_number: 1, action: 'call_qwen', model_used: 'qwen3:1.7b', tool_name: null, status: 'ok' }],
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
// One outstanding attachment queue, shared by both composer instances (idle
// centered / docked active) — a File[] rather than a single File so several
// files picked or dropped at once (multi-select in the OS file dialog, or a
// multi-file drag) all show up cleanly before sending, not just the last one.
let pendingFiles = [];

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

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

  function addPendingFiles(fileList) {
    const incoming = Array.from(fileList || []);
    // De-dupe by name+size — picking/dropping the same file twice (a common
    // slip with drag-and-drop) shouldn't queue it twice.
    incoming.forEach((f) => {
      if (!pendingFiles.some((p) => p.name === f.name && p.size === f.size)) pendingFiles.push(f);
    });
    syncFilePreview();
  }

  function removePendingFile(index) {
    pendingFiles.splice(index, 1);
    syncFilePreview();
  }

  function syncFilePreview() {
    [idlePreview, dockedPreview].forEach((el) => {
      if (!pendingFiles.length) { el.classList.remove('show'); el.innerHTML = ''; return; }
      el.classList.add('show');
      el.innerHTML =
        (pendingFiles.length > 1 ? `<div class="file-preview-count">${pendingFiles.length} files attached</div>` : '') +
        pendingFiles.map((f, i) => `
          <div class="file-chip-row${fileKind(f) === 'image' && i === 0 ? ' file-chip-primary' : ''}">
            <iconify-icon icon="${fileTypeIcon(f.name)}"></iconify-icon>
            <span class="name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
            <span class="size">${formatFileSize(f.size)}</span>
            <button type="button" class="remove-file-btn" data-remove-file="${i}" title="Remove"><iconify-icon icon="lucide:x"></iconify-icon></button>
          </div>`).join('');
      el.querySelectorAll('[data-remove-file]').forEach((btn) => {
        btn.addEventListener('click', () => removePendingFile(Number(btn.dataset.removeFile)));
      });
    });
  }
  [idleAttach, dockedAttach].forEach((btn) => btn.addEventListener('click', () => fileInput.click()));
  fileInput.addEventListener('change', () => {
    addPendingFiles(fileInput.files);
    fileInput.value = '';
  });

  // Drag-and-drop attach anywhere over the main pane — accepts multiple
  // files dropped at once, same as the file picker.
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
    addPendingFiles(e.dataTransfer.files);
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
    const files = pendingFiles.slice();
    if (!prompt && !files.length) return;

    setSendEnabled(false);
    idleInput.value = ''; dockedInput.value = '';
    fileInput.value = '';
    pendingFiles = [];
    syncFilePreview();

    // The backend contract sends ONE optional file per message (an image
    // for the vision model). Several documents can still be queued
    // together, though — those go to the Knowledge Base one at a time
    // (handleUpload already supports that), not into the message itself.
    // Only the FIRST image in the queue can ride along on this message;
    // say so plainly instead of silently dropping the rest.
    const images = files.filter((f) => fileKind(f) === 'image');
    const docs = files.filter((f) => fileKind(f) !== 'image');
    const primaryImage = images[0] || null;
    if (images.length > 1) {
      toast(`Only the first image (${images[0].name}) is attached to this message — the vision model takes one image per message. The rest were not sent.`, true);
    }

    try {
      for (const doc of docs) await handleUpload(doc);
      if (primaryImage) {
        const file_base64 = await fileToBase64(primaryImage);
        await handleMessage(prompt || 'Describe this image.', {
          file_base64,
          file_mime_type: primaryImage.type || 'image/png',
          file_name: primaryImage.name,
        });
      } else if (prompt) {
        await handleMessage(prompt);
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
  NodePopover.hide(true);
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
    { key: 'text', label: 'Text → qwen3-1.7b' },
    { key: 'vision', label: 'Vision → moondream' },
    { key: 'lora', label: 'Approval-Note LoRA' },
    { key: 'image', label: 'Image → SD Turbo' },
    { key: 'forecast', label: 'Forecast → MOMENT-1-small' },
  ];
  // Always show all three known tools as pill options (dim by default) plus
  // whatever tool the backend actually reported that isn't one of the
  // three — real per-call status now, not an inferred file-result guess.
  const knownTools = [
    { key: 'search_docs', label: TOOL_LABELS.search_docs },
    { key: 'execute_code', label: TOOL_LABELS.execute_code },
    { key: 'generate_file', label: TOOL_LABELS.generate_file },
    { key: 'scan_document', label: TOOL_LABELS.scan_document },
    { key: 'generate_image', label: TOOL_LABELS.generate_image },
    { key: 'forecast_timeseries', label: TOOL_LABELS.forecast_timeseries },
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
    const key = el.dataset.node;
    el.addEventListener('mouseenter', () => NodePopover.peek(key, state, el));
    el.addEventListener('mouseleave', () => NodePopover.unpeek(el));
    el.addEventListener('click', (e) => { e.stopPropagation(); NodePopover.togglePin(key, state, el); });
    el.addEventListener('focus', () => NodePopover.peek(key, state, el));
    el.addEventListener('blur', () => NodePopover.unpeek(el));
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); NodePopover.togglePin(key, state, el); }
    });
  });

  // Keep a pinned/hovered popover pointed at the freshly rendered node.
  NodePopover.reanchor(state);
}

/* ============================================================
   Node inspector — a compact contextual popover anchored to the
   selected execution-graph node. Hover peeks, click pins, click
   away / Escape / closing the graph dismisses it. It is NOT a
   side panel: it stays inside the graph overlay, repositions to
   the side with the most room, and clamps to the viewport so it
   never covers the graph path.

   Every field is derived only from what the backend actually
   reports for this task — prompt, status, the real per-step
   execution trace (task.steps -> modelSteps / toolCalls /
   routingBranches), result.type/text/file_name/file_url/sources,
   error, timing. Anything not in the trace is omitted or shown
   as a neutral "Not available" — never invented, never a guess.
   ============================================================ */

function nodeStatusMeta(s) {
  switch (s) {
    case 'active': return { label: 'ACTIVE', cls: 'active' };
    case 'completed': return { label: 'COMPLETED', cls: 'completed' };
    case 'error': return { label: 'FAILED', cls: 'error' };
    case 'na': return { label: 'NOT USED', cls: 'na' };
    default: return { label: 'QUEUED', cls: 'pending' };
  }
}

function modelModality(model) {
  if (!model) return null;
  const m = String(model).toLowerCase();
  if (m.includes('moondream')) return 'Vision · image understanding';
  if (m.includes('lora')) return 'Text · fine-tuned approval-note adapter';
  if (m.includes('qwen')) return 'Text · instruction-tuned language model';
  if (m.includes('sd-turbo') || m.includes('stable-diffusion')) return 'Image · text-to-image generation';
  if (m.includes('moment')) return 'Time-series · forecasting foundation model';
  return 'Local model';
}

function fileExtLabel(name) {
  const ext = (name || '').split('.').pop();
  return ext && ext !== name ? ext.toUpperCase() : 'Not available';
}

function outputSummary(t) {
  const r = (t && t.result) || {};
  if (r.type === 'file') return r.file_name || 'file generated';
  if (r.text) return truncate(r.text.replace(/\s+/g, ' ').trim(), 160);
  return null;
}

/**
 * Build the popover model for one node:
 *   { eyebrow, title, status:{label,cls}, rows:[{k,v,mono}], actions:[...], note }
 * A row whose value is null/'' renders as a muted "Not available".
 */
function buildNodePopover(nodeKey, state) {
  const t = currentTask || {};
  const r = t.result || {};
  const rows = [];
  const actions = [];
  let eyebrow = 'Stage', title = nodeKey, statusStr = 'pending', note = '';

  const fileActions = () => {
    if (r.type === 'file' && r.file_url) {
      actions.push({ label: 'Open', href: r.file_url, icon: 'lucide:external-link' });
      actions.push({ label: 'Download', href: r.file_url, download: true, icon: 'lucide:download' });
    }
  };

  if (nodeKey.startsWith('route-')) {
    // MODEL node — one route pill. Uses the real multi-model trace.
    const branch = nodeKey.replace('route-', '');
    const model = {
      text: 'qwen3:1.7b', vision: 'moondream', lora: 'approval-note-lora',
      image: 'sd-turbo', forecast: 'moment-1-small',
    }[branch] || branch;
    const isUsed = state.routingBranches.includes(branch);
    const modelsOnBranch = state.modelSteps.filter((s) => routeForModel(s.model_used) === branch);
    eyebrow = 'Model';
    title = routeLabelFor(branch) || branch;
    statusStr = isUsed
      ? (t.status === 'failed' ? 'error' : t.status === 'completed' ? 'completed' : 'active')
      : (state.routingBranches.length ? 'na' : 'pending');
    rows.push({ k: 'Model', v: model, mono: true });
    rows.push({ k: 'Modality', v: modelModality(model) });
    if (isUsed && modelsOnBranch.length) {
      modelsOnBranch.forEach((s, i) => rows.push({
        k: `Call ${i + 1}`,
        v: `${s.action === 'call_moondream' ? 'Vision' : 'Text'} · ${s.status === 'ok' ? 'succeeded' : 'failed'}`,
      }));
    }
    if (isUsed && state.routingBranches.length > 1) {
      note = 'One of multiple models used for this task — full call order is on the Routing node.';
    } else if (statusStr === 'na') {
      note = 'Not a route taken for this task.';
    }
    if (isUsed && statusStr === 'completed') { rows.push({ k: 'Output', v: outputSummary(t) }); fileActions(); }
  } else if (nodeKey.startsWith('tool-')) {
    // TOOL / KNOWLEDGE / CODE-EXECUTION / FILE-GENERATION node — one tool pill.
    const branch = nodeKey.replace('tool-', '');
    const calls = state.toolCalls.filter((c) => c.tool_name === branch);
    eyebrow = 'Knowledge / Tool';
    if (branch === 'na') {
      title = 'No Tool Call';
      statusStr = state.stages.tool === 'pending' ? 'pending' : state.toolCalls.length ? 'na' : 'completed';
      note = statusStr === 'completed'
        ? 'Confirmed from the execution trace: the model answered directly, with no tool call.'
        : 'A tool was called for this task — see the active pill(s).';
    } else {
      title = TOOL_LABELS[branch] || branch;
      statusStr = calls.length
        ? (calls.some((c) => c.status === 'error') ? 'error' : 'completed')
        : (state.stages.tool === 'pending' ? 'pending' : 'na');
      if (calls.length) {
        calls.forEach((c, i) => rows.push({ k: `Call ${i + 1}`, v: c.status === 'ok' ? 'Succeeded' : 'Failed' }));
        if (branch === 'generate_file' && r.file_name) {
          rows.push({ k: 'File type', v: fileExtLabel(r.file_name) });
          rows.push({ k: 'Filename', v: r.file_name, mono: true });
          fileActions();
        }
        if (branch === 'search_docs' && Array.isArray(r.sources) && r.sources.length) {
          rows.push({ k: 'Query', v: t.prompt ? truncate(t.prompt, 120) : null });
          rows.push({ k: 'Results', v: String(r.sources.length) });
          rows.push({ k: 'Sources', v: r.sources.slice(0, 4).map((s) => s.filename + (s.page != null ? ` p.${s.page}` : '')).join(', '), mono: true });
        }
      } else {
        note = statusStr === 'na' ? 'This tool was not called for this task.' : '';
      }
    }
  } else {
    statusStr = state.stages[nodeKey] || 'pending';

    if (nodeKey === 'task') {
      eyebrow = 'Task';
      title = 'Request';
      statusStr = t.status === 'completed' ? 'completed' : t.status === 'failed' ? 'error' : (t.status ? 'active' : 'pending');
      rows.push({ k: 'Prompt', v: t.prompt ? truncate(t.prompt, 220) : null });
      rows.push({ k: 'Status', v: (t.status || 'queued').toUpperCase() });
      if (t.file_name) rows.push({ k: 'Input file', v: t.file_name, mono: true });
      rows.push({ k: 'Submitted', v: t.submitted_at_client ? new Date(t.submitted_at_client).toLocaleTimeString() : null });
      rows.push({ k: 'Task ID', v: t.task_id ? t.task_id.slice(0, 8) : 'pending', mono: true });
    } else if (nodeKey === 'classification') {
      eyebrow = 'Classification';
      title = 'Task-Type Detection';
      rows.push({ k: 'Status', v: statusStr === 'active' ? 'In progress' : statusStr === 'completed' ? 'Resolved' : 'Queued' });
      if (statusStr === 'completed') {
        rows.push({ k: 'Detected route', v: state.routingBranches.map((b) => routeLabelFor(b)).filter(Boolean).join(' → ') || null });
        rows.push({ k: 'Result kind', v: r.type || null });
      }
      note = 'Task-type is reconciled from the finished result — the status API does not expose it mid-run.';
    } else if (nodeKey === 'routing') {
      eyebrow = 'Routing';
      title = 'Model & Compute Logic';
      const models = modelsUsedList(t);
      // Real per-call and total token usage (from Ollama's own
      // prompt_eval_count/eval_count) — replaces the old "not exposed"
      // placeholder now that app/agent/state.py's token_totals is wired
      // all the way through to the task-status response.
      const tokenUsageLabel = (t.token_usage && typeof t.token_usage.total_tokens === 'number')
        ? `${t.token_usage.total_tokens} tok (${t.token_usage.prompt_tokens ?? 0} in / ${t.token_usage.completion_tokens ?? 0} out) — real, from Ollama`
        : 'Not available for this task';
      if (state.modelSteps.length) {
        state.modelSteps.forEach((s, i) => rows.push({
          k: `${i + 1}. ${s.action === 'call_moondream' ? 'Vision' : 'Text'} call`,
          v: `${s.model_used} · ${s.status === 'ok' ? 'succeeded' : 'failed'}` +
            ((typeof s.prompt_tokens === 'number' || typeof s.completion_tokens === 'number') ? ` · ${s.prompt_tokens ?? 0}+${s.completion_tokens ?? 0} tok` : ''),
          mono: true,
        }));
        if (models.some((m) => String(m).toLowerCase().includes('lora'))) rows.push({ k: 'LoRA adapter', v: 'approval-note-lora (in use)' });
        rows.push({ k: 'Total token usage', v: tokenUsageLabel });
        if (models.length > 1) note = 'Multiple models were chained for this task, in the order shown — a real multi-step chain.';
      } else if (models.length) {
        rows.push({ k: 'Selected model', v: models.join(' → '), mono: true });
        rows.push({ k: 'Model type', v: modelModality(models[0]) });
        rows.push({ k: 'Total token usage', v: tokenUsageLabel });
      } else {
        rows.push({ k: 'Selected model', v: statusStr === 'active' ? 'Resolving…' : null });
        rows.push({ k: 'Total token usage', v: tokenUsageLabel });
      }
    } else if (nodeKey === 'tool') {
      eyebrow = 'Knowledge / Tool';
      title = 'Tool & Knowledge Activity';
      if (state.toolCalls.length) {
        state.toolCalls.forEach((c, i) => rows.push({ k: `${i + 1}. ${c.label}`, v: c.status === 'ok' ? 'Succeeded' : 'Failed' }));
        if (r.file_name) { rows.push({ k: 'Output file', v: r.file_name, mono: true }); fileActions(); }
        if (Array.isArray(r.sources) && r.sources.length) {
          rows.push({ k: 'Retrieved', v: `${r.sources.length} passage${r.sources.length === 1 ? '' : 's'}` });
          rows.push({ k: 'Sources', v: r.sources.slice(0, 4).map((s) => s.filename + (s.page != null ? ` p.${s.page}` : '')).join(', '), mono: true });
        }
      } else {
        statusStr = state.stages.tool;
        note = statusStr === 'na'
          ? 'Confirmed from the execution trace: no tool was called for this result.'
          : 'No tool or knowledge-base call has been recorded yet.';
      }
    } else if (nodeKey === 'validation') {
      eyebrow = 'Validation';
      title = 'Result Validation';
      rows.push({ k: 'Outcome', v: statusStr === 'error' ? 'Returned an error' : statusStr === 'completed' ? 'No error reported' : null });
      if (t.error) rows.push({ k: 'Error', v: truncate(t.error, 200) });
    } else if (nodeKey === 'deliverable') {
      eyebrow = 'Result';
      title = 'Deliverable';
      rows.push({ k: 'Result type', v: r.type || null });
      rows.push({ k: 'Completion', v: (t.status || '').toUpperCase() || null });
      if (statusStr === 'completed') {
        rows.push({ k: 'Summary', v: outputSummary(t) });
        rows.push({ k: 'Duration', v: (t.started_at && t.completed_at) ? timeAgoOrDuration(t.started_at, t.completed_at) : null });
        fileActions();
      } else if (statusStr === 'error') {
        note = 'No deliverable — the task failed.';
      }
    } else {
      title = (NODE_META[nodeKey] && NODE_META[nodeKey].title()) || nodeKey;
    }
  }

  return { eyebrow, title, status: nodeStatusMeta(statusStr), rows, actions, note };
}

const NodePopover = {
  el: null,
  pinnedKey: null,
  anchorEl: null,
  hoverKey: null,

  _root() { return this.el || (this.el = document.getElementById('node-popover')); },

  render(nodeKey, state) {
    const m = buildNodePopover(nodeKey, state);
    const pop = this._root();
    const rowsHtml = m.rows.map((row) => {
      const val = (row.v === null || row.v === undefined || row.v === '')
        ? '<span class="np-na">Not available</span>'
        : `<span class="np-v${row.mono ? ' mono' : ''}">${escapeHtml(String(row.v))}</span>`;
      return `<div class="np-row"><span class="np-k">${escapeHtml(row.k)}</span>${val}</div>`;
    }).join('');
    const actionsHtml = m.actions.length
      ? `<div class="np-actions">${m.actions.map((a) =>
          `<a class="np-btn" href="${a.href}" ${a.download ? 'download' : 'target="_blank" rel="noopener"'}>` +
          `<iconify-icon icon="${a.icon}" style="font-size:13px"></iconify-icon>${escapeHtml(a.label)}</a>`).join('')}</div>`
      : '';
    pop.className = `node-popover ${m.status.cls}${this.pinnedKey === nodeKey ? ' pinned' : ''}`;
    pop.innerHTML = `
      <div class="np-head">
        <div>
          <div class="np-eyebrow">${escapeHtml(m.eyebrow)}</div>
          <div class="np-title">${escapeHtml(m.title)}</div>
        </div>
        <span class="status-pill ${m.status.cls}">${m.status.label}</span>
      </div>
      ${rowsHtml ? `<div class="np-body">${rowsHtml}</div>` : ''}
      ${m.note ? `<p class="np-note">${escapeHtml(m.note)}</p>` : ''}
      ${actionsHtml}
      ${this.pinnedKey === nodeKey ? '<div class="np-pinned-hint"><iconify-icon icon="lucide:pin" style="font-size:11px"></iconify-icon>Pinned · click the node again or press Esc to close</div>' : ''}
    `;
    pop.hidden = false;
    void pop.offsetWidth; // force reflow so the enter transition always runs
    pop.classList.add('visible');
  },

  position(anchorEl) {
    const pop = this._root();
    if (!anchorEl) return;
    const a = anchorEl.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    const gap = 12, margin = 10;
    const vw = window.innerWidth, vh = window.innerHeight;
    let left, top, place;
    const spaceRight = vw - a.right, spaceLeft = a.left;
    if (spaceRight >= pw + gap + margin) { left = a.right + gap; place = 'right'; }
    else if (spaceLeft >= pw + gap + margin) { left = a.left - gap - pw; place = 'left'; }
    else { left = Math.min(Math.max(margin, a.left + a.width / 2 - pw / 2), vw - pw - margin); place = (a.top > vh / 2) ? 'top' : 'bottom'; }

    if (place === 'right' || place === 'left') top = a.top + a.height / 2 - ph / 2;
    else if (place === 'bottom') top = a.bottom + gap;
    else top = a.top - gap - ph;
    top = Math.min(Math.max(margin, top), vh - ph - margin);
    left = Math.min(Math.max(margin, left), vw - pw - margin);
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
    pop.dataset.place = place;
  },

  peek(nodeKey, state, anchorEl) {
    if (this.pinnedKey) return;
    this.hoverKey = nodeKey;
    this.anchorEl = anchorEl;
    this.render(nodeKey, state);
    this.position(anchorEl);
  },

  unpeek(anchorEl) {
    if (this.pinnedKey) return;
    if (anchorEl && anchorEl !== this.anchorEl) return;
    this.hoverKey = null;
    this.hide();
  },

  togglePin(nodeKey, state, anchorEl) {
    if (this.pinnedKey === nodeKey) { this.hide(true); return; }
    this.pinnedKey = nodeKey;
    this.anchorEl = anchorEl;
    this.render(nodeKey, state);
    this.position(anchorEl);
  },

  reanchor(state) {
    const key = this.pinnedKey || this.hoverKey;
    if (!key) return;
    const el = document.querySelector(`#graph-canvas [data-node="${key}"]`);
    if (!el) { this.hide(true); return; }
    this.anchorEl = el;
    this.render(key, state);
    this.position(el);
  },

  hide(force) {
    if (force) this.pinnedKey = null;
    if (this.pinnedKey) return;
    const pop = this._root();
    pop.classList.remove('visible');
    this.anchorEl = null;
    setTimeout(() => { if (!pop.classList.contains('visible')) pop.hidden = true; }, 160);
  },

  isOpen() { return !!this.pinnedKey || !!this.hoverKey; },
};

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
   Voice input — speech-to-text via the browser's own Web Speech API
   (window.SpeechRecognition / webkitSpeechRecognition). Fully local to
   the browser, no network call — consistent with the air-gapped story,
   same reasoning as the existing text-to-speech "Listen" feature. Opens
   a large, clear microphone overlay; the finalized transcript is dropped
   straight into whichever composer input opened it. Degrades invisibly
   (mic buttons hidden entirely) on a browser without support.
   ============================================================ */
const VoiceInput = {
  recognition: null,
  targetInput: null,

  supported() {
    return 'SpeechRecognition' in window || 'webkitSpeechRecognition' in window;
  },

  init() {
    const micIdle = document.getElementById('composer-mic-idle');
    const micDocked = document.getElementById('composer-mic-docked');
    const overlay = document.getElementById('mic-overlay');
    const closeBtn = document.getElementById('mic-close');
    const statusEl = document.getElementById('mic-status');
    const transcriptEl = document.getElementById('mic-transcript');
    if (!overlay) return;

    if (!this.supported()) {
      // No speech API on this browser — hide the mic buttons entirely
      // rather than showing a control that can only ever fail.
      [micIdle, micDocked].forEach((b) => { if (b) b.hidden = true; });
      return;
    }

    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    this.recognition = new SpeechRecognitionCtor();
    this.recognition.continuous = false;
    this.recognition.interimResults = true;
    this.recognition.lang = 'en-US';

    const open = (input) => {
      this.targetInput = input;
      overlay.hidden = false;
      overlay.classList.add('listening');
      statusEl.textContent = 'Listening…';
      transcriptEl.textContent = '';
      try { this.recognition.start(); } catch { /* already running — ignore */ }
    };
    const close = () => {
      overlay.classList.remove('listening');
      overlay.hidden = true;
      try { this.recognition.stop(); } catch { /* noop */ }
    };

    this.recognition.onresult = (e) => {
      let finalText = '', interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const chunk = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += chunk; else interim += chunk;
      }
      transcriptEl.textContent = (finalText + interim).trim();
      if (finalText.trim()) {
        const input = this.targetInput;
        if (input) {
          input.value = (input.value ? input.value.trim() + ' ' : '') + finalText.trim();
          input.focus();
        }
        close();
      }
    };
    this.recognition.onerror = (e) => {
      overlay.classList.remove('listening');
      statusEl.textContent = e.error === 'not-allowed'
        ? 'Microphone access was blocked — allow it in your browser settings.'
        : "Didn't catch that — try again.";
    };
    this.recognition.onend = () => {
      overlay.classList.remove('listening');
    };

    if (micIdle) micIdle.addEventListener('click', () => open(document.getElementById('composer-input-idle')));
    if (micDocked) micDocked.addEventListener('click', () => open(document.getElementById('composer-input')));
    closeBtn.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !overlay.hidden) close(); });
  },
};

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

/**
 * Disco mode — a small toggle next to the theme switch that spins the same
 * atmospheric wash GradientPicker already offers as fixed presets through
 * every hue in a fast, continuous loop instead of picking just one. Purely
 * decorative; reuses GradientPicker.apply() itself (same CSS vars, same
 * light/dark-aware color math) rather than a parallel implementation, so
 * it can never drift out of sync with what the swatches actually produce.
 * Toggling off restores whatever gradient the user had actually chosen.
 */
const DiscoMode = {
  active: false,
  timer: null,
  hue: 0,
  STEP_DEG: 14,
  INTERVAL_MS: 90,

  start() {
    if (this.active) return;
    this.active = true;
    document.querySelectorAll('[data-disco-toggle]').forEach((b) => b.classList.add('spinning'));
    this.timer = setInterval(() => {
      this.hue = (this.hue + this.STEP_DEG) % 360;
      GradientPicker.apply(this.hue);
    }, this.INTERVAL_MS);
  },
  stop() {
    if (!this.active) return;
    this.active = false;
    clearInterval(this.timer);
    document.querySelectorAll('[data-disco-toggle]').forEach((b) => b.classList.remove('spinning'));
    // Restore whatever gradient choice the user actually saved, rather
    // than freezing on whatever hue disco mode happened to land on.
    const stored = localStorage.getItem(GradientPicker.KEY);
    GradientPicker.apply(stored === null ? null : Number(stored));
  },
  toggle() { this.active ? this.stop() : this.start(); },
  init() {
    document.querySelectorAll('[data-disco-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => this.toggle());
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

/* ============================================================
   Task template library — one-click prompt starters for
   industrial knowledge work. A template only FILLS the composer
   (it never auto-sends), so the operator can attach the relevant
   files / paste a transcript / edit wording first. Each prompt is
   written to run through the existing agent + tools + KB +
   file-generation flow — nothing here is a hardcoded answer path
   and nothing asks for a capability the local models don't have.
   ============================================================ */
const TASK_TEMPLATES = [
  { id: 'exec-summary', label: 'Executive Summary', icon: 'lucide:file-text', group: 'Summarize & extract', needs: 'doc', featured: true,
    prompt: 'Produce an executive summary of the attached document. Use these sections: Purpose, Key Points (bullets), Decisions or Findings, Risks / Open Issues, Recommended Next Steps. Keep it under 200 words and use only information present in the document.' },
  { id: 'tech-findings', label: 'Extract Technical Findings', icon: 'lucide:search-check', group: 'Summarize & extract', needs: 'doc', featured: true,
    prompt: 'From the attached document, extract every technical finding as a numbered list. For each finding give: Finding, Location / Equipment, Severity (as stated, or "not stated"), Evidence / Measurement, Recommended action. Do not infer values that are not written in the document.' },
  { id: 'action-items', label: 'Extract Action Items', icon: 'lucide:list-checks', group: 'Summarize & extract', needs: 'doc',
    prompt: 'From the attached document or pasted transcript, extract every action item as a table with columns: Owner | Action | Due date | Source line. Then list any implied-but-unassigned actions separately. Use only what the text supports.' },

  { id: 'review-inspection', label: 'Review Inspection Report', icon: 'lucide:clipboard-check', group: 'Review & verify', needs: 'doc', featured: true,
    prompt: 'Review the attached inspection report against standard inspection-reporting practice. Report: missing mandatory sections; each finding and its priority classification; whether a formal Approval Note is required and to what approval level; and the Next Inspection Due date. Ground every point in the report and the inspection-reporting manual in the knowledge base.' },
  { id: 'validate-code', label: 'Validate / Verify Code', icon: 'lucide:code-2', group: 'Review & verify', needs: 'paste',
    prompt: 'Review the following code for correctness, edge cases, and safety, then run it in the sandbox and report the actual output. Sections: Summary, Issues Found (severity-ranked), Suggested Fix, Execution Result.\n\n```python\n# paste the code here\n```' },
  { id: 'check-calc', label: 'Check Calculation', icon: 'lucide:calculator', group: 'Review & verify', needs: 'paste',
    prompt: 'Verify this calculation by computing it independently in the sandbox. Show: Restated inputs, Method, Computed result, Whether it matches the stated result, and any discrepancy.\n\nCalculation to check: ' },
  { id: 'find-inconsistencies', label: 'Find Inconsistencies', icon: 'lucide:git-compare-arrows', group: 'Review & verify', needs: 'doc',
    prompt: 'Identify internal inconsistencies, contradictions, or values that do not add up in the attached document(s). For each: what conflicts, where it appears, and which value is more likely correct. If none, say so explicitly.' },

  { id: 'compare-docs', label: 'Compare Documents', icon: 'lucide:columns-2', group: 'Compare & analyze', needs: 'docs2', featured: true,
    prompt: 'Compare the attached documents. Report: 1) Changed values / clauses (old -> new), 2) Additions, 3) Removals, 4) Contradictions or inconsistencies, 5) Possible operational / business impact, 6) Recommended follow-up actions. Use only content present in the documents and name which document each point comes from.' },
  { id: 'assess-impact', label: 'Assess Possible Impact', icon: 'lucide:activity', group: 'Compare & analyze', needs: 'doc',
    prompt: 'Assess the possible operational, safety, and business impact of the issue described in the attached document / my message. Sections: Situation, Direct impact, Downstream / secondary impact, Worst case, Mitigations. State every assumption explicitly.' },
  { id: 'recommend-actions', label: 'Recommend Next Actions', icon: 'lucide:list-todo', group: 'Compare & analyze', needs: 'doc',
    prompt: 'Based on the attached document, give a prioritized checklist of recommended next actions. For each: action, owner / role (as stated or "unassigned"), rationale, and urgency. Use only what the document supports.' },
  { id: 'analyze-proposal', label: 'Analyze Vendor / Technical Proposal', icon: 'lucide:file-search', group: 'Compare & analyze', needs: 'doc',
    prompt: 'Analyze the attached vendor / technical proposal. Sections: Scope offered, Commercial terms, Technical strengths, Gaps or risks, Compliance with our requirements, Clarifications to request, Overall recommendation. Ground each point in the proposal text.' },

  { id: 'meeting-minutes', label: 'Prepare Meeting Minutes', icon: 'lucide:notebook-pen', group: 'Draft & minutes', needs: 'transcript', featured: true,
    prompt: 'Turn the attached or pasted meeting transcript into formal minutes. Sections: Attendees (if stated), Agenda, Discussion summary, Decisions, Action Items (owner - task - due date), Unresolved Issues. Use only the transcript — do not add commitments, owners, or dates that are not in it.\n\nTranscript:\n' },
  { id: 'draft-approval-note', label: 'Draft Approval Note', icon: 'lucide:stamp', group: 'Draft & minutes', needs: 'doc', featured: true,
    prompt: 'Draft a formal Approval Note as a Word document for the finding described in my message / the attached report. Use the standard structure: Subject, Reference, Findings, Recommendation, Required Approval Level, Approval Status.\n\nFinding: ' },
];

const TemplateLibrary = {
  FEATURED_LIMIT: 4,

  chipHtml(tpl) {
    return `<button class="template-chip" data-template="${tpl.id}" title="${escapeHtml(tpl.prompt.split('\n')[0])}">` +
      `<iconify-icon icon="${tpl.icon}"></iconify-icon><span>${escapeHtml(tpl.label)}</span></button>`;
  },

  renderBars() {
    const featured = TASK_TEMPLATES.filter((t) => t.featured).slice(0, this.FEATURED_LIMIT);
    const moreBtn = `<button class="template-chip template-chip--more" data-template-more><iconify-icon icon="lucide:layout-grid"></iconify-icon><span>More</span></button>`;
    const html = featured.map((t) => this.chipHtml(t)).join('') + moreBtn;
    ['template-bar-idle', 'template-bar-docked'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = html;
    });
    this.wireBar();
  },

  renderSheet() {
    const body = document.getElementById('template-sheet-body');
    if (!body) return;
    const groups = [];
    TASK_TEMPLATES.forEach((t) => {
      let g = groups.find((x) => x.name === t.group);
      if (!g) { g = { name: t.group, items: [] }; groups.push(g); }
      g.items.push(t);
    });
    const needLabel = { doc: 'attach a document', docs2: 'attach 2+ documents', transcript: 'attach / paste a transcript', paste: 'paste code or numbers', none: '' };
    body.innerHTML = groups.map((g) => `
      <div class="template-group">
        <div class="template-group-label">${escapeHtml(g.name)}</div>
        <div class="template-group-grid">
          ${g.items.map((t) => `
            <button class="template-card" data-template="${t.id}">
              <span class="template-card-icon"><iconify-icon icon="${t.icon}"></iconify-icon></span>
              <span class="template-card-body">
                <span class="template-card-title">${escapeHtml(t.label)}</span>
                <span class="template-card-hint">${escapeHtml(t.prompt.replace(/\n+/g, ' ').trim().slice(0, 110))}…</span>
                ${needLabel[t.needs] ? `<span class="template-card-need"><iconify-icon icon="lucide:paperclip" style="font-size:10px"></iconify-icon>${needLabel[t.needs]}</span>` : ''}
              </span>
            </button>`).join('')}
        </div>
      </div>`).join('');
    body.querySelectorAll('[data-template]').forEach((el) => {
      el.addEventListener('click', () => { this.apply(el.dataset.template); this.closeSheet(); });
    });
  },

  wireBar() {
    document.querySelectorAll('[data-template]').forEach((el) => {
      if (el.closest('#template-sheet-body') || el._wired) return;
      el._wired = true;
      el.addEventListener('click', () => this.apply(el.dataset.template));
    });
    document.querySelectorAll('[data-template-more]').forEach((el) => {
      if (el._wired) return;
      el._wired = true;
      el.addEventListener('click', () => this.openSheet());
    });
  },

  apply(id) {
    const tpl = TASK_TEMPLATES.find((t) => t.id === id);
    if (!tpl) return;
    const active = chatMessages.length > 0;
    const input = document.getElementById(active ? 'composer-input' : 'composer-input-idle');
    if (!input) return;
    input.value = tpl.prompt;
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch { /* noop */ }
    const hint = { doc: 'Attach the document, then send.', docs2: 'Attach two or more documents, then send.', transcript: 'Paste the transcript into the message (or attach it), then send.', paste: 'Paste your code / numbers into the message, then send.' }[tpl.needs];
    if (hint) toast(hint);
  },

  openSheet() {
    this.renderSheet();
    const ov = document.getElementById('template-overlay');
    if (ov) ov.hidden = false;
  },
  closeSheet() {
    const ov = document.getElementById('template-overlay');
    if (ov) ov.hidden = true;
  },

  init() {
    this.renderBars();
    const close = document.getElementById('template-close');
    if (close) close.addEventListener('click', () => this.closeSheet());
    const ov = document.getElementById('template-overlay');
    if (ov) ov.addEventListener('click', (e) => { if (e.target === ov) this.closeSheet(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && ov && !ov.hidden) this.closeSheet();
    });
  },
};

/* ============================================================
   Right-side document preview panel — opens in-page (like
   Claude's preview) when a generated deliverable or a Knowledge
   Base file is clicked. PDFs embed the file directly; Word /
   Excel / PowerPoint / text are rendered to HTML server-side
   (fully offline) by /api/preview/* and shown here.
   ============================================================ */
function absUrl(u) {
  if (!u) return '';
  if (/^https?:/i.test(u)) return u;
  if (u.startsWith('/')) return (typeof API_BASE === 'string' ? API_BASE : '') + u;
  return u;
}

const PreviewPanel = {
  currentKey: null,

  els() {
    return {
      panel: document.getElementById('preview-panel'),
      backdrop: document.getElementById('preview-backdrop'),
      body: document.getElementById('preview-panel-body'),
      name: document.getElementById('preview-panel-name'),
      icon: document.getElementById('preview-panel-icon'),
      openTab: document.getElementById('preview-open-tab'),
      download: document.getElementById('preview-download'),
      closeBtn: document.getElementById('preview-close'),
    };
  },

  init() {
    const { backdrop, closeBtn } = this.els();
    if (closeBtn) closeBtn.addEventListener('click', () => this.close());
    if (backdrop) backdrop.addEventListener('click', () => this.close());
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.isOpen()) { e.stopPropagation(); this.close(); }
    });
  },

  isOpen() {
    const { panel } = this.els();
    return !!panel && !panel.hidden;
  },

  _show(name) {
    const { panel, backdrop, icon, name: nameEl } = this.els();
    if (!panel) return;
    panel.hidden = false;
    backdrop.hidden = false;
    void panel.offsetWidth; // force reflow so the slide-in transition runs
    panel.classList.add('open');
    backdrop.classList.add('open');
    document.body.classList.add('preview-open');
    if (nameEl) nameEl.textContent = name || 'Preview';
    if (icon) icon.setAttribute('icon', fileTypeIcon(name || ''));
  },

  close() {
    const { panel, backdrop, body } = this.els();
    this.currentKey = null;
    if (panel) panel.classList.remove('open');
    if (backdrop) backdrop.classList.remove('open');
    document.body.classList.remove('preview-open');
    setTimeout(() => {
      if (panel && !panel.classList.contains('open')) panel.hidden = true;
      if (backdrop && !backdrop.classList.contains('open')) backdrop.hidden = true;
      if (body && !PreviewPanel.currentKey) body.innerHTML = '';
    }, 220);
  },

  _loading() {
    const { body } = this.els();
    if (body) body.innerHTML = `<div class="preview-state"><span class="spark"></span>Loading preview…</div>`;
  },

  _error(msg) {
    const { body } = this.els();
    if (body) body.innerHTML = `<div class="preview-state err"><iconify-icon icon="lucide:alert-triangle"></iconify-icon><span>${escapeHtml(msg)}</span></div>`;
  },

  _setActions(rawUrl) {
    const { openTab, download } = this.els();
    const abs = absUrl(rawUrl);
    if (openTab) { openTab.href = abs || '#'; openTab.classList.toggle('hidden', !abs); }
    if (download) { download.href = abs || '#'; download.classList.toggle('hidden', !abs); }
  },

  _renderPayload(payload) {
    const { body } = this.els();
    if (!body) return;
    this._setActions(payload.raw_url);
    const raw = absUrl(payload.raw_url);
    if (payload.mode === 'pdf' && raw) {
      body.innerHTML = `<iframe class="preview-frame" src="${escapeHtml(raw)}" title="Document preview"></iframe>`;
    } else if (payload.mode === 'image' && raw) {
      body.innerHTML = `<div class="preview-image-wrap"><img src="${escapeHtml(raw)}" alt="Generated image" style="max-width:100%;height:auto;display:block;margin:0 auto"></div>`;
    } else if (payload.mode === 'html') {
      body.innerHTML = `<div class="preview-doc">${payload.html || ''}</div>`;
    } else if (payload.mode === 'text') {
      body.innerHTML = `<pre class="preview-text">${escapeHtml(payload.text || '')}</pre>`;
    } else if (payload.mode === 'error') {
      this._error(payload.message || 'Could not render a preview of this file.');
    } else {
      body.innerHTML = `<div class="preview-state">No inline preview for this file type.${raw ? ` <a href="${escapeHtml(raw)}" download>Download it</a> instead.` : ''}</div>`;
    }
  },

  async openKb(doc) {
    const key = 'kb:' + doc.document_id;
    this.currentKey = key;
    this._show(doc.filename);
    if (doc.pending) { this._error('This file is still being indexed — try again in a moment.'); return; }
    if (!LIVE_BACKEND) { this._error('Backend not detected — preview is unavailable.'); return; }
    this._loading();
    try {
      const payload = await Api.kbPreview(Store.USER_ID, doc.document_id);
      if (this.currentKey !== key) return; // switched away while loading
      this._renderPayload(payload);
    } catch (err) {
      if (this.currentKey === key) this._error(err.message);
    }
  },

  async openGenerated(fileUrl, fileName) {
    const name = fileName || String(fileUrl).split('/').pop();
    const key = 'gen:' + fileUrl;
    this.currentKey = key;
    this._show(name);
    if (/\.pdf($|\?)/i.test(name) || /\.pdf($|\?)/i.test(fileUrl)) {
      this._renderPayload({ mode: 'pdf', raw_url: fileUrl });
      return;
    }
    if (/\.(png|jpe?g|webp|gif)($|\?)/i.test(name) || /\.(png|jpe?g|webp|gif)($|\?)/i.test(fileUrl)) {
      // A generated image (SD Turbo) is just a static file — render it
      // directly client-side, same as PDF above, no backend round-trip
      // needed (unlike docx/pptx/xlsx, which do need server-side
      // rendering into HTML/text via Api.previewGenerated).
      this._renderPayload({ mode: 'image', raw_url: fileUrl });
      return;
    }
    if (!LIVE_BACKEND) { this._error('Backend not detected — preview is unavailable.'); return; }
    this._loading();
    try {
      const payload = await Api.previewGenerated(fileUrl);
      if (this.currentKey !== key) return;
      this._renderPayload(payload);
    } catch (err) {
      if (this.currentKey === key) this._error(err.message);
    }
  },
};

/** Wire every "Preview" control currently in the DOM (deliverable cards,
 *  the Files & Deliverables sidebar list). Idempotent. */
function wirePreviewButtons() {
  document.querySelectorAll('.preview-file-btn').forEach((btn) => {
    if (btn._wired) return;
    btn._wired = true;
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      PreviewPanel.openGenerated(btn.dataset.previewUrl, btn.dataset.previewName);
    });
  });
}

/* ============================================================
   Knowledge Base — the persistent, per-operator file store in
   the sidebar. Files added here stay in retrieval scope for
   EVERY chat (merged with each chat's own uploads server-side)
   until removed. Add / remove / search all live here.
   ============================================================ */
const KB_ADD_EXTS = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.txt', '.md'];

const KnowledgeBase = {
  docs: [],
  q: '',

  els() {
    return {
      list: document.getElementById('kb-sidebar-list'),
      empty: document.getElementById('kb-sidebar-empty'),
      search: document.getElementById('kb-search'),
      addBtn: document.getElementById('kb-add-btn'),
      fileInput: document.getElementById('kb-file-input'),
    };
  },

  init() {
    const { search, addBtn, fileInput } = this.els();
    if (addBtn && fileInput) {
      addBtn.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files || []);
        fileInput.value = '';
        files.forEach((f) => this.add(f));
      });
    }
    if (search) {
      search.addEventListener('input', () => { this.q = search.value.trim().toLowerCase(); this.render(); });
    }
    this.render();
  },

  async refresh() {
    if (!LIVE_BACKEND) { this.render(); return; }
    try {
      const data = await Api.kbList(Store.USER_ID);
      this.docs = data.documents || [];
    } catch (err) {
      toast(`Couldn't load the Knowledge Base: ${err.message}`, true);
    }
    this.render();
  },

  filtered() {
    if (!this.q) return this.docs;
    return this.docs.filter((d) => (d.filename || '').toLowerCase().includes(this.q));
  },

  render() {
    const { list, empty } = this.els();
    if (!list || !empty) return;
    const rows = this.filtered();
    if (!rows.length) {
      list.innerHTML = '';
      empty.hidden = false;
      const span = empty.querySelector('span');
      if (span) {
        span.textContent = this.q
          ? 'No files match your search'
          : (LIVE_BACKEND ? 'Add files here — every chat can use them' : 'Connect the backend to use the Knowledge Base');
      }
      return;
    }
    empty.hidden = true;
    list.innerHTML = rows.map((d) => `
      <div class="kb-item${d.pending ? ' pending' : ''}" title="${escapeHtml(d.filename)}">
        <button class="kb-item-open" data-kb-open="${escapeHtml(d.document_id)}"${d.pending ? ' disabled' : ''}>
          <iconify-icon icon="${d.pending ? 'lucide:loader' : fileTypeIcon(d.filename)}" class="task-sidebar-item-icon"></iconify-icon>
          <span class="kb-item-name mono">${escapeHtml(truncate(d.filename, 20))}</span>
          ${d.pending ? '<span class="kb-item-chunks">…</span>' : (d.chunks ? `<span class="kb-item-chunks" title="${d.chunks} indexed chunk(s)">${d.chunks}</span>` : '')}
        </button>
        <button class="kb-item-remove" data-kb-remove="${escapeHtml(d.document_id)}" title="Remove from Knowledge Base"${d.pending ? ' disabled' : ''}>
          <iconify-icon icon="lucide:x"></iconify-icon>
        </button>
      </div>
    `).join('');
    list.querySelectorAll('[data-kb-open]').forEach((el) => {
      el.addEventListener('click', () => {
        const doc = this.docs.find((x) => x.document_id === el.dataset.kbOpen);
        if (doc) PreviewPanel.openKb(doc);
      });
    });
    list.querySelectorAll('[data-kb-remove]').forEach((el) => {
      el.addEventListener('click', (e) => { e.stopPropagation(); this.remove(el.dataset.kbRemove); });
    });
  },

  async add(file) {
    const name = file.name || 'file';
    const lname = name.toLowerCase();
    if (!KB_ADD_EXTS.some((e) => lname.endsWith(e))) {
      toast('Knowledge Base files must be PDF, Word, PowerPoint, Excel, or text.', true);
      return;
    }
    if (!LIVE_BACKEND) { toast('Backend not detected — the file was not added.', true); return; }

    const tempId = 'pending-' + Math.random().toString(36).slice(2);
    this.docs = [{ document_id: tempId, filename: name, chunks: 0, pending: true }, ...this.docs];
    this.render();
    try {
      const file_base64 = await fileToBase64(file);
      const res = await Api.kbUpload({
        user_id: Store.USER_ID, file_base64, file_name: name, file_mime_type: file.type || null,
      });
      this.docs = this.docs.filter((d) => d.document_id !== tempId);
      await this.refresh();
      toast(`${res.filename || name} added${res.chunks ? ` · ${res.chunks} chunk${res.chunks === 1 ? '' : 's'}` : ''}.`);
    } catch (err) {
      this.docs = this.docs.filter((d) => d.document_id !== tempId);
      this.render();
      toast(`Couldn't add ${name}: ${err.message}`, true);
    }
  },

  async remove(documentId) {
    const doc = this.docs.find((d) => d.document_id === documentId);
    if (!doc || doc.pending) return;
    if (!LIVE_BACKEND) { toast('Backend not detected.', true); return; }
    this.docs = this.docs.filter((d) => d.document_id !== documentId);
    this.render();
    if (PreviewPanel.currentKey === 'kb:' + documentId) PreviewPanel.close();
    try {
      await Api.kbDelete(Store.USER_ID, documentId);
      toast(`${doc.filename} removed from the Knowledge Base.`);
    } catch (err) {
      toast(`Couldn't remove ${doc.filename}: ${err.message}`, true);
      this.refresh();
    }
  },
};

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
  DiscoMode.init();
  initNotifications();
  TemplateLibrary.init();
  PreviewPanel.init();
  KnowledgeBase.init();
  VoiceInput.init();

  const greetEl = document.getElementById('idle-greet');
  if (greetEl) greetEl.textContent = Greetings.pick(UserAuth.nicknameFor(Store.USER_ID));

  document.getElementById('maximize-activity').addEventListener('click', (e) => { e.preventDefault(); openGraph(); });
  document.getElementById('graph-close').addEventListener('click', closeGraph);

  // Dismiss a pinned node popover on any click that isn't on the popover or a graph node.
  document.addEventListener('click', (e) => {
    if (!NodePopover.pinnedKey) return;
    if (e.target.closest('#node-popover') || e.target.closest('#graph-canvas [data-node]')) return;
    NodePopover.hide(true);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (NodePopover.isOpen()) { NodePopover.hide(true); return; }
    if (graphOpen) closeGraph();
  });

  LIVE_BACKEND = await Api.probe();
  const badge = document.getElementById('demo-badge');
  if (badge) badge.hidden = LIVE_BACKEND;
  if (!LIVE_BACKEND) toast('Backend not detected — running in demo simulation mode.');

  // Load the persistent per-operator Knowledge Base now that live-vs-demo
  // is known (the sidebar manager already painted its empty state).
  KnowledgeBase.refresh();
});
