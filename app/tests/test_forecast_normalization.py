"""MOMENT forecasting: input standardization and its inverse.

MOMENT is trained on z-score-normalized windows, so a raw real-world series
has to be standardized before inference and mapped back afterwards. Without
that the forecast ignored the input's trend entirely — verified against the
running deployment: the clean ramp 10,12,...,24 (which should continue
26,28,30,32,34) came back as 18.3, 18.7, 16.0, 16.5, 15.8.

torch and the MOMENT weights are not installed on every dev machine, so the
tensor library and the model are both stubbed here — what's under test is
the normalize -> infer -> de-normalize arithmetic, which is ours.
"""
import sys
import types

import pytest


class _FakeTensor:
    def __init__(self, data):
        self.data = list(data)

    def reshape(self, *_a):
        return self

    def to(self, *_a):
        return self


class _FakeArray:
    def __init__(self, values):
        self.values = values

    def reshape(self, *_a):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.values


class _NoGrad:
    def __enter__(self):
        return None

    def __exit__(self, *_a):
        return False


@pytest.fixture
def forecast_module(monkeypatch):
    torch = types.ModuleType("torch")
    torch.tensor = lambda data, dtype=None: _FakeTensor(data)
    torch.float32 = "float32"
    torch.no_grad = _NoGrad
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch)

    from app.tools import forecast

    return forecast


def test_normalization_round_trip_preserves_real_world_scale(forecast_module, monkeypatch):
    """A model that perfectly continues the NORMALIZED trend must produce a
    forecast that continues the ORIGINAL series once de-normalized."""
    seen = {}

    def fake_model(x_enc=None):
        seen["input"] = x_enc.data
        step = x_enc.data[-1] - x_enc.data[-2]
        return types.SimpleNamespace(
            forecast=_FakeArray([x_enc.data[-1] + step * (i + 1) for i in range(5)])
        )

    monkeypatch.setattr(forecast_module, "_get_pipeline", lambda horizon: (fake_model, "cpu"))

    out = forecast_module.forecast_timeseries([10, 12, 14, 16, 18, 20, 22, 24], horizon=5)

    assert out["forecast"] == pytest.approx([26.0, 28.0, 30.0, 32.0, 34.0])
    assert out["input_length"] == 8

    # The model must actually receive standardized values, not raw magnitudes.
    fed = seen["input"]
    mean = sum(fed) / len(fed)
    std = (sum((v - mean) ** 2 for v in fed) / len(fed)) ** 0.5
    assert mean == pytest.approx(0.0, abs=1e-6)
    assert std == pytest.approx(1.0, abs=1e-6)


def test_flat_series_does_not_divide_by_zero(forecast_module, monkeypatch):
    """A constant series has std 0; dividing by it would yield NaN/inf and
    poison every forecast value."""
    def fake_model(x_enc=None):
        return types.SimpleNamespace(forecast=_FakeArray([0.0, 0.0, 0.0]))

    monkeypatch.setattr(forecast_module, "_get_pipeline", lambda horizon: (fake_model, "cpu"))

    out = forecast_module.forecast_timeseries([5.0] * 8, horizon=3)
    assert out["forecast"] == pytest.approx([5.0, 5.0, 5.0])
    assert all(v == v for v in out["forecast"])  # no NaN


def test_rejects_too_short_series(forecast_module):
    with pytest.raises(ValueError):
        forecast_module.forecast_timeseries([1.0], horizon=4)
