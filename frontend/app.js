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

/* Backend contract per MASTER_BUILD_GUIDE.md §2.3 — the ONLY API this frontend calls.
   Relative paths so this works whether served by backend/main.py (port 8000) or
   proxied; API_BASE can be overridden for local static-preview testing. */
const API_BASE = window.SOVEREIGN_API_BASE || '';

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
    const res = await fetch(`${API_BASE}/api/audit-log`);
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
  async chatMessage(chatId, { user_id, prompt, chat_title = null }) {
    const res = await fetch(`${API_BASE}/api/chat/${encodeURIComponent(chatId)}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id, prompt, chat_title }),
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

  /** Probe the real backend once, briefly, so the UI can honestly signal live-vs-demo mode. */
  async probe(timeoutMs = 1500) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), timeoutMs);
      const res = await fetch(`${API_BASE}/api/audit-log`, { signal: ctrl.signal });
      clearTimeout(t);
      return res.ok;
    } catch {
      return false;
    }
  },
};

const Store = {
  USER_ID: (() => {
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
  return 'lucide:file-text';
}

/** Classifies a model_used string into the routing branch it lights up. Never guesses
 *  ahead of real data — call only once model_used is known (task completed/failed). */
function routeForModel(modelUsed) {
  if (!modelUsed) return null;
  const m = modelUsed.toLowerCase();
  if (m.includes('moondream') || m.includes('vision')) return 'vision';
  if (m.includes('lora') || m.includes('approval')) return 'lora';
  return 'text';
}

/**
 * AdminAuth — a lightweight, CLIENT-SIDE-ONLY access gate for the Admin
 * shell. The locked backend contract (MASTER_BUILD_GUIDE.md §2.3) has no
 * login/session/auth endpoint, so this cannot be — and does not claim to
 * be — real authenticated security. It exists purely so the Admin surface
 * isn't one click away from the User workbench: a passcode is set on first
 * use and stored in this browser's localStorage, then required (session-
 * scoped) on every subsequent visit to the Admin shell. This is a UI
 * access gate, not a substitute for real server-side authentication —
 * if/when the contract adds a real auth endpoint, this module is the only
 * place that needs to change.
 */
const AdminAuth = {
  PASSCODE_KEY: 'sovereign-admin-passcode',
  SESSION_KEY: 'sovereign-admin-session',
  hasPasscode() { return !!localStorage.getItem(this.PASSCODE_KEY); },
  setPasscode(p) { localStorage.setItem(this.PASSCODE_KEY, p); },
  checkPasscode(p) { return p && p === localStorage.getItem(this.PASSCODE_KEY); },
  isSessionActive() { return sessionStorage.getItem(this.SESSION_KEY) === 'true'; },
  startSession() { sessionStorage.setItem(this.SESSION_KEY, 'true'); },
  endSession() { sessionStorage.removeItem(this.SESSION_KEY); },
  /** Call at the very top of any admin-shell page. Redirects to the login
   *  screen immediately if there is no active session for this tab. */
  guard() {
    if (!this.isSessionActive()) {
      const next = encodeURIComponent(location.pathname.split('/').pop() + location.hash);
      location.replace(`admin-login.html?next=${next}`);
    }
  },
};

document.addEventListener('DOMContentLoaded', () => Theme.init());
