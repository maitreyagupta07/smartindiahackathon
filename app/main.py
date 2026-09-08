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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.errors import install_error_handlers
from .api.tasks import router as tasks_router
from .api.chat import router as chat_router
from .api.knowledge import router as knowledge_router
from .audit.log import init_db
from .storage.config import FILES_DIR, REPO_ROOT

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

app.include_router(tasks_router)
app.include_router(chat_router)
app.include_router(knowledge_router)

init_db()


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
