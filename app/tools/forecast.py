"""
Local time-series forecasting via a locally-downloaded MOMENT-1-small
pipeline (AutonLab/MOMENT-1-small, weights already on disk under
extra_models/moment-1-small — no internet access at call time).

Same pattern as imagegen.py / every other tool: one module here, one thin
async wrapper in facade.py, one entry in loop.py's tool_dispatch, one
branch in planner.py. Not part of the original §2.6 contract — a genuine
fifth capability added the same additive way generate_image was.
"""
from ..storage.config import REPO_ROOT

_MODEL_DIR = REPO_ROOT / "extra_models" / "moment-1-small"

# MOMENT-1-small's forecasting head is trained on a fixed 512-timestep
# context window — this is a property of the model, not a tunable
# parameter. A shorter user-supplied series is tiled (repeated) up to this
# length before inference; tiling (rather than zero/edge padding) keeps
# whatever periodicity is in the input visible to the model instead of
# diluting it with flat padding.
_CONTEXT_LENGTH = 512

_pipeline = None


class ForecastUnavailable(Exception):
    """The local MOMENT-1-small weights aren't present, or torch/momentfm
    aren't installed — surfaced as a normal tool error (§2.8 shape)."""


def _get_pipeline(horizon: int):
    global _pipeline
    # Cached per-horizon: MOMENT's forecasting head is configured with a
    # fixed forecast_horizon at construction time, so a different horizon
    # needs its own instance rather than silently reusing a mismatched one.
    if _pipeline is not None and _pipeline[0] == horizon:
        return _pipeline[1], _pipeline[2]

    if not _MODEL_DIR.exists():
        raise ForecastUnavailable(
            f"MOMENT-1-small weights not found at {_MODEL_DIR} — download "
            f"AutonLab/MOMENT-1-small into extra_models/moment-1-small first."
        )
    try:
        import torch
        from momentfm import MOMENTPipeline
    except ImportError as exc:
        raise ForecastUnavailable(
            "momentfm/torch are not installed — run "
            "`pip install torch momentfm` in this project's venv."
        ) from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[FORECAST] loading MOMENT-1-small from {_MODEL_DIR} horizon={horizon} device={device}")
    model = MOMENTPipeline.from_pretrained(
        str(_MODEL_DIR),
        model_kwargs={"task_name": "forecasting", "forecast_horizon": horizon},
        local_files_only=True,
    )
    model.init()
    # from_pretrained/init() don't move the model to the GPU on their own
    # (unlike diffusers' from_pretrained(...).to(device) pattern in
    # imagegen.py) — without this explicit .to(), MOMENT silently runs on
    # CPU even with a CUDA-enabled torch installed and a free GPU sitting
    # right there.
    model = model.to(device)
    _pipeline = (horizon, model, device)
    return model, device


def _tile_to_context(data: list, length: int = _CONTEXT_LENGTH) -> list:
    if len(data) >= length:
        return data[-length:]
    reps = (length // len(data)) + 1
    return (data * reps)[:length]


def forecast_timeseries(data: list, horizon: int = 8) -> dict:
    """
    Returns: {"input_length": int, "horizon": int, "forecast": [float, ...]}

    `data`: the raw numeric series as given (not the tiled/padded version)
    — kept separate from what's actually fed to the model so a caller can
    always see exactly what it asked for.
    """
    if not data or len(data) < 2:
        raise ValueError("forecast_timeseries needs at least 2 numeric data points")

    import torch

    model, device = _get_pipeline(horizon)
    values = [float(x) for x in data]
    series = _tile_to_context(values)

    # MOMENT is trained on z-score-normalized windows, so raw real-world
    # magnitudes have to be standardized before inference and the output
    # mapped back afterwards. Without this the model was returning values
    # that ignored the input entirely — verified live: the clean linear ramp
    # 10,12,...,24 (which should continue ~26,28,30,32,34) came back as
    # 18.3, 18.7, 16.0, 16.5, 15.8 — decreasing, and clustered around the
    # input's own mean rather than following its trend. Standardizing on the
    # ORIGINAL series (not the tiled copy) keeps mean/std the statistics of
    # the data the user actually supplied.
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = var ** 0.5
    # A perfectly flat series has std 0 — dividing by it would produce
    # NaN/inf and poison the whole forecast. Fall back to 1.0, which makes
    # normalization a pure mean-shift and still round-trips exactly.
    if std < 1e-8:
        std = 1.0
    normalized = [(v - mean) / std for v in series]

    x_enc = torch.tensor(normalized, dtype=torch.float32).reshape(1, 1, -1).to(device)
    print(
        f"[FORECAST] running inference input_len={len(data)} tiled_len={len(series)} "
        f"horizon={horizon} device={device} mean={mean:.4f} std={std:.4f}"
    )
    with torch.no_grad():
        output = model(x_enc=x_enc)
    raw = output.forecast.reshape(-1).cpu().tolist()
    # Invert the normalization so the forecast comes back in the user's own units.
    forecast = [v * std + mean for v in raw]

    return {"input_length": len(data), "horizon": horizon, "forecast": [round(v, 4) for v in forecast]}


def check_forecast_available() -> bool:
    return _MODEL_DIR.exists()
