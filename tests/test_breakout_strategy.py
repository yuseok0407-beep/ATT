import numpy as np
import pandas as pd

from src.core.breakout_strategy import detect_signal


def _flat_df(n=40, level=100.0):
    closes = np.full(n, level)
    return pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})


def _upward_breakout_df(n=40, channel_high=100.5):
    """channel_period(기본 20)개 봉은 채널 상단이 channel_high가 되도록 평평하게 두고,
    마지막 봉만 그 위로 확실히 뚫고 올라가는 시나리오."""
    closes = np.full(n, 100.0)
    closes[-1] = channel_high + 1.0
    df = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})
    df.loc[df.index[-1], "high"] = channel_high + 1.5
    return df


def _downward_breakout_df(n=40, channel_low=99.5):
    closes = np.full(n, 100.0)
    closes[-1] = channel_low - 1.0
    df = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})
    df.loc[df.index[-1], "low"] = channel_low - 1.5
    return df


def test_detect_signal_returns_none_with_too_few_bars():
    df = _upward_breakout_df(n=10)
    assert detect_signal(df, channel_period=20) is None


def test_detect_signal_none_when_price_stays_inside_channel():
    df = _flat_df()
    assert detect_signal(df, channel_period=20) is None


def test_detect_signal_long_on_upward_breakout():
    df = _upward_breakout_df()
    assert detect_signal(df, channel_period=20) == "LONG"


def test_detect_signal_short_on_downward_breakout():
    df = _downward_breakout_df()
    assert detect_signal(df, channel_period=20) == "SHORT"


def test_detect_signal_no_repeat_signal_while_camped_above_channel():
    # 이미 채널 위로 두 봉째 머무는 상황 -> 새 돌파 "순간"이 아니므로 신호 없음
    closes = np.full(41, 100.0)
    closes[-2] = 105.0
    closes[-1] = 106.0
    df = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})
    assert detect_signal(df, channel_period=20) is None


def test_detect_signal_blocked_by_adx_filter_when_set_high():
    df = _upward_breakout_df()
    assert detect_signal(df, channel_period=20, adx_threshold=None) == "LONG"
    assert detect_signal(df, channel_period=20, adx_threshold=99.0) is None
