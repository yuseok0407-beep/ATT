import numpy as np
import pandas as pd

from src.core.ma_cross_strategy import detect_signal


def _golden_cross_df(n=60, jump=30.0):
    """평평한 구간 위에서 마지막 봉만 크게 점프시킨다. 점프가 빠른선(짧은 기간) 평균에는 크게,
    느린선(긴 기간) 평균에는 작게 반영되므로 빠른선이 느린선을 막 상향 돌파하게 된다."""
    closes = np.full(n, 100.0)
    closes[-1] = 100.0 + jump
    return pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})


def test_detect_signal_returns_none_with_too_few_bars():
    df = _golden_cross_df(n=18)  # slow_period(15) + 5 = 20 > 18
    assert detect_signal(df, fast_period=5, slow_period=15) is None


def test_detect_signal_none_when_lines_dont_cross():
    closes = np.full(60, 100.0)
    df = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})
    assert detect_signal(df, fast_period=10, slow_period=30) is None


def test_detect_signal_long_on_golden_cross():
    df = _golden_cross_df(n=60, jump=30.0)
    signal = detect_signal(df, fast_period=10, slow_period=30)
    assert signal == "LONG"


def test_detect_signal_short_on_dead_cross():
    df = _golden_cross_df(n=60, jump=-30.0)
    signal = detect_signal(df, fast_period=10, slow_period=30)
    assert signal == "SHORT"


def test_detect_signal_ema_vs_sma_agree_on_direction():
    df = _golden_cross_df(n=60, jump=30.0)
    # EMA는 최근 가격에 더 민감해서 SMA와 교차 폭 등 세부는 다를 수 있지만, 이 정도로 큰 점프면
    # 방향 자체는 둘 다 일치해야 한다(반대 신호가 나오면 버그).
    sma_signal = detect_signal(df, fast_period=10, slow_period=30, use_ema=False)
    ema_signal = detect_signal(df, fast_period=10, slow_period=30, use_ema=True)
    assert sma_signal == "LONG"
    assert ema_signal == "LONG"


def test_detect_signal_blocked_by_adx_filter_when_set_high():
    df = _golden_cross_df(n=60, jump=30.0)
    assert detect_signal(df, fast_period=10, slow_period=30, adx_threshold=None) == "LONG"
    assert detect_signal(df, fast_period=10, slow_period=30, adx_threshold=99.0) is None
