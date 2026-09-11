/* Shared utilities: theme, API client (real backend contract §2.3), storage, toast. */

const Theme = {
  init() {
    const saved = localStorage.getItem('sovereign-theme');
    if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
    this._syncIcons();
    document.querySelectorAll('[data-theme-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => this.toggle());
    });
  },
  toggle() {
    const root = document.documentElement;
    const isLight = root.getAttribute('data-theme') === 'light';
    if (isLight) {
      root.removeAttribute('data-theme');
      localStorage.setItem('sovereign-theme', 'dark');
    } else {
      root.setAttribute('data-theme', 'light');
      localStorage.setItem('sovereign-theme', 'light');
    }
    this._syncIcons();
  },
  _syncIcons() {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    document.querySelectorAll('[data-icon-sun]').forEach((el) => el.classList.toggle('hidden', isLight));
    document.querySelectorAll('[data-icon-moon]').forEach((el) => el.classList.toggle('hidden', !isLight));
  },
};

/* Base URL of the Person B backend API (config.json ports.backend = 8000).
   NOT the tools service on 8001 — that only serves /tools/*, so every /api/*
   call 404s against it.
   - Served BY the backend itself (http://<host>:8000/...): same-origin, so an
     empty base -> relative URLs, no CORS needed.
   - Served by a separate static server (Live Server / python -m http.server
     on :5500, file://, etc): target the backend on :8000 explicitly; the
     backend's CORSMiddleware allow-lists :5500.
   Override for anything else with window.ERSA_API_BASE. */
const API_BASE = window.ERSA_API_BASE ?? window.SOVEREIGN_API_BASE ?? (() => {
  if (location.port === '8000') return '';  // served by the backend itself -> same-origin
  // Force 127.0.0.1, never the literal "localhost": on Windows "localhost"
  // resolves to IPv6 ::1 first, but uvicorn (host="0.0.0.0") only listens on
  // IPv4, so http://localhost:8000/* fails with "Failed to fetch" while
  // http://127.0.0.1:8000/* works.
  const host = (!location.hostname || location.hostname === 'localhost')
    ? '127.0.0.1'
    : location.hostname;
  return `http://${host}:8000`;
})();
/** Bearer token for the server-side session (see app/api/auth.py).
 *  Identity is now proven to the server by this token, not by a user_id
 *  string in the request — that string was previously accepted as-is, so
 *  anyone could read or delete another operator's Knowledge Base. */
const AuthToken = {
  KEY: 'sovereign-auth-token',
  get() { try { return localStorage.getItem(this.KEY) || null; } catch { return null; } },
  set(t) { try { localStorage.setItem(this.KEY, t); } catch { /* noop */ } },
  clear() { try { localStorage.removeItem(this.KEY); } catch { /* noop */ } },
};

/** Request headers carrying the session token when we have one. */
function authHeaders(extra = {}) {
  const t = AuthToken.get();
  return t ? { ...extra, Authorization: `Bearer ${t}` } : { ...extra };
}

const Api = {
  async submitTask({ user_id, prompt, file_base64 = null, file_name = null, file_mime_type = null }) {
    const res = await fetch(`${API_BASE}/api/submit-task`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id, prompt, file_base64, file_name, file_mime_type }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      throw new Error(body.error || `submit-task failed (${res.status})`);
    }
    return res.json(); // { task_id, status: "queued" }
  },

  async getTaskStatus(taskId) {
    const res = await fetch(`${API_BASE}/api/task-status/${encodeURIComponent(taskId)}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      throw new Error(body.error || `task-status failed (${res.status})`);
    }
    return res.json();
  },

  async getAuditLog() {
    const res = await fetch(`${API_BASE}/api/audit-log`, { headers: authHeaders() });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      throw new Error(body.error || `audit-log failed (${res.status})`);
    }
    return res.json(); // { entries: [...] }
  },

  /* ---- Chat-scoped Knowledge Base + conversational context (§ chat API) ---- */

  /** Ingest a PDF into one chat's Knowledge Base. The chat upload IS the
   *  ingestion action — no separate Admin step. */
  async chatUpload(chatId, { user_id, file_base64, file_name, file_mime_type = null, chat_title = null }) {
    const res = await fetch(`${API_BASE}/api/chat/${encodeURIComponent(chatId)}/upload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id, file_base64, file_name, file_mime_type, chat_title }),
    });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `upload failed (${res.status})`);
    return body; // { success, document_id, filename, chat_id, status, chunks }
  },

  /** Ask a question in a chat. Server keeps recent conversation context and
   *  retrieves only THIS chat's uploaded documents. */
  async chatMessage(chatId, { user_id, prompt, chat_title = null, file_base64 = null, file_mime_type = null, file_name = null }) {
    const res = await fetch(`${API_BASE}/api/chat/${encodeURIComponent(chatId)}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id, prompt, chat_title, file_base64, file_mime_type, file_name }),
    });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `message failed (${res.status})`);
    return body; // { task_id, status, chat_id }
  },

  async getKnowledgeBase(chatId = null) {
    const url = chatId
      ? `${API_BASE}/api/knowledge-base?chat_id=${encodeURIComponent(chatId)}`
      : `${API_BASE}/api/knowledge-base`;
    const res = await fetch(url);
    if (!res.ok) {
      const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
      throw new Error(body.error || `knowledge-base failed (${res.status})`);
    }
    return res.json(); // { documents: [...] }
  },

  /* ---- Persistent per-operator global Knowledge Base (sidebar manager) ---- */

  /** Every document in this operator's global KB. In scope for every chat. */
  async kbList() {
    const res = await fetch(`${API_BASE}/api/kb/list`, { headers: authHeaders() });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `kb list failed (${res.status})`);
    return body; // { documents: [...] }
  },

  /** Add a file to the global KB. Stays in scope for every chat until removed. */
  async kbUpload({ file_base64, file_name, file_mime_type = null }) {
    const res = await fetch(`${API_BASE}/api/kb/upload`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ file_base64, file_name, file_mime_type }),
    });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `kb upload failed (${res.status})`);
    return body;
  },

  async kbDelete(documentId) {
    const res = await fetch(
      `${API_BASE}/api/kb/${encodeURIComponent(documentId)}`,
      { method: 'DELETE', headers: authHeaders() },
    );
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `kb delete failed (${res.status})`);
    return body;
  },

  /** Server-rendered inline preview payload for one KB document. */
  async kbPreview(documentId) {
    const res = await fetch(
      `${API_BASE}/api/kb/${encodeURIComponent(documentId)}/preview`,
      { headers: authHeaders() },
    );
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `kb preview failed (${res.status})`);
    return body;
  },

  /** The raw KB file as a blob: URL.
   *  /api/kb/<id>/raw needs the bearer token, and a browser cannot attach a
   *  header to an <iframe src> or a download link — so fetch the bytes with
   *  auth and hand back an object URL instead. Putting the token in the
   *  query string would work too, but would leak it into history and logs. */
  async kbRawBlobUrl(documentId) {
    const res = await fetch(`${API_BASE}/api/kb/${encodeURIComponent(documentId)}/raw`, {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error(`could not load file (${res.status})`);
    return URL.createObjectURL(await res.blob());
  },

  /** Server-rendered inline preview payload for a generated deliverable in /files/. */
  async previewGenerated(fileNameOrUrl) {
    const name = String(fileNameOrUrl).split('/').pop().split('?')[0];
    const res = await fetch(`${API_BASE}/api/preview/generated/${encodeURIComponent(name)}`);
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `preview failed (${res.status})`);
    return body;
  },

  /* ---- Network-security monitor (Person E) — admin Security & Isolation ---- */

  /** Current/global sweep of this machine's own outbound TCP connections. */
  async getNetworkStatus() {
    const res = await fetch(`${API_BASE}/api/network-status`, { headers: authHeaders() });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `network-status failed (${res.status})`);
    return body;
  },

  /** Network-security record accumulated for one task (start + end sample). */
  async getTaskNetworkStatus(taskId) {
    const res = await fetch(`${API_BASE}/api/network-status/${encodeURIComponent(taskId)}`, { headers: authHeaders() });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `task network-status failed (${res.status})`);
    return body;
  },

  /* ---- Egress firewall (enforcement layer) — admin Security & Isolation ---- */

  /** Current state of the in-process egress firewall: enforcing flag,
   *  allow-list, and the live log of blocked off-LAN connection attempts. */
  async getEgressFirewall() {
    const res = await fetch(`${API_BASE}/api/egress-firewall`);
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `egress-firewall failed (${res.status})`);
    return body;
  },

  /** Actively probe public endpoints and confirm each is refused. */
  async runEgressSelfTest() {
    const res = await fetch(`${API_BASE}/api/egress-firewall/self-test`, { method: 'POST' });
    const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.error || `egress self-test failed (${res.status})`);
    return body;
  },

  /** POST /api/auth/admin-login — the real backend half of the "Switch to
   *  Admin" passcode gate (see AdminAuth.loginWithPasscode). Sends the
   *  CURRENT bearer token (if any) so an already-signed-in user gets
   *  promoted in place instead of being swapped to a different identity. */
  async adminLogin(passcode) {
    const res = await fetch(`${API_BASE}/api/auth/admin-login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ passcode }),
    });
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.detail || body.error || `admin sign-in failed (${res.status})`);
    return body;
  },

  /** Probe the real backend once, briefly, so the UI can honestly signal live-vs-demo mode.
   *
   *  Hits GET /health — unauthenticated by design, and the ONLY endpoint
   *  that answers the question actually being asked ("is the server up?").
   *  This used to probe /api/audit-log, which worked only for as long as
   *  that endpoint was unauthenticated; once it was correctly restricted to
   *  admins it returned 403, res.ok went false, and every page decided the
   *  backend was down and silently fell back to illustrative demo data. A
   *  liveness check must never depend on the caller's privileges. */
  async probe(timeoutMs = 1500) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/health`, { signal: ctrl.signal });
      clearTimeout(t);
      return res.ok;
    } catch {
      return false;
    }
  },
};

/**
 * UserAuth — the User workbench's sign-in, backed by REAL server-side
 * authentication (app/api/auth.py): passwords are PBKDF2-hashed in SQLite
 * and a bearer token proves identity on every API call.
 *
 * This was previously a client-side-only gate whose accounts lived in
 * localStorage, which meant the APIs behind it accepted whatever user_id a
 * caller chose to send. What remains local is presentation state only —
 * the nickname shown in the greeting, and the per-tab session marker.
 *
 * A signed-in account's chosen User ID becomes Store.USER_ID itself (see
 * below) — the same identifier already used to key every task, chat, and
 * this operator's persistent Knowledge Base — so signing in on this browser
 * again later reliably resumes the same identity instead of a random one.
 */
const UserAuth = {
  ACCOUNTS_KEY: 'sovereign-user-accounts',
  SESSION_KEY: 'sovereign-user-session',
  _accounts() {
    try { return JSON.parse(localStorage.getItem(this.ACCOUNTS_KEY) || '{}'); }
    catch { return {}; }
  },
  _save(accounts) { localStorage.setItem(this.ACCOUNTS_KEY, JSON.stringify(accounts)); },

  /* Credentials are verified SERVER-side now (app/api/auth.py): passwords
     are PBKDF2-hashed in SQLite and a bearer token proves identity on every
     API call. The old browser-local password check that lived here was a UI
     gate only — the APIs behind it accepted any user_id anyone cared to
     send. What stays local is presentation state (the nickname used for the
     greeting) and the per-tab session marker. */

  /** POST /api/auth/signup — creates the account and starts a session. */
  async signup(userId, password, nickname) {
    const res = await fetch(`${API_BASE}/api/auth/signup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, password, nickname }),
    });
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.detail || body.error || `signup failed (${res.status})`);
    this._acceptSession(body);
    return body;
  },

  /** POST /api/auth/login — verifies the password and starts a session. */
  async login(userId, password) {
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, password }),
    });
    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(body.detail || body.error || `sign in failed (${res.status})`);
    this._acceptSession(body);
    return body;
  },

  _acceptSession(body) {
    AuthToken.set(body.token);
    const accounts = this._accounts();
    accounts[body.user_id] = { nickname: body.nickname || body.user_id, is_admin: !!body.is_admin };
    this._save(accounts);
    this.startSession(body.user_id);
  },

  nicknameFor(userId) {
    const acc = this._accounts()[userId];
    return (acc && acc.nickname) || userId;
  },
  isAdmin(userId) {
    const acc = this._accounts()[userId || this.currentUserId()];
    return !!(acc && acc.is_admin);
  },
  startSession(userId) { sessionStorage.setItem(this.SESSION_KEY, userId); },
  endSession() {
    const t = AuthToken.get();
    if (t) {
      // Best-effort server-side revoke; the local clear happens regardless.
      fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', headers: authHeaders() }).catch(() => {});
    }
    AuthToken.clear();
    sessionStorage.removeItem(this.SESSION_KEY);
  },
  isSessionActive() { return !!sessionStorage.getItem(this.SESSION_KEY); },
  currentUserId() { return sessionStorage.getItem(this.SESSION_KEY); },
  /** Call at the very top of any user-workbench page. Redirects to the
   *  login screen immediately if there is no active session for this tab. */
  guard() {
    if (!this.isSessionActive() || !AuthToken.get()) {
      const next = encodeURIComponent(location.pathname.split('/').pop() + location.hash);
      location.replace(`login.html?next=${next}`);
    }
  },
};

/**
 * Dynamic, nickname-aware idle-view greeting — a few varied, lightly playful
 * (but still professional) lines per time-of-day bucket, so the workspace
 * doesn't say the same flat "Hi, Operator" on every visit. Picked once per
 * page load, not re-rolled on every render, so it doesn't visibly change
 * out from under the user mid-session.
 */
const Greetings = {
  BUCKETS: {
    morning: (n) => [
      `Good morning, ${n} ☀️`,
      `Morning, ${n} — ready when you are.`,
      `Rise and shine, ${n}.`,
    ],
    afternoon: (n) => [
      `Good afternoon, ${n}.`,
      `Hope your day's going well, ${n}.`,
      `Back at it, ${n}?`,
    ],
    evening: (n) => [
      `Good evening, night owl ${n}.`,
      `Evening, ${n} — burning the midnight oil?`,
      `Good evening, ${n}.`,
    ],
    lateNight: (n) => [
      `Coffee and ERSA time, ${n}?`,
      `Still up, ${n}? Let's make it count.`,
      `Late one tonight, ${n}.`,
    ],
  },
  pick(nickname) {
    const n = nickname || 'Operator';
    const h = new Date().getHours();
    const bucket = h >= 5 && h < 12 ? 'morning'
      : h >= 12 && h < 17 ? 'afternoon'
      : h >= 17 && h < 22 ? 'evening'
      : 'lateNight';
    const options = this.BUCKETS[bucket](n);
    return options[Math.floor(Math.random() * options.length)];
  },
};

const Store = {
  USER_ID: (() => {
    // A signed-in account's own chosen User ID always wins — this is what
    // makes signing in on this browser again resume the same identity
    // (same chats, same persistent Knowledge Base) instead of a random one.
    const signedIn = sessionStorage.getItem('sovereign-user-session');
    if (signedIn) return signedIn;
    let id = localStorage.getItem('sovereign-user-id');
    if (!id) {
      id = 'u-' + Math.random().toString(36).slice(2, 8);
      localStorage.setItem('sovereign-user-id', id);
    }
    return id;
  })(),
  key(userId) { return `sovereign-tasks:${userId}`; },
  getTasks(userId = Store.USER_ID) {
    try { return JSON.parse(localStorage.getItem(Store.key(userId)) || '[]'); }
    catch { return []; }
  },
  addTask(task, userId = Store.USER_ID) {
    const tasks = Store.getTasks(userId);
    tasks.unshift(task);
    localStorage.setItem(Store.key(userId), JSON.stringify(tasks.slice(0, 50)));
  },
  updateTask(taskId, patch, userId = Store.USER_ID) {
    const tasks = Store.getTasks(userId);
    const idx = tasks.findIndex((t) => t.task_id === taskId);
    if (idx !== -1) {
      tasks[idx] = { ...tasks[idx], ...patch };
      localStorage.setItem(Store.key(userId), JSON.stringify(tasks));
    }
  },

  /* ---- Chats (multi-turn). The chat_id is generated client-side and is the
     isolation key: it is sent on every upload and every message, and switching
     chats switches it. Conversation turns are cached here per browser for
     resume; the server keeps the authoritative recent context. ---- */
  chatsKey(userId = Store.USER_ID) { return `sovereign-chats:${userId}`; },
  chatMsgsKey(chatId, userId = Store.USER_ID) { return `sovereign-chat-msgs:${userId}:${chatId}`; },
  newChatId() {
    return (crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'chat-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  },
  getChats(userId = Store.USER_ID) {
    try { return JSON.parse(localStorage.getItem(Store.chatsKey(userId)) || '[]'); }
    catch { return []; }
  },
  upsertChat(chat, userId = Store.USER_ID) {
    const chats = Store.getChats(userId);
    const idx = chats.findIndex((c) => c.chat_id === chat.chat_id);
    if (idx === -1) chats.unshift(chat);
    else chats[idx] = { ...chats[idx], ...chat };
    localStorage.setItem(Store.chatsKey(userId), JSON.stringify(chats.slice(0, 50)));
  },
  getChatMessages(chatId, userId = Store.USER_ID) {
    try { return JSON.parse(localStorage.getItem(Store.chatMsgsKey(chatId, userId)) || '[]'); }
    catch { return []; }
  },
  saveChatMessages(chatId, messages, userId = Store.USER_ID) {
    localStorage.setItem(Store.chatMsgsKey(chatId, userId), JSON.stringify(messages.slice(-200)));
  },
};

function toast(message, isError = false) {
  let el = document.getElementById('global-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'global-toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.toggle('error-toast', isError);
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3200);
}

function timeAgoOrDuration(startIso, endIso) {
  if (!startIso || !endIso) return '—';
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function fileTypeIcon(filename = '') {
  const ext = filename.split('.').pop().toLowerCase();
  if (ext === 'xlsx' || ext === 'xls') return 'lucide:file-spreadsheet';
  if (ext === 'pptx' || ext === 'ppt') return 'lucide:file-sliders';
  if (ext === 'doc' || ext === 'docx') return 'lucide:file-type';
  if (ext === 'pdf') return 'lucide:file-text';
  if (['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(ext)) return 'lucide:image';
  return 'lucide:file-text';
}

/** Classifies a model_used string into the routing branch it lights up. Never guesses
 *  ahead of real data — call only once model_used is known (task completed/failed). */
function routeForModel(modelUsed) {
  if (!modelUsed) return null;
  const m = modelUsed.toLowerCase();
  if (m.includes('moondream') || m.includes('vision')) return 'vision';
  if (m.includes('lora') || m.includes('approval')) return 'lora';
  if (m.includes('sd-turbo') || m.includes('stable-diffusion') || m.includes('sd_turbo')) return 'image';
  if (m.includes('moment')) return 'forecast';
  return 'text';
}

/**
 * AdminAuth — the "Switch to Admin" gate, backed by the REAL backend
 * endpoint POST /api/auth/admin-login (app/api/auth.py). Entering the
 * shared admin passcode (config.json's "admin_passcode", default "1230#")
 * promotes the caller to a genuine is_admin=1 account and returns a real
 * bearer token — the same kind UserAuth uses — so every admin-only
 * endpoint (audit log, network/isolation status) actually authorizes.
 *
 * Deliberately a single SHARED passcode, not per-account security: this is
 * a single-operator, air-gapped, on-premise deployment, so the passcode
 * only exists to keep the Admin console from being one click away from the
 * User workbench, not to gate between untrusted parties (see
 * ADMIN_PASSCODE's docstring in app/storage/config.py).
 */
const AdminAuth = {
  SESSION_KEY: 'sovereign-admin-session',
  /** Sends the passcode to the backend; on success stores the returned
   *  bearer token and starts the admin session for this tab. Throws with a
   *  human-readable message on an incorrect passcode or a network error. */
  async loginWithPasscode(passcode) {
    const body = await Api.adminLogin(passcode);
    AuthToken.set(body.token);
    this.startSession();
    return body;
  },
  isSessionActive() { return sessionStorage.getItem(this.SESSION_KEY) === 'true'; },
  startSession() { sessionStorage.setItem(this.SESSION_KEY, 'true'); },
  endSession() {
    const t = AuthToken.get();
    if (t) {
      // Best-effort server-side revoke; the local clear happens regardless.
      fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', headers: authHeaders() }).catch(() => {});
    }
    AuthToken.clear();
    sessionStorage.removeItem(this.SESSION_KEY);
  },
  /** Call at the very top of any admin-shell page. Redirects to the login
   *  screen immediately if there is no active session for this tab. */
  guard() {
    if (!this.isSessionActive() || !AuthToken.get()) {
      const next = encodeURIComponent(location.pathname.split('/').pop() + location.hash);
      location.replace(`admin-login.html?next=${next}`);
    }
  },
};

/**
 * Mobile sidebar drawer — shared by both index.html and admin.html, since
 * both use the same .app-shell/.sidebar/.sidebar-backdrop structure. On
 * desktop the toggle button is hidden entirely (see the mobile media query
 * in styles.css), so this only ever does anything on a narrow viewport.
 */
function initSidebarToggle() {
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.querySelector('.sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (!toggle || !sidebar || !backdrop) return;

  const close = () => { sidebar.classList.remove('open'); backdrop.classList.remove('open'); };
  const open = () => { sidebar.classList.add('open'); backdrop.classList.add('open'); };

  toggle.addEventListener('click', () => {
    sidebar.classList.contains('open') ? close() : open();
  });
  backdrop.addEventListener('click', close);
  // Picking anything in the sidebar (a chat, a nav tab, "New Chat", …)
  // should close the drawer on mobile — otherwise it just sits open over
  // the page you meant to see.
  sidebar.addEventListener('click', (e) => {
    if (e.target.closest('a, button')) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

/**
 * Keeps --composer-dock-h (read by .conversation-scroll's bottom padding
 * in styles.css) equal to the docked composer's REAL measured height.
 *
 * The dock is position:absolute over the bottom of the conversation, and
 * its content stacks a variable number of rows (template chip bar, file
 * preview, composer bar, "AI can make mistakes" disclaimer) — any fixed
 * padding-bottom guess drifts out of date the moment a row is added or
 * removed, and the conversation's last message(s) render underneath the
 * dock instead of above it (see the "technical findings / summary
 * templates overlap the chat" bug this was written to fix). A
 * ResizeObserver on the dock itself is the only way this stays correct
 * automatically as its content changes, rather than needing every future
 * change to the dock's contents to remember to also bump a CSS number.
 * No-ops entirely on pages without a composer-dock (e.g. admin.html),
 * since this file is shared between index.html and admin.html.
 */
function initComposerDockSync() {
  const dock = document.getElementById('composer-dock');
  if (!dock) return;
  const root = document.documentElement;
  const scroller = document.getElementById('conversation-scroll');
  const sync = () => {
    // The dock's height (and so the padding this feeds) can change AFTER
    // the conversation was already scrolled to its bottom (e.g. the
    // observer fires on a later frame than renderConversation()'s own
    // scrollTop=scrollHeight) — capture "was the view at the bottom"
    // against the OLD scrollHeight before changing the padding, so
    // growing/shrinking the dock doesn't leave the view stranded above
    // the new true bottom (or, if it shrinks, unnecessarily short of it).
    const wasNearBottom = !scroller
      || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 200;
    root.style.setProperty('--composer-dock-h', `${dock.getBoundingClientRect().height}px`);
    if (scroller && wasNearBottom) scroller.scrollTop = scroller.scrollHeight;
  };
  sync();
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(sync).observe(dock);
  } else {
    // Old-browser fallback — not pixel-perfect on every content change,
    // but keeps it roughly right instead of frozen at page-load height.
    window.addEventListener('resize', sync);
  }
}

document.addEventListener('DOMContentLoaded', () => { Theme.init(); initSidebarToggle(); initComposerDockSync(); });
