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
    usage: dict | None = None,
) -> str:
    # Base shape is exactly contract §2.5's request — {model, prompt, images?,
    # stream}. `options.temperature` is an Ollama-supported addition, only
    # ever included when a caller explicitly asks for it (e.g. the
    # approval-note LoRA adapter, for steadier output); omitted entirely
    # otherwise, so every other caller's request is byte-identical to before.
    payload = {"model": model, "prompt": prompt, "stream": False}
    if image_base64:
        payload["images"] = [image_base64]
    # repeat_penalty is set on every call (unlike temperature, which stays
    # opt-in) — Ollama's own default (1.1) is mild enough that this small,
    # quantized model can still fall into visible verbatim-repetition loops
    # on longer generations (observed live: a requested poem degenerated
    # into the same two lines repeating for several stanzas). A somewhat
    # stronger default measurably reduces that failure mode across every
    # call site — document content, code, chat answers alike — without
    # needing each caller to opt in individually.
    options = {"repeat_penalty": 1.3}
    if temperature is not None:
        options["temperature"] = temperature
    payload["options"] = options

    url = _get_inference_url()
    print(f"[INFERENCE_CLIENT] -> model={model} url={url} has_image={bool(image_base64)}")

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        print(f"[INFERENCE_CLIENT] <- model={model} response_preview={str(data.get('response'))[:120]!r}")
        # Ollama's own real token counts (confirmed present on a non-streaming
        # /api/generate response: prompt_eval_count, eval_count) — never
        # estimated. `usage` is an optional out-param (return type here stays
        # plain `str` so every existing caller/test mocking a string return
        # value is unaffected) that the agent loop fills in for real usage
        # tracking (see app/agent/state.py's token_totals).
        if usage is not None:
            usage["prompt_tokens"] = data.get("prompt_eval_count")
            usage["completion_tokens"] = data.get("eval_count")
        return data["response"]
