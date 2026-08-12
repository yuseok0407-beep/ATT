import numpy as np
import pandas as pd
import pytest

from src.core.indicators import atr, bollinger_bands, ema, rsi, sma, volatility


def test_sma_basic():
    close = pd.Series([1, 2, 3, 4, 5])
    result = sma(close, 3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == 2.0
    assert result.iloc[4] == 4.0


def test_ema_constant_price_equals_that_price():
    close = pd.Series([100.0] * 20)
    result = ema(close, 10)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_ema_reacts_faster_than_sma_to_a_recent_jump():
    close = pd.Series([100.0] * 15 + [110.0] * 5)
    ema_result = ema(close, 10)
    sma_result = sma(close, 10)
    # 최근 급등 이후 EMA가 SMA보다 더 최근 가격(110)에 가깝게 붙어야 한다
    assert abs(ema_result.iloc[-1] - 110.0) < abs(sma_result.iloc[-1] - 110.0)


def test_rsi_all_gains_is_100():
    close = pd.Series(range(1, 30))
    result = rsi(close, 14)
    assert result.iloc[-1] == 100.0


def test_rsi_all_losses_is_0():
    close = pd.Series(range(30, 1, -1))
    result = rsi(close, 14)
    assert result.iloc[-1] == 0.0


def test_atr_nonnegative():
    df = pd.DataFrame({
        "high": [10, 11, 12, 11, 13],
        "low": [9, 9, 10, 9, 11],
        "close": [9.5, 10.5, 11, 10, 12],
    })
    result = atr(df, 3)
    assert (result.dropna() >= 0).all()


def test_volatility_zero_for_constant_price():
    close = pd.Series([100] * 30)
    result = volatility(close, 20)
    assert result.iloc[-1] == 0.0


def test_bollinger_bands_zero_width_for_constant_price():
    close = pd.Series([100.0] * 25)
    bands = bollinger_bands(close, period=20, num_std=2.0)
    assert bands["upper"].iloc[-1] == pytest.approx(100.0)
    assert bands["lower"].iloc[-1] == pytest.approx(100.0)
    assert bands["middle"].iloc[-1] == pytest.approx(100.0)


def test_bollinger_bands_widen_with_volatility():
    close = pd.Series([100, 90, 110, 95, 105] * 5)
    bands = bollinger_bands(close, period=20, num_std=2.0)
    assert bands["upper"].iloc[-1] > bands["middle"].iloc[-1] > bands["lower"].iloc[-1]
