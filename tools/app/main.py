"""
Person C — Tools Service
Implements contract §2.6 exactly:
    POST /tools/execute-code
    POST /tools/search-docs
    POST /tools/generate-file

Runs on port 8001, localhost-only (never exposed to the LAN — §2.2).
Only ever called by Person F's Agent Loop (§2.6): "you only ever respond
to calls from Person F."
"""
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import TOOLS_PORT
from .errors import install_error_handlers
from . import sandbox
from . import docsearch
from . import filegen

app = FastAPI(title="Tools Service (Person C)")
install_error_handlers(app)


# ---------------------------------------------------------------------------
# POST /tools/execute-code
# ---------------------------------------------------------------------------

class ExecuteCodeRequest(BaseModel):
    code: str
    language: str = "python"


class ExecuteCodeResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


@app.post("/tools/execute-code", response_model=ExecuteCodeResponse)
def execute_code(req: ExecuteCodeRequest):
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="`code` must be a non-empty string")

    try:
        result = sandbox.run_code(req.code, req.language)
    except sandbox.UnsupportedLanguage as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ExecuteCodeResponse(**result)


# ---------------------------------------------------------------------------
# POST /tools/search-docs
# ---------------------------------------------------------------------------

class SearchDocsRequest(BaseModel):
    query: str
    top_k: int = 3


class SearchResultItem(BaseModel):
    text: str
    source: str
    score: float


class SearchDocsResponse(BaseModel):
    results: List[SearchResultItem]


@app.post("/tools/search-docs", response_model=SearchDocsResponse)
def search_docs(req: SearchDocsRequest):
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="`query` must be a non-empty string")
    if req.top_k < 1:
        raise HTTPException(status_code=400, detail="`top_k` must be >= 1")

    results = docsearch.search_docs(req.query, req.top_k)
    return SearchDocsResponse(results=[SearchResultItem(**r) for r in results])


# ---------------------------------------------------------------------------
# POST /tools/generate-file
# ---------------------------------------------------------------------------

class Section(BaseModel):
    heading: str
    body: str


class FileContent(BaseModel):
    title: str
    sections: List[Section]


class GenerateFileRequest(BaseModel):
    type: str  # "docx" | "xlsx" | "pptx"
    content: FileContent


class GenerateFileResponse(BaseModel):
    file_url: str
    file_name: str


@app.post("/tools/generate-file", response_model=GenerateFileResponse)
def generate_file(req: GenerateFileRequest):
    if req.type not in ("docx", "xlsx", "pptx"):
        raise HTTPException(
            status_code=400,
            detail=f"`type` must be one of docx|xlsx|pptx, got {req.type!r}",
        )

    try:
        result = filegen.generate_file(req.type, req.content.model_dump())
    except filegen.UnsupportedFileType as e:
        raise HTTPException(status_code=400, detail=str(e))

    return GenerateFileResponse(**result)


# ---------------------------------------------------------------------------
# Health check (not part of the contract, but useful for isolation testing
# per §2.10 item 6 and for Person E's integration checkpoints)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "docker_available": sandbox.check_docker_available(),
        "port": TOOLS_PORT,
    }


if __name__ == "__main__":
    import uvicorn
    # localhost-only binding — this service is never exposed to the LAN (§2.2)
    uvicorn.run("app.main:app", host="127.0.0.1", port=TOOLS_PORT, reload=False)
