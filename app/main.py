"""
Sovereign On-Premise Agentic AI Workbench — single-node deployment.

The ONE FastAPI app, the ONE LAN-facing network listener (§2.2/§2.3 of the
original multi-service contract, now collapsed into a single process per
the single-node architecture decision — see PERSON_A_NOTES.md for the
audit/rationale). Everything downstream of this file — the agent loop,
router, tools (code execution, doc search, file generation), and Ollama
inference client — is invoked as plain Python, in-process, EXCEPT:

  - Ollama: stays a separate local OS process (a third-party binary, not
    our code), invoked over HTTP to localhost only (app/inference/client.py).
  - The Docker code-execution sandbox: stays a separate, ephemeral,
    network-disabled container per run (app/tools/sandbox.py) — that
    container boundary IS the actual security isolation; nothing about
    merging the Python processes should or does change it.

No other internal service is exposed. The browser talks ONLY to this app.
"""
import sys

# Windows' console/redirected-stdout encoding defaults to the legacy
# system codepage (e.g. cp1252), not UTF-8 — unlike Linux/macOS, where
# stdout is UTF-8 by default. Model responses (and prompts) routinely
# contain emoji/non-Latin characters, and this app's debug logging
# (app/inference/client.py and others) prints them straight to
# stdout/stderr. Without this, a single emoji in a model's reply crashes
# the *entire request* with UnicodeEncodeError the moment it's logged —
# not a model or prompt problem, purely a Windows console-encoding gap.
# errors="replace" means a truly unencodable byte becomes "?" in the log
# instead of taking the process down; reconfigure() is a no-op-safe call
# on platforms where stdout is already UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.auth import router as auth_router, init_accounts_db
from .api.errors import install_error_handlers
from .api.tasks import router as tasks_router
from .api.chat import router as chat_router
from .api.knowledge import router as knowledge_router
from .api.network import router as network_router
from .audit.log import init_db
from .security import egress_firewall
from .storage.config import EGRESS_FIREWALL, FILES_DIR, REPO_ROOT

# Install the in-process egress firewall as early as possible — before the
# routers, the audit DB, or the inference client get a chance to open a
# socket. From here on, any outbound connection from THIS process to a
# non-LAN / publicly routable address is refused at connect() time and
# recorded (see app/security/egress_firewall.py). Loopback (incl. Ollama
# on 127.0.0.1:11434), RFC1918 / link-local, and the configured LAN peers
# keep working normally. This is the enforcement counterpart to the
# read-only sweep in app/monitor/network.py.
_ef_status = egress_firewall.install(
    enabled=EGRESS_FIREWALL["enabled"],
    extra_cidrs=EGRESS_FIREWALL["extra_allowed_cidrs"],
    extra_ips=EGRESS_FIREWALL["extra_allowed_ips"],
)
print(
    f"[SECURITY] egress firewall: "
    f"{'ENFORCING' if _ef_status['enforcing'] else 'DISABLED (config)'} "
    f"— off-LAN outbound connections from this process are "
    f"{'blocked' if _ef_status['enforcing'] else 'NOT blocked'}"
)

app = FastAPI(title="Sovereign On-Premise Agentic AI Workbench")
install_error_handlers(app)

# The real deployment always serves the frontend from this same app (same
# origin, so no CORS is actually needed for it). This middleware exists
# purely so a developer can preview frontend/ from a separate local static
# server (Live Server, `python -m http.server`, etc.) during frontend work
# without a CORS error — localhost/127.0.0.1 only, never the LAN, so it
# doesn't loosen anything about what the LAN itself can reach.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(network_router)

init_db()
init_accounts_db()


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Serve the frontend HTML entry points with `Cache-Control: no-cache`
    so a browser always revalidates them (StaticFiles' ETag then makes that
    a cheap 304 when unchanged). Without this, a browser can keep showing a
    stale index.html — and therefore keep requesting the old ?v= asset URLs
    — indefinitely after a frontend change ships. Hashed CSS/JS (?v=NNN) are
    left alone: their URL changes when their content does, so they stay
    safely cacheable."""
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path == "/" or "." not in path.rsplit("/", 1)[-1]:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/health")
async def health():
    """Liveness check for run.sh/monitoring — not part of the frozen §2.3/§2.4 contract."""
    return {"status": "ok"}


# Generated-file serving (§2.7a) — one physical directory, one URL prefix.
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")

# Frontend static serving (§2.7) — mounted last so it doesn't shadow /api or
# /files. Unchanged: the frontend already only ever calls relative /api/...
# and /files/... paths (see frontend/app.js's API_BASE), so serving it from
# this same single origin is exactly what makes "one LAN URL" true.
FRONTEND_DIR = REPO_ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    from .storage.config import BACKEND_PORT
    # 0.0.0.0 — this is the one and only LAN-facing bind in the whole system.
    uvicorn.run("app.main:app", host="0.0.0.0", port=BACKEND_PORT, reload=False)
