"""
Client for Person C's Tools service.
Contract §2.6: three endpoints on port 8001 (localhost only).

Port resolution reads config["ports"]["tools"] (Person C's actual
config.json shape — see tools/app/config.py), falling back to a flat
"tools_port" key for backward compatibility, then the §2.2 default 8001.
Host is always "localhost" — Person C's service is never exposed to the
LAN (§2.2).
"""
import json
import os
import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.json")
TOOLS_HOST = "localhost"


def _get_tools_base_url() -> str:
    port = 8001
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        if "ports" in cfg and isinstance(cfg["ports"], dict) and "tools" in cfg["ports"]:
            port = cfg["ports"]["tools"]
        elif "tools_port" in cfg:
            port = cfg["tools_port"]
    except FileNotFoundError:
        pass
    return f"http://{TOOLS_HOST}:{port}"


async def execute_code(code: str, language: str = "python") -> dict:
    """
    Contract §2.6: POST /tools/execute-code
    Returns: { "stdout": str, "stderr": str, "exit_code": int }
    """
    url = f"{_get_tools_base_url()}/tools/execute-code"
    print(f"[TOOLS_CLIENT] -> execute_code url={url} language={language}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"code": code, "language": language})
        resp.raise_for_status()
        data = resp.json()
        print(f"[TOOLS_CLIENT] <- execute_code exit_code={data.get('exit_code')}")
        return data


async def search_docs(query: str, top_k: int = 3, chat_id: str | None = None) -> dict:
    """
    Contract §2.6: POST /tools/search-docs
    Returns: { "results": [ { "text": str, "source": str, "score": float,
                              "page": int|None } ] }

    `chat_id` (optional): when given, retrieval is restricted to that chat's
    uploaded Knowledge Base only. Omitted -> unchanged corpus-wide search.
    """
    url = f"{_get_tools_base_url()}/tools/search-docs"
    payload: dict = {"query": query, "top_k": top_k}
    if chat_id:
        payload["chat_id"] = chat_id
    print(f"[TOOLS_CLIENT] -> search_docs url={url} query={query!r} top_k={top_k} chat_id={chat_id!r}")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        print(f"[TOOLS_CLIENT] <- search_docs result_count={len(data.get('results', []))}")
        return data


async def generate_file(file_type: str, content: dict) -> dict:
    """
    Contract §2.6 / §2.7a: POST /tools/generate-file
    file_type: "docx" | "xlsx" | "pptx"
    content: { "title": str, "sections": [ {"heading": str, "body": str} ] }
    Returns { "file_url": str, "file_name": str } — pass file_url straight
    through unchanged into your own response (§2.7a).
    """
    url = f"{_get_tools_base_url()}/tools/generate-file"
    print(f"[TOOLS_CLIENT] -> generate_file url={url} file_type={file_type} title={content.get('title')!r}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"type": file_type, "content": content})
        resp.raise_for_status()
        data = resp.json()
        print(f"[TOOLS_CLIENT] <- generate_file file_url={data.get('file_url')}")
        return data
