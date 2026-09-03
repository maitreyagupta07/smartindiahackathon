import json
from pathlib import Path
from typing import Optional

import httpx


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

INFERENCE_HOST = CONFIG["inference_host"]
INFERENCE_PORT = CONFIG["ports"]["inference"]

INFERENCE_URL = (
    f"http://{INFERENCE_HOST}:{INFERENCE_PORT}/api/generate"
)


async def call_inference(
    model: str,
    prompt: str,
    image_base64: Optional[str] = None
) -> str:

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False
    }

    if image_base64:
        payload["images"] = [image_base64]

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            INFERENCE_URL,
            json=payload
        )

        resp.raise_for_status()

        data = resp.json()

        return data["response"]