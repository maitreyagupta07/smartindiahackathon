"""
Local text-to-image generation via a locally-downloaded SD Turbo pipeline
(stabilityai/sd-turbo, weights already on disk under extra_models/sd-turbo
— no internet access at call time, consistent with the air-gapped design).

Not one of the three tools in the original §2.6 contract (execute_code,
search_docs, generate_file) — this is a genuine fourth capability added on
top of it, following the exact same pattern every other tool already uses:
one small module here, one thin async wrapper in facade.py, one entry in
loop.py's tool_dispatch, one branch in planner.py. Swapping which image
model backs this later (e.g. SD Turbo -> SDXL-Turbo) is then a one-line
change to _MODEL_DIR/_get_pipeline, exactly like model_registry.py's
TEXT_MODEL/VISION_MODEL swaps — same reason those are cheap: one call
shape, one place that knows the model name.
"""
import uuid

from ..storage.config import FILES_DIR, REPO_ROOT

_MODEL_DIR = REPO_ROOT / "extra_models" / "sd-turbo"

# Loaded lazily, once, on first use — not at import time. Importing this
# module must stay cheap (torch/diffusers are heavy, GPU-touching imports)
# so a machine that never asks for an image never pays that cost, and unit
# tests that import planner.py/facade.py don't need torch installed at all.
_pipe = None


class ImageGenUnavailable(Exception):
    """The local SD Turbo weights aren't present, or torch/diffusers aren't
    installed on this machine — surfaced as a normal tool error (§2.8
    shape), never a silent failure."""


def _get_pipeline():
    global _pipe
    if _pipe is not None:
        return _pipe

    if not _MODEL_DIR.exists():
        raise ImageGenUnavailable(
            f"SD Turbo weights not found at {_MODEL_DIR} — download "
            f"stabilityai/sd-turbo into extra_models/sd-turbo first."
        )
    try:
        import torch
        from diffusers import AutoPipelineForText2Image
    except ImportError as exc:
        raise ImageGenUnavailable(
            "torch/diffusers are not installed — run "
            "`pip install torch diffusers` in this project's venv."
        ) from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    print(f"[IMAGEGEN] loading SD Turbo from {_MODEL_DIR} onto device={device} dtype={dtype}")
    _pipe = AutoPipelineForText2Image.from_pretrained(
        str(_MODEL_DIR), torch_dtype=dtype, safety_checker=None,
    ).to(device)
    return _pipe


def generate_image(prompt: str, num_inference_steps: int = 1) -> dict:
    """
    Returns: {"file_url": "/files/<name>.png", "file_name": "<name>.png"} —
    same shape/FILES_DIR convention as filegen.generate_file's response
    (§2.7a): a path under the shared FILES_DIR, servable at /files/... on
    the one LAN-facing port, never a direct localhost:PORT URL.

    SD Turbo is a distilled, few-step model — 1 step (its trained regime,
    guidance_scale=0.0) is enough for a real image and keeps this fast
    enough for a live demo even on modest hardware.
    """
    pipe = _get_pipeline()
    print(f"[IMAGEGEN] generating image prompt={prompt!r} steps={num_inference_steps}")
    image = pipe(prompt=prompt, num_inference_steps=num_inference_steps, guidance_scale=0.0).images[0]

    file_name = f"{uuid.uuid4().hex[:8]}-generated-image.png"
    out_path = FILES_DIR / file_name
    image.save(out_path)
    print(f"[IMAGEGEN] saved -> {out_path}")

    return {"file_url": f"/files/{file_name}", "file_name": file_name}


def check_imagegen_available() -> bool:
    return _MODEL_DIR.exists()
