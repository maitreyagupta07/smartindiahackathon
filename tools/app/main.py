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
from . import pdf_ingest

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
    # Optional chat-scoped retrieval. When provided, the search runs against
    # the `chat_kb` collection filtered to this chat_id ONLY — documents
    # uploaded in any other chat are never returned. Omitted -> unchanged
    # behavior: search the shared local document corpus (`mrpl_docs`).
    chat_id: Optional[str] = None


class SearchResultItem(BaseModel):
    text: str
    source: str
    score: float
    page: Optional[int] = None
    document_id: Optional[str] = None


class SearchDocsResponse(BaseModel):
    results: List[SearchResultItem]


@app.post("/tools/search-docs", response_model=SearchDocsResponse)
def search_docs(req: SearchDocsRequest):
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="`query` must be a non-empty string")
    if req.top_k < 1:
        raise HTTPException(status_code=400, detail="`top_k` must be >= 1")

    if req.chat_id and req.chat_id.strip():
        results = docsearch.search_chat_docs(req.query, req.chat_id, req.top_k)
    else:
        results = docsearch.search_docs(req.query, req.top_k)
    return SearchDocsResponse(results=[SearchResultItem(**r) for r in results])


# ---------------------------------------------------------------------------
# POST /tools/ingest-doc  — chat-scoped Knowledge Base ingestion
# ---------------------------------------------------------------------------

class IngestDocRequest(BaseModel):
    chat_id: str
    filename: str
    file_base64: str
    document_id: Optional[str] = None
    mime_type: Optional[str] = None
    chat_title: Optional[str] = None


class IngestDocResponse(BaseModel):
    document_id: str
    filename: str
    chat_id: str
    chunks: int
    status: str


@app.post("/tools/ingest-doc", response_model=IngestDocResponse)
def ingest_doc(req: IngestDocRequest):
    if not req.chat_id or not req.chat_id.strip():
        raise HTTPException(status_code=400, detail="`chat_id` is required")
    if not req.filename or not req.filename.strip():
        raise HTTPException(status_code=400, detail="`filename` is required")
    if not req.file_base64:
        raise HTTPException(status_code=400, detail="`file_base64` is empty")
    if not pdf_ingest.is_pdf(req.mime_type, req.filename):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type {req.mime_type or req.filename!r} — only PDF is supported",
        )

    try:
        pages = pdf_ingest.extract_pdf_pages(req.file_base64)
    except pdf_ingest.PdfExtractionError as e:
        raise HTTPException(status_code=400, detail=f"could not read PDF: {e}")

    if not pages:
        raise HTTPException(
            status_code=400,
            detail="no extractable text found in PDF (empty, or a scanned PDF with no OCR available)",
        )

    try:
        result = docsearch.ingest_chat_document(
            chat_id=req.chat_id,
            filename=req.filename,
            pages=pages,
            document_id=req.document_id,
            chat_title=req.chat_title,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return IngestDocResponse(**result)


# ---------------------------------------------------------------------------
# GET /tools/kb-documents  — chat KB listing for Admin -> Knowledge Base
# ---------------------------------------------------------------------------

class KbDocumentItem(BaseModel):
    document_id: str
    filename: str
    file_type: str
    chat_id: Optional[str] = None
    chat_title: Optional[str] = None
    uploaded_at: Optional[str] = None
    chunks: int
    status: str


class KbDocumentsResponse(BaseModel):
    documents: List[KbDocumentItem]


@app.get("/tools/kb-documents", response_model=KbDocumentsResponse)
def kb_documents(chat_id: Optional[str] = None):
    docs = docsearch.list_chat_documents(chat_id)
    return KbDocumentsResponse(documents=[KbDocumentItem(**d) for d in docs])


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
