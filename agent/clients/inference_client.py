"""
Client for Person A's Ollama inference server.
Contract §2.5: POST http://localhost:11434/api/generate
"""
import json
import os
import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.json")


def _get_inference_url() -> str:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        host = cfg.get("inference_host", "localhost")
        port = cfg.get("inference_port", 11434)
    except FileNotFoundError:
        host = "localhost"
        port = 11434
    return f"http://{host}:{port}/api/generate"


async def call_inference(model: str, prompt: str, image_base64: str | None = None) -> str:
    payload = {"model": model, "prompt": prompt, "stream": False}
    if image_base64:
        payload["images"] = [image_base64]

    url = _get_inference_url()
    print(f"[INFERENCE_CLIENT] -> model={model} url={url} has_image={bool(image_base64)}")

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        print(f"[INFERENCE_CLIENT] <- model={model} response_preview={str(data.get('response'))[:120]!r}")
        return data["response"]
