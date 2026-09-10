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

/* Live Client Connections & Token Usage (Security & Sovereignty tab) — real
   data straight from the audit log: the actual source IP FastAPI recorded
   for each request (app/api/tasks.py's request.client.host, not a value the
   browser sent), and real token counts from Ollama's own response
   (prompt_eval_count/eval_count, see app/inference/client.py). Never
   estimated — a task predating this feature just shows blank IP/tokens. */
function isLocalOrPrivateIp(ip) {
  if (!ip) return true; // unknown -> don't false-flag it as external
  if (ip === '127.0.0.1' || ip === '::1' || ip.startsWith('127.')) return true;
  if (ip.startsWith('10.') || ip.startsWith('192.168.')) return true;
  const m = ip.match(/^172\.(\d+)\./);
  if (m && Number(m[1]) >= 16 && Number(m[1]) <= 31) return true; // 172.16.0.0/12
  return false;
}

function renderNetworkUsage(entries) {
  const tbody = document.getElementById('network-usage-tbody');
  const emptyEl = document.getElementById('network-usage-empty');
  if (!tbody) return; // section not on this page load

  const byUser = {};
  const uniqueIps = new Set();
  const nonLocalIps = new Set();
  let totalTokens = 0;

  entries.forEach((e) => {
    const key = e.user_id || 'unknown';
    if (!byUser[key]) byUser[key] = { ip: null, requests: 0, promptTokens: 0, completionTokens: 0, lastSeen: e.timestamp };
    const u = byUser[key];
    u.requests += 1;
    if (e.client_ip) {
      u.ip = e.client_ip; // most recent wins (entries are appended in order)
      uniqueIps.add(e.client_ip);
      if (!isLocalOrPrivateIp(e.client_ip)) nonLocalIps.add(e.client_ip);
    }
    u.promptTokens += e.prompt_tokens || 0;
    u.completionTokens += e.completion_tokens || 0;
    totalTokens += (e.prompt_tokens || 0) + (e.completion_tokens || 0);
    if (String(e.timestamp).localeCompare(String(u.lastSeen)) > 0) u.lastSeen = e.timestamp;
  });

  const statIps = document.getElementById('stat-unique-ips');
  const statNonLocal = document.getElementById('stat-nonlocal-ips');
  const statTokens = document.getElementById('stat-total-tokens');
  if (statIps) statIps.textContent = uniqueIps.size.toLocaleString();
  if (statNonLocal) {
    statNonLocal.textContent = nonLocalIps.size.toLocaleString();
    statNonLocal.style.color = nonLocalIps.size > 0 ? 'var(--error)' : 'var(--success)';
  }
  if (statTokens) statTokens.textContent = totalTokens.toLocaleString();

  const rows = Object.entries(byUser).sort((a, b) => b[1].requests - a[1].requests);
  if (rows.length === 0) {
    tbody.innerHTML = '';
    if (emptyEl) emptyEl.hidden = false;
    return;
  }
  if (emptyEl) emptyEl.hidden = true;
  tbody.innerHTML = rows.map(([userId, u]) => `
    <tr>
      <td>${escapeHtml(userId)}</td>
      <td class="mono">${u.ip ? escapeHtml(u.ip) + (isLocalOrPrivateIp(u.ip) ? '' : ' <span style="color:var(--error)">(non-local!)</span>') : '<span style="color:var(--text-muted)">unknown</span>'}</td>
      <td class="num">${u.requests}</td>
      <td class="num mono">${u.promptTokens.toLocaleString()}</td>
      <td class="num mono">${u.completionTokens.toLocaleString()}</td>
      <td class="num">${escapeHtml(formatTs(u.lastSeen))}</td>
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
  renderNetworkUsage(AUDIT_ENTRIES);
  renderComplianceView(AUDIT_ENTRIES);
  applyFiltersAndRender();
}

function demoAuditEntries() {
  const now = Date.now();
  const ips = ['192.168.1.42', '192.168.1.57', '10.0.0.2'];
  const mk = (i, type, model, file, promptTok, compTok) => ({
    task_id: `demo-${1000 + i}`,
    user_id: ['u-a1b2c3', 'u-x7y8z9', 'u-m4n5o6'][i % 3],
    task_type: type,
    model_used: model,
    timestamp: new Date(now - i * 3600_000).toISOString(),
    file_uploaded: file,
    client_ip: ips[i % 3],
    prompt_tokens: promptTok,
    completion_tokens: compTok,
  });
  return [
    mk(0, 'document-generation', 'approval-note-lora', true, 210, 340),
    mk(1, 'text-generation', 'qwen3:1.7b', false, 18, 52),
    mk(2, 'vision', 'moondream', true, 40, 88),
    mk(3, 'code-execution', 'qwen3:1.7b', false, 65, 120),
    mk(4, 'doc-search', 'qwen3:1.7b', false, 95, 60),
    mk(5, 'document-generation', 'qwen3:1.7b', true, 180, 410),
    mk(6, 'image-generation', 'sd-turbo', true, 12, 0),
    mk(7, 'time-series-forecasting', 'moment-1-small', false, 45, 30),
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

/* Egress Firewall — Security & Sovereignty tab. The ENFORCEMENT layer:
   calls GET /api/egress-firewall for live state + the blocked-attempt log,
   and POST /api/egress-firewall/self-test to actively prove public
   endpoints are unreachable. Distinct from NetworkMonitor below, which is
   the read-only witness. */
const EgressFirewall = {
  _fmtTs(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  },

  _setStatus(cls, icon, text) {
    const el = document.getElementById('efw-status');
    if (!el) return;
    el.className = `netmon-status ${cls}`;
    el.innerHTML = `<iconify-icon icon="${icon}"></iconify-icon><span>${escapeHtml(text)}</span>`;
  },

  render(d) {
    const setVal = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    const blocked = Number(d.blocked_attempts_total || 0);
    setVal('efw-blocked-total', blocked.toLocaleString());
    setVal('efw-allowed-total', Number(d.allowed_connections_total || 0).toLocaleString());
    setVal('efw-last-blocked', d.last_blocked_at ? this._fmtTs(d.last_blocked_at) : 'never');
    setVal('efw-last-checked', this._fmtTs(d.last_checked));

    const bt = document.getElementById('efw-blocked-total');
    if (bt) bt.style.color = blocked > 0 ? 'var(--warning, #b7791f)' : 'var(--success)';

    const list = document.getElementById('efw-allowlist');
    if (list) {
      list.innerHTML = (d.allowed_cidrs || [])
        .map((c) => `<span style="background:var(--border-subtle);padding:2px 7px;border-radius:6px">${escapeHtml(c)}</span>`)
        .join('');
    }

    const rows = Array.isArray(d.recent_blocked) ? d.recent_blocked : [];
    const wrap = document.getElementById('efw-blocked-log-wrap');
    const tbody = document.getElementById('efw-blocked-log');
    if (wrap) wrap.hidden = rows.length === 0;
    if (tbody) {
      tbody.innerHTML = rows.map((r) => `
        <tr>
          <td class="mono">${escapeHtml(this._fmtTs(r.ts))}</td>
          <td class="mono" style="color:var(--error)">${escapeHtml(String(r.dest_ip))}:${escapeHtml(String(r.dest_port))}</td>
          <td class="mono">${escapeHtml(String(r.caller || 'unknown'))}</td>
        </tr>`).join('');
    }

    if (d.enforcing) {
      this._setStatus('secure', 'lucide:shield-check',
        blocked > 0
          ? `ENFORCING — ${blocked} off-LAN connection${blocked === 1 ? '' : 's'} refused before leaving this machine`
          : 'ENFORCING — no off-LAN connection has been attempted; the internet is unreachable from this process');
    } else {
      this._setStatus('violation', 'lucide:shield-off',
        'NOT ENFORCING — the egress firewall is disabled in config (egress_firewall.enabled=false)');
    }
  },

  renderDemo() {
    this.render({
      enforcing: true, blocked_attempts_total: 0, allowed_connections_total: 0,
      allowed_cidrs: ['127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16'],
      recent_blocked: [], last_blocked_at: null, last_checked: new Date().toISOString(),
    });
    this._setStatus('unknown', 'lucide:help-circle', 'Backend not detected — illustrative demo. Run the app for live enforcement state.');
  },

  async load() {
    try { this.render(await Api.getEgressFirewall()); }
    catch { this.renderDemo(); }
  },

  async selfTest() {
    const out = document.getElementById('efw-self-test-result');
    const btn = document.getElementById('efw-self-test');
    if (!out) return;
    out.innerHTML = '<span style="color:var(--text-muted)">Probing public endpoints…</span>';
    if (btn) btn.disabled = true;
    try {
      const d = await Api.runEgressSelfTest();
      const rows = Array.isArray(d.results) ? d.results : [];
      const ok = d.verdict === 'ISOLATED';
      const head = `<div style="font-weight:600;color:${ok ? 'var(--success)' : 'var(--error)'};margin-bottom:6px">
        <iconify-icon icon="${ok ? 'lucide:shield-check' : 'lucide:shield-alert'}"></iconify-icon>
        ${escapeHtml(d.verdict)} — ${rows.length} public endpoint${rows.length === 1 ? '' : 's'} tested, ${Number(d.leaked || 0)} leaked</div>`;
      const body = rows.map((r) => {
        const blocked = r.outcome === 'BLOCKED';
        return `<div style="display:flex;gap:8px;align-items:baseline;padding:2px 0">
          <span class="mono" style="color:${blocked ? 'var(--success)' : 'var(--error)'};font-weight:600;min-width:74px">${escapeHtml(r.outcome)}</span>
          <span class="mono">${escapeHtml(r.target)}</span>
          <span style="color:var(--text-muted);font-size:11px">${escapeHtml(r.reason || '')}</span>
        </div>`;
      }).join('');
      out.innerHTML = head + body;
      this.load();
    } catch (err) {
      out.innerHTML = `<span style="color:var(--error)">${escapeHtml(err.message || 'self-test failed')}</span>`;
    } finally {
      if (btn) btn.disabled = false;
    }
  },

  init() {
    const r = document.getElementById('efw-refresh');
    if (r) r.addEventListener('click', () => this.load());
    const st = document.getElementById('efw-self-test');
    if (st) st.addEventListener('click', () => this.selfTest());
    const sovNav = document.querySelector('.nav-item[data-tab="sovereignty"]');
    if (sovNav) sovNav.addEventListener('click', () => this.load());
    this.load();
  },
};

/* Network Monitor (Person E) — Security & Sovereignty tab. Calls the app's
   own /api/network-status endpoint (a live psutil sweep of THIS machine's
   established TCP connections, classified LOCAL / LAN_CLIENT / EXTERNAL /
   VIOLATION). Evidence wording only ("N external connections observed"),
   never "zero external calls ever". Wireshark stays the independent
   packet-level check and is not wired in here. */
const NetworkMonitor = {
  _fmtTs(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  },

  _setStatus(elId, cls, icon, text) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.className = `netmon-status ${cls}`;
    el.innerHTML = `<iconify-icon icon="${icon}"></iconify-icon><span>${escapeHtml(text)}</span>`;
  },

  render(data) {
    const conns = Number(data.external_connections || 0);
    const ips = Array.isArray(data.external_ips) ? data.external_ips : [];
    const violations = Number(data.policy_violations || 0);

    const setVal = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setVal('netmon-ext-conns', conns.toLocaleString());
    setVal('netmon-ext-ips', ips.length.toLocaleString());
    setVal('netmon-violations', violations.toLocaleString());

    const violationColor = (id, bad) => {
      const e = document.getElementById(id);
      if (e) e.style.color = bad ? 'var(--error)' : 'var(--success)';
    };
    violationColor('netmon-ext-conns', conns > 0);
    violationColor('netmon-ext-ips', ips.length > 0);
    violationColor('netmon-violations', violations > 0);

    const ipList = document.getElementById('netmon-ext-ip-list');
    if (ipList) {
      ipList.hidden = ips.length === 0;
      ipList.textContent = ips.length ? `External/public IPs observed: ${ips.join(', ')}` : '';
    }
    const detail = document.getElementById('netmon-detail');
    if (detail) {
      detail.hidden = !data.detail;
      detail.textContent = data.detail || '';
    }
    document.getElementById('netmon-last-checked').textContent = this._fmtTs(data.last_checked);

    if (data.status === 'UNAVAILABLE') {
      this._setStatus('netmon-status', 'unknown', 'lucide:help-circle', 'MONITOR UNAVAILABLE — could not read this host’s socket table');
    } else if (data.status === 'VIOLATIONS_DETECTED' || conns > 0) {
      this._setStatus('netmon-status', 'violation', 'lucide:shield-x',
        `VIOLATIONS DETECTED — ${conns} external connection${conns === 1 ? '' : 's'} to ${ips.length} public IP${ips.length === 1 ? '' : 's'} observed`);
    } else {
      this._setStatus('netmon-status', 'secure', 'lucide:shield-check', 'SECURE — 0 external connections observed');
    }
  },

  renderDemo() {
    this.render({
      status: 'SECURE', external_connections: 0, external_ips: [], policy_violations: 0,
      last_checked: new Date().toISOString(),
      detail: 'Illustrative demo data — backend not detected. Run the app for a live sweep.',
    });
  },

  async load() {
    try {
      const data = await Api.getNetworkStatus();
      this.render(data);
    } catch {
      this.renderDemo();
    }
  },

  async checkTask() {
    const input = document.getElementById('netmon-task-input');
    const out = document.getElementById('netmon-task-result');
    if (!input || !out) return;
    const id = input.value.trim();
    if (!id) { out.innerHTML = '<span style="color:var(--text-muted)">Enter a task_id.</span>'; return; }
    out.innerHTML = '<span style="color:var(--text-muted)">Checking…</span>';
    try {
      const d = await Api.getTaskNetworkStatus(id);
      const ips = Array.isArray(d.external_ips) ? d.external_ips : [];
      const bad = d.status === 'VIOLATIONS_DETECTED' || Number(d.external_connections) > 0;
      const color = d.status === 'UNAVAILABLE' ? 'var(--text-muted)' : (bad ? 'var(--error)' : 'var(--success)');
      out.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:6px">
          <div style="font-weight:600;color:${color}">${escapeHtml(d.status)} · task ${escapeHtml(String(d.task_id).slice(0, 12))}</div>
          <div>External connections observed: <span class="mono">${Number(d.external_connections || 0)}</span></div>
          <div>External/public IPs observed: <span class="mono">${ips.length ? escapeHtml(ips.join(', ')) : '0'}</span></div>
          <div>Policy violations: <span class="mono">${Number(d.policy_violations || 0)}</span></div>
          <div style="color:var(--text-muted);font-size:11px">Sampled ${escapeHtml(this._fmtTs(d.started_at))} → ${escapeHtml(this._fmtTs(d.ended_at || d.last_checked))}</div>
        </div>`;
    } catch (err) {
      out.innerHTML = `<span style="color:var(--error)">${escapeHtml(err.message || 'lookup failed')}</span>`;
    }
  },

  init() {
    const refresh = document.getElementById('netmon-refresh');
    if (refresh) refresh.addEventListener('click', () => this.load());
    const check = document.getElementById('netmon-task-check');
    if (check) check.addEventListener('click', () => this.checkTask());
    const input = document.getElementById('netmon-task-input');
    if (input) input.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.checkTask(); });
    const sovNav = document.querySelector('.nav-item[data-tab="sovereignty"]');
    if (sovNav) sovNav.addEventListener('click', () => this.load());
    this.load();
  },
};

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initTableControls();
  initKnowledgeBase();
  EgressFirewall.init();
  NetworkMonitor.init();
  loadAuditLog();
});
