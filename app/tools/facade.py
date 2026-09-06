"""
Direct in-process entry points into the tools layer (code execution, doc
search, file generation) — replaces the old agent/clients/tools_client.py,
which made this same functionality available over HTTP to a separate
localhost:8001 Tools service. That service no longer exists as its own
process after the single-node refactor; app/agent/loop.py now calls these
functions directly (same names, same argument shapes, same return shapes —
callers did not need to change).

Every underlying tools function (sandbox.run_code, docsearch.search_docs /
search_chat_docs, filegen.generate_file) is synchronous, blocking Python —
real Docker API calls, ChromaDB queries, python-docx/openpyxl file writes —
wrapped in asyncio.to_thread() here so a slow tool call can't stall the
event loop for other in-flight requests. This is what keeps contract
§2.4's "must not serialize" rule true after collapsing three processes
into one: previously each blocking call ran in ITS OWN process/thread-pool
(uvicorn's own worker threads for the old Tools service); now they all
share this one process's event loop, so the to_thread() offload is what
takes over that same job.
"""
import asyncio
from typing import Optional

from . import sandbox
from . import docsearch
from . import filegen
from . import pdf_ingest


class DocumentIngestError(Exception):
    """Raised for a bad/empty/corrupt PDF or missing required field — the
    caller (app/api/chat.py) turns this into the same 400 the old
    /tools/ingest-doc endpoint returned."""


async def execute_code(code: str, language: str = "python") -> dict:
    """Contract §2.6 shape: {"stdout": str, "stderr": str, "exit_code": int}."""
    print(f"[TOOLS] execute_code language={language}")
    result = await asyncio.to_thread(sandbox.run_code, code, language)
    print(f"[TOOLS] execute_code done exit_code={result.get('exit_code')}")
    return result


async def search_docs(query: str, top_k: int = 3, chat_id: str | None = None) -> dict:
    """Contract §2.6 shape: {"results": [{"text","source","score","page"?}, ...]}."""
    print(f"[TOOLS] search_docs query={query!r} top_k={top_k} chat_id={chat_id!r}")
    if chat_id:
        results = await asyncio.to_thread(docsearch.search_chat_docs, query, chat_id, top_k)
    else:
        results = await asyncio.to_thread(docsearch.search_docs, query, top_k)
    print(f"[TOOLS] search_docs result_count={len(results)}")
    return {"results": results}


async def generate_file(file_type: str, content: dict) -> dict:
    """Contract §2.6/§2.7a shape: {"file_url": "/files/<name>", "file_name": str}."""
    print(f"[TOOLS] generate_file file_type={file_type} title={content.get('title')!r}")
    result = await asyncio.to_thread(filegen.generate_file, file_type, content)
    print(f"[TOOLS] generate_file done file_url={result.get('file_url')}")
    return result


def _ingest_document_sync(
    chat_id: str,
    filename: str,
    file_base64: str,
    mime_type: Optional[str],
    document_id: Optional[str],
    chat_title: Optional[str],
) -> dict:
    if not pdf_ingest.is_pdf(mime_type, filename):
        raise DocumentIngestError(
            f"unsupported file type {mime_type or filename!r} — only PDF is supported"
        )
    try:
        pages = pdf_ingest.extract_pdf_pages(file_base64)
    except pdf_ingest.PdfExtractionError as e:
        raise DocumentIngestError(f"could not read PDF: {e}")
    if not pages:
        raise DocumentIngestError(
            "no extractable text found in PDF (empty, or a scanned PDF with no OCR available)"
        )
    return docsearch.ingest_chat_document(
        chat_id=chat_id, filename=filename, pages=pages,
        document_id=document_id, chat_title=chat_title,
    )


async def ingest_document(
    chat_id: str,
    filename: str,
    file_base64: str,
    mime_type: Optional[str] = None,
    document_id: Optional[str] = None,
    chat_title: Optional[str] = None,
) -> dict:
    """Contract-equivalent of the old POST /tools/ingest-doc — chat-scoped
    Knowledge Base ingestion. Returns {"document_id","filename","chat_id",
    "chunks","status"}. Raises DocumentIngestError for a bad/empty/corrupt
    PDF (caller maps that to a 400, same as before)."""
    print(f"[TOOLS] ingest_document chat_id={chat_id!r} filename={filename!r}")
    result = await asyncio.to_thread(
        _ingest_document_sync, chat_id, filename, file_base64, mime_type, document_id, chat_title,
    )
    print(f"[TOOLS] ingest_document done chunks={result.get('chunks')} status={result.get('status')}")
    return result


async def list_kb_documents(chat_id: Optional[str] = None) -> list[dict]:
    """Contract-equivalent of the old GET /tools/kb-documents."""
    return await asyncio.to_thread(docsearch.list_chat_documents, chat_id)
