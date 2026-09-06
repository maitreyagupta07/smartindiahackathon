"""
Client for the local Ollama inference server.
Contract §2.5: POST http://localhost:11434/api/generate

Ollama stays a separate local OS process even after the single-node
refactor (it's a third-party binary, not our code) — this is the one
internal HTTP hop that's genuinely still an HTTP hop, invoked only from
inside this app, never exposed to the LAN.
"""
import httpx

from ..storage.config import INFERENCE_HOST, INFERENCE_PORT


def _get_inference_url() -> str:
    return f"http://{INFERENCE_HOST}:{INFERENCE_PORT}/api/generate"


async def call_inference(
    model: str,
    prompt: str,
    image_base64: str | None = None,
    temperature: float | None = None,
) -> str:
    # Base shape is exactly contract §2.5's request — {model, prompt, images?,
    # stream}. `options.temperature` is an Ollama-supported addition, only
    # ever included when a caller explicitly asks for it (e.g. the
    # approval-note LoRA adapter, for steadier output); omitted entirely
    # otherwise, so every other caller's request is byte-identical to before.
    payload = {"model": model, "prompt": prompt, "stream": False}
    if image_base64:
        payload["images"] = [image_base64]
    if temperature is not None:
        payload["options"] = {"temperature": temperature}

    url = _get_inference_url()
    print(f"[INFERENCE_CLIENT] -> model={model} url={url} has_image={bool(image_base64)}")

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        print(f"[INFERENCE_CLIENT] <- model={model} response_preview={str(data.get('response'))[:120]!r}")
        return data["response"]
