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

    # This model MUST run on the GPU whenever one is visible. A CPU fallback
    # still works but is ~15-30x slower, so it is a loud warning, never a
    # silent downgrade.
    if torch.cuda.is_available():
        device = "cuda"
        print(
            f"[IMAGEGEN] CUDA OK — {torch.cuda.get_device_name(0)} "
            f"(torch {torch.__version__}); SD Turbo will run on GPU"
        )
    else:
        device = "cpu"
        print(
            "[IMAGEGEN] WARNING: torch.cuda.is_available() is False — SD Turbo "
            "will run on CPU (slow). Install a CUDA build of torch to fix."
        )

    # Always the fp16 weights. `variant="fp16"` is what actually matters:
    # without it, diffusers loads the full-precision (fp32) files by DEFAULT
    # regardless of torch_dtype, then casts down — doubling load time/IO and
    # needing fp32 files that aren't on disk. On CPU, fall back to fp32 math
    # (fp16 matmul is unsupported on many CPUs) while still loading fp16 weights.
    dtype = torch.float16 if device == "cuda" else torch.float32
    print(f"[IMAGEGEN] loading SD Turbo from {_MODEL_DIR} onto device={device} dtype={dtype}")
    pipe = AutoPipelineForText2Image.from_pretrained(
        str(_MODEL_DIR), torch_dtype=dtype, variant="fp16", safety_checker=None,
    ).to(device)

    if device == "cuda":
        # ERSA shares an 8 GB card with Ollama's resident text+vision models.
        # These slicing options cut SD Turbo's peak VRAM enough that a
        # generation can't OOM against those — negligible cost at 1 step.
        try:
            pipe.enable_attention_slicing()
            pipe.enable_vae_slicing()
        except Exception as exc:  # noqa: BLE001 - optional, never fatal
            print(f"[IMAGEGEN] (attention/vae slicing unavailable: {exc})")
        try:
            pipe.set_progress_bar_config(disable=True)
        except Exception:  # noqa: BLE001
            pass

    _pipe = pipe
    _pipe._ersa_device = device  # noqa: SLF001 - read back in generate_image for logging
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
    device = getattr(pipe, "_ersa_device", "cpu")
    print(f"[IMAGEGEN] generating image on {device} prompt={prompt!r} steps={num_inference_steps}")
    image = pipe(prompt=prompt, num_inference_steps=num_inference_steps, guidance_scale=0.0).images[0]

    file_name = f"{uuid.uuid4().hex[:8]}-generated-image.png"
    out_path = FILES_DIR / file_name
    image.save(out_path)
    print(f"[IMAGEGEN] saved -> {out_path}")

    # Hand the VRAM back so the next Ollama call / forecast isn't squeezed.
    if device == "cuda":
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    return {"file_url": f"/files/{file_name}", "file_name": file_name}


def check_imagegen_available() -> bool:
    return _MODEL_DIR.exists()
