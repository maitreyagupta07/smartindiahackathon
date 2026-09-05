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
let currentTask = null; // the task object driving the UI right now
let pollHandle = null;

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
  const hasTask = !!currentTask;
  document.getElementById('idle-view').hidden = hasTask;
  document.getElementById('conversation-scroll').hidden = !hasTask;
  document.getElementById('composer-dock').hidden = !hasTask;
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
  if (!currentTask) {
    document.getElementById('topbar-task-title').textContent = 'Workspace';
    document.getElementById('topbar-task-id').textContent = '';
    return;
  }
  document.getElementById('topbar-task-title').textContent = truncate(currentTask.prompt, 60);
  document.getElementById('topbar-task-id').textContent = `#${currentTask.task_id.slice(0, 8).toUpperCase()}`;

  const inner = document.getElementById('conversation-inner');
  const userMsg = `
    <div class="msg-user">
      <div class="bubble">${escapeHtml(currentTask.prompt)}</div>
      ${currentTask.file_name ? `<div class="file-chip"><iconify-icon icon="${fileTypeIcon(currentTask.file_name)}"></iconify-icon>${escapeHtml(currentTask.file_name)}</div>` : ''}
    </div>`;

  let aiMsg = '';
  if (currentTask.status === 'failed') {
    aiMsg = `<div class="msg-ai"><div class="error-banner"><iconify-icon icon="lucide:alert-triangle" style="font-size:16px;flex-shrink:0;margin-top:1px"></iconify-icon><div>${escapeHtml(currentTask.error || 'The task failed.')}</div></div>${taskMetaRow(currentTask)}</div>`;
  } else if (currentTask.status === 'completed') {
    const chip = currentTask.model_used
      ? `<div class="model-chip"><iconify-icon icon="lucide:terminal" style="font-size:12px"></iconify-icon><span class="mono">${escapeHtml(currentTask.model_used)}</span></div>`
      : '';
    let body = '';
    if (currentTask.result && currentTask.result.type === 'file') {
      const fn = currentTask.result.file_name || 'deliverable';
      const url = currentTask.result.file_url || '#';
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
    } else if (currentTask.result && currentTask.result.text) {
      body = `<p class="summary-text">${escapeHtml(currentTask.result.text)}</p>`;
    } else {
      body = `<p class="summary-text" style="color:var(--text-muted)">Task completed with no returned content.</p>`;
    }
    aiMsg = `<div class="msg-ai">${chip}${body}${taskMetaRow(currentTask)}</div>`;
  } else {
    aiMsg = `<div class="msg-ai"><p class="summary-text" style="color:var(--text-muted)">Working on it${LIVE_BACKEND ? '' : ' (demo simulation)'}…</p></div>`;
  }

  inner.innerHTML = userMsg + aiMsg;
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
  const tasks = Store.getTasks();
  const listEl = document.getElementById('task-sidebar-list');
  const emptyEl = document.getElementById('task-sidebar-empty');
  emptyEl.hidden = tasks.length > 0;
  listEl.innerHTML = tasks.map((t) => taskSidebarItemHtml(t)).join('');
  listEl.querySelectorAll('[data-open-task]').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      openTaskFromSidebar(el.dataset.openTask);
    });
  });

  const files = tasks.filter((t) => t.result && t.result.type === 'file' && t.result.file_name);
  const filesListEl = document.getElementById('files-sidebar-list');
  const filesEmptyEl = document.getElementById('files-sidebar-empty');
  filesEmptyEl.hidden = files.length > 0;
  filesListEl.innerHTML = files.map((t) => `
    <a href="#" class="task-sidebar-item" data-open-task="${t.task_id}">
      <iconify-icon icon="${fileTypeIcon(t.result.file_name)}" class="task-sidebar-item-icon"></iconify-icon>
      <div class="task-sidebar-item-body">
        <div class="task-sidebar-item-title mono">${escapeHtml(truncate(t.result.file_name, 26))}</div>
      </div>
    </a>
  `).join('');
  filesListEl.querySelectorAll('[data-open-task]').forEach((el) => {
    el.addEventListener('click', (e) => { e.preventDefault(); openTaskFromSidebar(el.dataset.openTask); });
  });
}

function taskSidebarItemHtml(t) {
  const isActive = currentTask && currentTask.task_id === t.task_id;
  const statusDot = t.status === 'completed' ? 'ok' : t.status === 'failed' ? 'err' : 'pending';
  return `
    <a href="#" class="task-sidebar-item ${isActive ? 'active' : ''}" data-open-task="${t.task_id}">
      <span class="status-dot ${statusDot} task-sidebar-item-dot"></span>
      <div class="task-sidebar-item-body">
        <div class="task-sidebar-item-title">${escapeHtml(truncate(t.prompt, 30))}</div>
      </div>
    </a>`;
}

function openTaskFromSidebar(taskId) {
  const task = Store.getTasks().find((t) => t.task_id === taskId);
  if (!task) return;
  stopPolling();
  currentTask = task;
  renderConversation();
  renderTaskSidebar();
  tick();
  if (LIVE_BACKEND && (task.status === 'queued' || task.status === 'processing')) {
    startPollingReal(task.task_id);
  }
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

async function pollReal(taskId) {
  try {
    const data = await Api.getTaskStatus(taskId);
    const wasSettled = currentTask && (currentTask.status === 'completed' || currentTask.status === 'failed');
    Object.assign(currentTask, data);
    Store.updateTask(taskId, data);
    renderConversation();
    renderTaskSidebar();
    if ((data.status === 'completed' || data.status === 'failed') && !wasSettled) {
      stopPolling();
      Notifications.notifyTaskSettled(currentTask);
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
  const isFileish = /excel|xlsx|spreadsheet|docx|report|pptx|presentation|approval note/i.test(task.prompt);
  const isVision = task.file_mime_type && task.file_mime_type.startsWith('image/');
  const isApproval = /approval note/i.test(task.prompt);
  const totalMs = 4200;
  const tId = setInterval(() => tick(), 200);
  setTimeout(() => {
    clearInterval(tId);
    const model_used = isVision ? 'moondream' : isApproval ? 'approval-note-lora' : 'qwen2.5:1.5b-instruct';
    const completed_at = new Date().toISOString();
    const ext = isFileish ? (/excel|xlsx|spreadsheet/i.test(task.prompt) ? 'xlsx' : /presentation|pptx/i.test(task.prompt) ? 'pptx' : 'docx') : null;
    const result = isFileish
      ? { type: 'file', text: null, file_url: '#', file_name: `demo-output-${task.task_id.slice(0, 6)}.${ext}` }
      : { type: 'text', text: 'This is a demo-simulated response — no real backend was reached, so this content is illustrative only.', file_url: null, file_name: null };
    Object.assign(currentTask, { status: 'completed', model_used, completed_at, result, error: null });
    Store.updateTask(task.task_id, currentTask);
    renderConversation();
    renderTaskSidebar();
    tick();
    Notifications.notifyTaskSettled(currentTask);
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

  async function submit(fromInput) {
    const prompt = fromInput.value.trim();
    if (!prompt) return;
    idleSend.disabled = true; dockedSend.disabled = true;

    let file_base64 = null, file_name = null, file_mime_type = null;
    if (pendingFile) {
      file_name = pendingFile.name;
      file_mime_type = pendingFile.type || null;
      file_base64 = await fileToBase64(pendingFile);
    }

    const submitted_at_client = new Date().toISOString();
    idleInput.value = ''; dockedInput.value = '';
    fileInput.value = '';
    pendingFile = null;
    syncFilePreview();

    try {
      let task_id, status;
      if (LIVE_BACKEND) {
        const res = await Api.submitTask({ user_id: Store.USER_ID, prompt, file_base64, file_name, file_mime_type });
        task_id = res.task_id; status = res.status;
      } else {
        task_id = 'demo-' + Math.random().toString(36).slice(2, 10);
        status = 'queued';
      }
      currentTask = {
        task_id, status, prompt, file_name, file_mime_type,
        model_used: null, started_at: null, completed_at: null,
        result: { type: null, text: null, file_url: null, file_name: null }, error: null,
        submitted_at_client, demo: !LIVE_BACKEND,
      };
      Store.addTask(currentTask);
      renderConversation();
      renderTaskSidebar();
      tick();

      if (LIVE_BACKEND) {
        startPollingReal(task_id);
      } else {
        currentTask.started_at = submitted_at_client;
        currentTask.status = 'processing';
        runDemoSimulation(currentTask);
      }
    } catch (err) {
      toast(`Couldn't submit task: ${err.message}`, true);
    } finally {
      idleSend.disabled = false; dockedSend.disabled = false;
    }
  }

  idleSend.addEventListener('click', () => submit(idleInput));
  idleInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(idleInput); });
  dockedSend.addEventListener('click', () => submit(dockedInput));
  dockedInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(dockedInput); });

  document.getElementById('nav-new-task').addEventListener('click', (e) => {
    e.preventDefault();
    currentTask = null;
    stopPolling();
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
    subtitle.textContent = `TASK-${currentTask.task_id.slice(0, 8).toUpperCase()}${currentTask.demo ? ' · DEMO' : ''}`;
    footerTask.textContent = `TASK: ${currentTask.task_id.slice(0, 12).toUpperCase()}`;
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
      n.onclick = () => { window.focus(); openTaskFromSidebar(task.task_id); };
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
