"""
POST /tools/search-docs  (contract §2.6)

Local vector search over a small sample corpus of SOPs/manuals using
ChromaDB with its default (local, on-disk, no separate server process)
embedding + storage. Everything here is local-only — no external API calls,
consistent with the air-gapped requirement.
"""
from pathlib import Path

import chromadb

from .config import REPO_ROOT

CORPUS_DIR = REPO_ROOT / "tools" / "docs_corpus"
CHROMA_DIR = REPO_ROOT / "tools" / "chroma_db"
COLLECTION_NAME = "mrpl_docs"

_client = None
_collection = None


def _chunk_text(text: str, chunk_size: int = 800, overlap: int = 100):
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return [c.strip() for c in chunks if c.strip()]


def _load_corpus_into(collection):
    """Index every .txt/.md file in CORPUS_DIR, idempotently."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    existing = set()
    try:
        existing = set(collection.get(include=[])["ids"])
    except Exception:
        pass

    files = list(CORPUS_DIR.glob("*.txt")) + list(CORPUS_DIR.glob("*.md"))
    ids, docs, metadatas = [], [], []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, chunk in enumerate(_chunk_text(text)):
            chunk_id = f"{path.name}::{i}"
            if chunk_id in existing:
                continue
            ids.append(chunk_id)
            docs.append(chunk)
            metadatas.append({"source": path.name})

    if ids:
        collection.add(ids=ids, documents=docs, metadatas=metadatas)


def get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    _collection = _client.get_or_create_collection(name=COLLECTION_NAME)
    _load_corpus_into(_collection)
    return _collection


def search_docs(query: str, top_k: int = 3) -> list[dict]:
    collection = get_collection()
    count = collection.count()
    if count == 0:
        return []

    top_k = max(1, min(top_k, count))
    result = collection.query(query_texts=[query], n_results=top_k)

    out = []
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    dists = result.get("distances", [[]])[0]
    for text, meta, dist in zip(docs, metas, dists):
        # Chroma returns a distance (lower = closer); convert to a
        # 0-1 "similarity-ish" score for the contract's `score` field.
        score = 1.0 / (1.0 + dist) if dist is not None else 0.0
        out.append({
            "text": text,
            "source": (meta or {}).get("source", "unknown"),
            "score": round(float(score), 4),
        })
    return out
