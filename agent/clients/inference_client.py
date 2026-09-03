import httpx
from typing import Optional


INFERENCE_URL = "http://10.58.114.70:11434/api/generate"


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