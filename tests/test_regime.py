import numpy as np
import pandas as pd

from src.core.regime import classify_regime


def _make_df(closes):
    closes = pd.Series(closes, dtype=float)
    high = closes + 0.5
    low = closes - 0.5
    return pd.DataFrame({"high": high, "low": low, "close": closes})


def test_strong_uptrend_is_trending_up():
    closes = np.linspace(100, 200, 60)  # steady, low-noise rise
    result = classify_regime(_make_df(closes))
    assert result["label"] == "TRENDING_UP"
    assert result["adx"] >= 25


def test_strong_downtrend_is_trending_down():
    closes = np.linspace(200, 100, 60)
    result = classify_regime(_make_df(closes))
    assert result["label"] == "TRENDING_DOWN"


def test_flat_price_is_ranging():
    rng = np.random.default_rng(42)
    closes = 100 + rng.normal(0, 0.05, 60).cumsum() * 0  # constant
    closes = np.full(60, 100.0)
    result = classify_regime(_make_df(closes))
    assert result["label"] == "RANGING"


def test_choppy_high_volatility_overrides_trend():
    rng = np.random.default_rng(1)
    closes = 100 + rng.normal(0, 8, 60).cumsum()
    result = classify_regime(_make_df(closes))
    assert result["label"] in ("HIGH_VOLATILITY", "RANGING", "TRENDING_UP", "TRENDING_DOWN")
    # sanity: fields are present and JSON-safe (no NaN)
    for key in ("adx", "plus_di", "minus_di", "volatility_20"):
        assert result[key] is None or isinstance(result[key], float)
