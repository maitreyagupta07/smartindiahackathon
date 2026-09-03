"""
Client for Person C's Tools service.
Contract §2.6: three endpoints on port 8001 (localhost only).
"""
import json
import os
import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.json")


def _get_tools_base_url() -> str:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        port = cfg.get("tools_port", 8001)
    except FileNotFoundError:
        port = 8001
    return f"http://localhost:{port}"


async def execute_code(code: str, language: str = "python") -> dict:
    url = f"{_get_tools_base_url()}/tools/execute-code"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"code": code, "language": language})
        resp.raise_for_status()
        return resp.json()


async def search_docs(query: str, top_k: int = 3) -> dict:
    url = f"{_get_tools_base_url()}/tools/search-docs"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json={"query": query, "top_k": top_k})
        resp.raise_for_status()
        return resp.json()


async def generate_file(file_type: str, content: dict) -> dict:
    """
    file_type: "docx" | "xlsx" | "pptx"
    content: { "title": str, "sections": [ {"heading": str, "body": str} ] }
    Returns { "file_url": str, "file_name": str } — pass file_url straight
    through unchanged into your own response (§2.7a).
    """
    url = f"{_get_tools_base_url()}/tools/generate-file"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"type": file_type, "content": content})
        resp.raise_for_status()
        return resp.json()
