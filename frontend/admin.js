/* Admin — Overview + Security & Sovereignty. Reads ONLY GET /api/audit-log (§2.3).
   That endpoint returns {task_id, user_id, task_type, model_used, timestamp, file_uploaded}
   per entry — there is no status/success field in it, so this view never fabricates a
   success rate or a status column; it only shows what the real data actually carries. */

let AUDIT_ENTRIES = [];
let SORT = { col: 'timestamp', dir: 'desc' };

function modelTagClass(model) {
  if (!model) return 'lora';
  const m = model.toLowerCase();
  if (m.includes('moondream')) return 'vision';
  if (m.includes('lora')) return 'lora';
  return 'text';
}

function renderStats(entries) {
  const total = entries.length;
  const uniqueUsers = new Set(entries.map((e) => e.user_id)).size;
  const withFiles = entries.filter((e) => e.file_uploaded).length;

  document.getElementById('stat-total').textContent = total.toLocaleString();
  document.getElementById('stat-users').textContent = uniqueUsers.toLocaleString();
  document.getElementById('stat-files').textContent = withFiles.toLocaleString();
  document.getElementById('stat-types').textContent = new Set(entries.map((e) => e.task_type)).size.toLocaleString();

  renderModelDistribution(entries, 'model-dist-bar', 'model-dist-legend');
  renderModelDistribution(entries, 'model-dist-bar-2', 'model-dist-legend-2');
}

function renderModelDistribution(entries, barId, legendId) {
  const distEl = document.getElementById(barId);
  const legendEl = document.getElementById(legendId);
  if (!distEl || !legendEl) return;
  const total = entries.length;
  const modelCounts = {};
  entries.forEach((e) => { modelCounts[e.model_used || 'unknown'] = (modelCounts[e.model_used || 'unknown'] || 0) + 1; });
  const colors = ['var(--accent)', 'var(--text-secondary)', 'var(--text-muted)', 'var(--pending)'];
  const sorted = Object.entries(modelCounts).sort((a, b) => b[1] - a[1]);
  if (total === 0) {
    distEl.innerHTML = '';
    legendEl.innerHTML = '<span style="color:var(--text-muted);font-size:12px">No tasks logged yet.</span>';
  } else {
    distEl.innerHTML = sorted.map(([, count], i) => `<div style="height:100%;background:${colors[i % colors.length]};width:${(count / total * 100).toFixed(1)}%"></div>`).join('');
    legendEl.innerHTML = sorted.map(([model, count], i) => `
      <div class="dist-legend-item"><span class="sw" style="background:${colors[i % colors.length]}"></span>${escapeHtml(model)} <span class="mono" style="color:var(--text-muted)">${(count / total * 100).toFixed(0)}%</span></div>
    `).join('');
  }
}

function renderOverviewPreview(entries) {
  const tbody = document.getElementById('overview-preview-tbody');
  const emptyEl = document.getElementById('overview-preview-empty');
  const preview = [...entries].sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp))).slice(0, 5);
  if (preview.length === 0) {
    tbody.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  tbody.innerHTML = preview.map((e) => `
    <tr>
      <td class="mono">${escapeHtml(e.task_id.slice(0, 8))}</td>
      <td>${escapeHtml(e.user_id)}</td>
      <td>${escapeHtml(e.task_type)}</td>
      <td><span class="model-tag ${modelTagClass(e.model_used)}">${escapeHtml(e.model_used || 'unknown')}</span></td>
      <td class="num">${escapeHtml(formatTs(e.timestamp))}</td>
    </tr>
  `).join('');
}

function renderUsers(entries) {
  const tbody = document.getElementById('users-tbody');
  const emptyEl = document.getElementById('users-empty');
  const byUser = {};
  entries.forEach((e) => {
    if (!byUser[e.user_id]) byUser[e.user_id] = { tasks: 0, models: new Set(), attachments: 0, lastActive: e.timestamp };
    const u = byUser[e.user_id];
    u.tasks += 1;
    u.models.add(e.model_used || 'unknown');
    if (e.file_uploaded) u.attachments += 1;
    if (String(e.timestamp).localeCompare(String(u.lastActive)) > 0) u.lastActive = e.timestamp;
  });
  const rows = Object.entries(byUser).sort((a, b) => b[1].tasks - a[1].tasks);
  if (rows.length === 0) {
    tbody.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  tbody.innerHTML = rows.map(([userId, u]) => `
    <tr>
      <td>${escapeHtml(userId)}</td>
      <td class="num">${u.tasks}</td>
      <td>${Array.from(u.models).map((m) => `<span class="model-tag ${modelTagClass(m)}" style="margin-right:4px">${escapeHtml(m)}</span>`).join('')}</td>
      <td class="num">${u.attachments}</td>
      <td class="num">${escapeHtml(formatTs(u.lastActive))}</td>
    </tr>
  `).join('');
}

function renderComplianceView(entries) {
  const tbody = document.getElementById('compliance-tbody');
  const emptyEl = document.getElementById('compliance-empty');
  const ordered = [...entries].sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
  if (ordered.length === 0) {
    tbody.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  tbody.innerHTML = ordered.map((e, i) => `
    <tr>
      <td class="num mono">${i + 1}</td>
      <td class="mono">${escapeHtml(e.task_id.slice(0, 8))}</td>
      <td>${escapeHtml(e.user_id)}</td>
      <td>${escapeHtml(e.task_type)}</td>
      <td><span class="model-tag ${modelTagClass(e.model_used)}">${escapeHtml(e.model_used || 'unknown')}</span></td>
      <td class="num">${escapeHtml(formatTs(e.timestamp))}</td>
    </tr>
  `).join('');
}

/* Knowledge Base — live data. Documents uploaded through a chat are ingested
   into the Tools service's ChromaDB (tagged with their chat_id) and listed
   here via GET /api/knowledge-base. No separate upload here: the chat upload
   is the ingestion action, this is the management/viewing surface. */
let KB_DOCS = [];

function fmtDateOnly(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

async function loadKnowledgeBase() {
  const searchEl = document.getElementById('kb-search');
  try {
    const live = await Api.probe();
    if (live) {
      const data = await Api.getKnowledgeBase();
      KB_DOCS = data.documents || [];
    } else {
      KB_DOCS = demoKbDocs();
    }
  } catch (err) {
    KB_DOCS = [];
    toast(`Couldn't load Knowledge Base: ${err.message}`, true);
  }
  renderKnowledgeBase(searchEl ? searchEl.value : '');
}

function demoKbDocs() {
  return [
    { filename: 'research.pdf', file_type: 'pdf', status: 'indexed', chat_title: 'Research Discussion', chat_id: 'demo-a', chunks: 24, uploaded_at: new Date(Date.now() - 3600_000).toISOString() },
    { filename: 'paper.pdf', file_type: 'pdf', status: 'indexed', chat_title: 'Project Discussion', chat_id: 'demo-b', chunks: 11, uploaded_at: new Date(Date.now() - 7200_000).toISOString() },
  ];
}

function renderKnowledgeBase(query = '') {
  const tbody = document.getElementById('kb-tbody');
  const emptyEl = document.getElementById('kb-empty');
  const q = query.trim().toLowerCase();
  const rows = KB_DOCS.filter((d) => {
    if (!q) return true;
    return `${d.filename || ''} ${d.chat_title || ''} ${d.chat_id || ''}`.toLowerCase().includes(q);
  });
  if (rows.length === 0) {
    tbody.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  tbody.innerHTML = rows.map((d) => {
    const chat = d.chat_title || (d.chat_id ? d.chat_id.slice(0, 8) : '—');
    const status = (d.status || 'indexed').toLowerCase();
    const statusClass = status === 'indexed' ? 'ok' : status === 'failed' ? 'err' : 'pending';
    return `
    <tr>
      <td><div class="status-cell"><iconify-icon icon="lucide:file-text" style="font-size:14px;color:var(--text-muted)"></iconify-icon>${escapeHtml(d.filename || 'unknown')}</div></td>
      <td class="mono">${escapeHtml((d.file_type || 'pdf').toUpperCase())}</td>
      <td><span class="status-cell" style="color:var(--text-secondary)"><span class="status-dot ${statusClass}"></span>${escapeHtml(status.charAt(0).toUpperCase() + status.slice(1))}</span></td>
      <td>${escapeHtml(chat)}</td>
      <td class="num">${d.chunks != null ? d.chunks : '—'}</td>
      <td class="num">${escapeHtml(fmtDateOnly(d.uploaded_at))}</td>
    </tr>`;
  }).join('');
}

function populateTypeFilter(entries) {
  const sel = document.getElementById('filter-type');
  const types = Array.from(new Set(entries.map((e) => e.task_type))).sort();
  sel.innerHTML = '<option value="">All Types</option>' + types.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
}

function applyFiltersAndRender() {
  const q = document.getElementById('search-input').value.trim().toLowerCase();
  const typeFilter = document.getElementById('filter-type').value;
  const fileFilter = document.getElementById('filter-attachment').value;

  let rows = AUDIT_ENTRIES.filter((e) => {
    if (typeFilter && e.task_type !== typeFilter) return false;
    if (fileFilter === 'yes' && !e.file_uploaded) return false;
    if (fileFilter === 'no' && e.file_uploaded) return false;
    if (q && !(`${e.task_id} ${e.user_id} ${e.task_type} ${e.model_used}`.toLowerCase().includes(q))) return false;
    return true;
  });

  rows.sort((a, b) => {
    const av = a[SORT.col], bv = b[SORT.col];
    const cmp = String(av).localeCompare(String(bv));
    return SORT.dir === 'asc' ? cmp : -cmp;
  });

  const tbody = document.getElementById('audit-tbody');
  const emptyEl = document.getElementById('audit-empty');
  if (rows.length === 0) {
    tbody.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  tbody.innerHTML = rows.map((e) => `
    <tr>
      <td class="mono">${escapeHtml(e.task_id.slice(0, 8))}</td>
      <td>${escapeHtml(e.user_id)}</td>
      <td>${escapeHtml(e.task_type)}</td>
      <td><span class="model-tag ${modelTagClass(e.model_used)}">${escapeHtml(e.model_used || 'unknown')}</span></td>
      <td>${e.file_uploaded ? '<span class="status-cell" style="color:var(--text-secondary)"><iconify-icon icon="lucide:paperclip" style="font-size:12px"></iconify-icon>Yes</span>' : '<span style="color:var(--text-muted)">No</span>'}</td>
      <td class="num">${escapeHtml(formatTs(e.timestamp))}</td>
    </tr>
  `).join('');
}

function formatTs(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadAuditLog() {
  const loadingBar = document.getElementById('audit-loading');
  loadingBar.classList.add('show');
  try {
    const live = await Api.probe();
    document.getElementById('admin-demo-badge').hidden = live;
    if (live) {
      const data = await Api.getAuditLog();
      AUDIT_ENTRIES = data.entries || [];
    } else {
      AUDIT_ENTRIES = demoAuditEntries();
      toast('Backend not detected — showing illustrative demo audit data.');
    }
  } catch (err) {
    toast(`Couldn't load audit log: ${err.message}`, true);
    AUDIT_ENTRIES = [];
  } finally {
    loadingBar.classList.remove('show');
  }
  populateTypeFilter(AUDIT_ENTRIES);
  renderStats(AUDIT_ENTRIES);
  renderOverviewPreview(AUDIT_ENTRIES);
  renderUsers(AUDIT_ENTRIES);
  renderComplianceView(AUDIT_ENTRIES);
  applyFiltersAndRender();
}

function demoAuditEntries() {
  const now = Date.now();
  const mk = (i, type, model, file) => ({
    task_id: `demo-${1000 + i}`,
    user_id: ['u-a1b2c3', 'u-x7y8z9', 'u-m4n5o6'][i % 3],
    task_type: type,
    model_used: model,
    timestamp: new Date(now - i * 3600_000).toISOString(),
    file_uploaded: file,
  });
  return [
    mk(0, 'document-generation', 'approval-note-lora', true),
    mk(1, 'text-generation', 'qwen2.5:1.5b-instruct', false),
    mk(2, 'vision', 'moondream', true),
    mk(3, 'code-execution', 'qwen2.5:1.5b-instruct', false),
    mk(4, 'doc-search', 'qwen2.5:1.5b-instruct', false),
    mk(5, 'document-generation', 'qwen2.5:1.5b-instruct', true),
  ];
}

function initTabs() {
  const navTabs = document.querySelectorAll('.nav-item[data-tab]');
  const sections = document.querySelectorAll('.admin-tabs-content > section');
  function activate(tabId) {
    if (!document.getElementById(`tab-${tabId}`)) return;
    navTabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === tabId));
    sections.forEach((s) => s.classList.toggle('active', s.id === `tab-${tabId}`));
    const matched = Array.from(navTabs).find((t) => t.dataset.tab === tabId);
    document.getElementById('admin-page-title').textContent = matched ? matched.textContent.trim() : 'Overview';
    const scroller = document.querySelector('.conversation-scroll');
    if (scroller) scroller.scrollTop = 0;
  }
  // Delegate to any in-page link that points at a known tab hash (sidebar nav
  // items, "View all →", the compliance view's cross-link, etc.) so they all
  // share one activation path instead of each needing a bespoke listener.
  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[href^="#"]');
    if (!link) return;
    const tabId = link.getAttribute('href').slice(1);
    if (!document.getElementById(`tab-${tabId}`)) return;
    e.preventDefault();
    activate(tabId);
    history.replaceState(null, '', `#${tabId}`);
  });
  const initial = (location.hash || '').replace('#', '') || 'overview';
  activate(document.getElementById(`tab-${initial}`) ? initial : 'overview');
}

function initTableControls() {
  document.getElementById('search-input').addEventListener('input', applyFiltersAndRender);
  document.getElementById('filter-type').addEventListener('change', applyFiltersAndRender);
  document.getElementById('filter-attachment').addEventListener('change', applyFiltersAndRender);
  document.querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      SORT.dir = SORT.col === col && SORT.dir === 'asc' ? 'desc' : 'asc';
      SORT.col = col;
      applyFiltersAndRender();
    });
  });
  document.getElementById('export-log').addEventListener('click', () => {
    const csv = ['task_id,user_id,task_type,model_used,file_uploaded,timestamp']
      .concat(AUDIT_ENTRIES.map((e) => [e.task_id, e.user_id, e.task_type, e.model_used, e.file_uploaded, e.timestamp].join(',')))
      .join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'audit-log.csv';
    a.click();
  });
  document.getElementById('reset-filters').addEventListener('click', () => {
    document.getElementById('search-input').value = '';
    document.getElementById('filter-type').value = '';
    document.getElementById('filter-attachment').value = '';
    applyFiltersAndRender();
  });
}

function initKnowledgeBase() {
  document.getElementById('kb-search').addEventListener('input', (e) => renderKnowledgeBase(e.target.value));
  // Re-fetch whenever the Knowledge Base tab is opened, so a PDF just added
  // from a chat shows up without a full page reload.
  const kbNav = document.querySelector('.nav-item[data-tab="knowledge"]');
  if (kbNav) kbNav.addEventListener('click', () => loadKnowledgeBase());
  loadKnowledgeBase();
}

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initTableControls();
  initKnowledgeBase();
  loadAuditLog();
});
