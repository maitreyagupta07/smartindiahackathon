# Sovereign AI Workbench — Frontend

Vanilla HTML/CSS/JS, no build step, no dependencies beyond Google Fonts and the
Iconify web component (both loaded from CDN). Talks ONLY to Person B's three
endpoints — `POST /api/submit-task`, `GET /api/task-status/{id}`,
`GET /api/audit-log` — exactly per `MASTER_BUILD_GUIDE.md` §2.3. No other API
is called or invented, and the browser never talks to the Agent Executor,
Ollama, or the Tools service directly.

## Files

- `index.html` / `user.js` — the User workbench: task sidebar (history +
  files/deliverables), a centered composer for new tasks that transitions to
  a docked composer once a task is active, the minimal live-state strip, the
  maximized execution graph overlay, the node detail drawer, the notification
  bell, and the gradient color picker.
- `admin-login.html` — the Admin access gate (see **Admin access** below).
- `admin.html` / `admin.js` — the Admin "Sovereign AI Operations" shell:
  Overview, Users, Task Activity, Model & Routing, Knowledge Base, Audit Log,
  Security & Sovereignty, System Resources.
- `styles.css` — shared design tokens (dark default + light theme) and every
  component style.
- `app.js` — shared utilities: theme toggle, the API client, local
  task-history storage, `AdminAuth`, toasts.

## Preview it (no backend required)

```bash
cd frontend
python3 -m http.server 5500
# then open http://localhost:5500/index.html
```

With no backend reachable, the UI automatically drops into **Demo mode** (a
small badge says so) — submitting a task runs a client-side simulation
through the exact same state machine the real backend drives, so you can
watch the live strip, the execution graph, and notifications end-to-end.
Demo-only values are always labeled as such.

## Run it against the real backend

```bash
cd backend
pip install -r requirements.txt
python main.py          # serves both the API and this frontend on :8000
```

Then open `http://localhost:8000/`. The frontend probes `/api/audit-log` on
load; if the backend (and whatever of the executor/inference/tools stack is
up) responds, it runs live instead of in demo mode. This was verified
end-to-end during this integration pass using the backend's own documented
`mock_executor.py` stand-in for Person F — real UUID task IDs, real
`queued → processing → completed` polling, real `model_used`, and a real
written audit-log entry all round-tripped correctly.

**Bug fixed during this integration pass:** `backend/main.py` resolved
`FILES_DIR` against the process's current working directory
(`Path("./shared_files").resolve()`), but the backend's own README documents
launching it from *inside* `backend/` — which would resolve to
`backend/shared_files`, a different physical directory than the one
`tools/app/config.py` (which already anchors correctly) writes generated
files into. That mismatch would have 404'd every deliverable download no
matter how correct the frontend was. Fixed by anchoring `FILES_DIR` to the
repo root the same way `tools/app/config.py` already does — confirmed both
services now resolve to the identical path, and a real generated `.xlsx`
served correctly through `/files/<name>` afterward. No endpoint, port, or
response shape changed.

## Admin access

The locked contract has no login/session endpoint, so the "Switch to Admin"
link in the User sidebar leads to `admin-login.html`, a **lightweight,
client-side-only** access gate: a passcode is set on first use (stored in
`localStorage`) and required (session-scoped, via `sessionStorage`) on every
later visit — see the `AdminAuth` module in `app.js`. This intentionally does
not claim to be real server-side authentication; it exists only so the Admin
shell isn't one click away from the User workbench. If the contract ever adds
a real auth endpoint, `AdminAuth` is the only module that needs to change.

## Honesty constraints this UI follows

- `GET /api/task-status` does not return `task_type`, and `model_used` stays
  `null` until a task completes — so while a task is `processing`, the live
  strip and the graph show a generic, honestly-labeled progression
  (Classifying → Routing → Processing) rather than claiming to know the real
  route in advance. Once the poll returns `completed`/`failed`, the graph
  reconciles with the real `model_used` and `result.type` — routing/tool
  branches that weren't actually taken stay visibly present but dimmed, and a
  text-result task's "tool activity" node is honestly marked "not confirmed"
  rather than guessing which (if any) tool ran.
- **Token usage — backend contract gap, not fabricated.** The task/model
  detail views have a "Token Usage" slot, and it always reads *"Not exposed
  by current backend contract"*. This was verified by inspecting the actual
  code, not assumed: `agent/clients/inference_client.py`'s `call_inference()`
  discards everything from Ollama's `/api/generate` response except
  `data["response"]` (Ollama's real response does carry fields like
  `eval_count`/`prompt_eval_count`, but they never leave that function), and
  neither `ExecuteTaskResponse`/`TaskResult` (`agent/schemas/task.py`) nor the
  `/api/task-status` response shape carry a token field. Wiring real numbers
  through would mean touching the locked §2.4/§2.5 contract, which this pass
  was explicitly told not to do. The UI stays ready to display it the moment
  a future contract revision adds it.
- The Security & Sovereignty page is a static architecture explainer built
  from the real, fixed port topology in `MASTER_BUILD_GUIDE.md` §2.1/§2.2 —
  not a live network monitor. System Resources is likewise a static panel of
  the documented hardware-scaling decisions, not live GPU/CPU telemetry.
- Notifications use only the browser's own `Notification` API, gated on the
  existing task-status polling — there is no separate backend notification
  system. A notification fires once, exactly on a task's first transition
  into `completed`/`failed` (never during `queued`/`processing`), and the
  bell degrades gracefully when `Notification` is unsupported or permission
  is denied.
