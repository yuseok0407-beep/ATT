import numpy as np
import pandas as pd

from src.core.reversion_strategy import detect_signal


def _flat_df(n=60, level=100.0):
    closes = np.full(n, level)
    return pd.DataFrame({"high": closes + 0.1, "low": closes - 0.1, "close": closes})


def _lower_band_breach_df(n=60):
    """평평한 구간 위에서 마지막 봉만 급락시켜 하단밴드를 막 하향 돌파하는 시나리오.
    급락폭을 크게 잡아 RSI도 자연히 과매도 구간으로 들어가게 한다."""
    closes = np.full(n, 100.0)
    closes[-1] = 90.0
    return pd.DataFrame({"high": closes + 0.1, "low": closes - 0.1, "close": closes})


def _upper_band_breach_df(n=60):
    closes = np.full(n, 100.0)
    closes[-1] = 110.0
    return pd.DataFrame({"high": closes + 0.1, "low": closes - 0.1, "close": closes})


def test_detect_signal_returns_none_with_too_few_bars():
    df = _lower_band_breach_df(n=10)
    assert detect_signal(df, adx_threshold=None) is None


def test_detect_signal_none_when_no_band_breach():
    df = _flat_df()
    assert detect_signal(df, adx_threshold=None) is None


def test_detect_signal_long_on_lower_band_breach_with_oversold_rsi():
    df = _lower_band_breach_df()
    signal = detect_signal(df, adx_threshold=None)
    assert signal == "LONG"


def test_detect_signal_short_on_upper_band_breach_with_overbought_rsi():
    df = _upper_band_breach_df()
    signal = detect_signal(df, adx_threshold=None)
    assert signal == "SHORT"


def test_detect_signal_no_repeat_signal_while_camped_below_band():
    # 하단밴드 아래로 이미 한 봉 전에 돌파해서 계속 머무는 상황 -> 새 돌파 "순간"이 아니므로 신호 없음
    closes = np.full(61, 100.0)
    closes[-2] = 90.0
    closes[-1] = 89.0
    df = pd.DataFrame({"high": closes + 0.1, "low": closes - 0.1, "close": closes})
    assert detect_signal(df, adx_threshold=None) is None


def test_detect_signal_blocked_by_adx_regime_filter():
    df = _lower_band_breach_df()
    # 레짐 필터 없이는(adx_threshold=None) 신호가 나야 정상
    assert detect_signal(df, adx_threshold=None) == "LONG"
    # ADX는 항상 0 이상이므로 threshold=0으로 걸면 어떤 구간이든 "추세 있음"으로 간주돼 막혀야 정상
    assert detect_signal(df, adx_threshold=0.0) is None
