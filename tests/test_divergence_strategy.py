import numpy as np
import pandas as pd

from src.core.divergence_strategy import detect_signal
from src.core.indicators import rsi


def _flat_df(n=40, level=100.0):
    closes = np.full(n, level)
    return pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})


def _bullish_divergence_closes():
    """가격은 새로운 저점을 찍지만 RSI는 오히려 개선된(덜 과매도) 시나리오를 만든다 — 숫자는
    실제 rsi() 계산으로 직접 확인 후 고정한 값(아래 테스트에서 그 전제를 다시 검증함).

    RSI는 EWM이라 먼 과거의 영향이 서서히 남는다 — "1구간은 단조하락 RSI≈0, 2구간부턴 상승이
    섞여 RSI가 그보다 높음"이라는 직관이 실제로는 창(lookback=14) 안의 값들끼리만 비교되므로,
    창 안에서도 지그재그(상승/하락 반복)로 완만하게 내려가되 각 저점을 찍을 때마다 직전 저점보다
    RSI가 살짝살짝 올라가는 패턴을 만들어야 한다."""
    warmup = [100.0] * 15
    leg1 = [100 - 3 * i for i in range(1, 6)]  # 97,94,91,88,85 — 급하게 단조 하락
    bounce = [85 + 3 * i for i in range(1, 4)]  # 88,91,94 — 작은 반등
    leg2 = [92, 87, 90, 84, 88, 82, 87, 80, 85, 77, 83, 74]  # 지그재그 순하락, 74가 새 저점
    return warmup + leg1 + bounce + leg2


def _bearish_divergence_closes():
    """강세 시나리오를 가격축으로 뒤집은 약세 다이버전스(신고가인데 RSI는 덜 과매수)."""
    bullish = _bullish_divergence_closes()
    base = bullish[0]
    return [base - (c - base) for c in bullish]


def test_detect_signal_bullish_divergence_returns_long():
    closes = _bullish_divergence_closes()
    close_series = pd.Series(closes)
    rsi_series = rsi(close_series, 14)

    lookback = 14
    window_close = close_series.iloc[-lookback:]
    window_rsi = rsi_series.iloc[-lookback:]
    current_rsi = window_rsi.iloc[-1]
    prior_low_idx = window_close.iloc[:-1].idxmin()
    # 시나리오 전제 확인: 지금 봉이 창 안 신저가이고, RSI는 직전 신저가 시점보다 높아야 한다
    assert window_close.iloc[-1] <= window_close.min()
    assert current_rsi > window_rsi.loc[prior_low_idx]

    df = pd.DataFrame({"high": close_series + 0.5, "low": close_series - 0.5, "close": close_series})
    assert detect_signal(df, lookback=lookback, adx_threshold=None) == "LONG"


def test_detect_signal_bearish_divergence_returns_short():
    closes = _bearish_divergence_closes()
    close_series = pd.Series(closes)
    df = pd.DataFrame({"high": close_series + 0.5, "low": close_series - 0.5, "close": close_series})
    assert detect_signal(df, lookback=14, adx_threshold=None) == "SHORT"


def test_detect_signal_returns_none_with_too_few_bars():
    df = _flat_df(n=10)
    assert detect_signal(df, lookback=14) is None


def test_detect_signal_none_when_no_new_extreme():
    df = _flat_df(n=40)
    assert detect_signal(df, lookback=14, adx_threshold=None) is None


def test_detect_signal_none_when_new_low_but_rsi_also_worse():
    """신저가인데 RSI도 같이 나빠졌으면(다이버전스가 아니라 그냥 계속되는 하락) 신호가 없어야
    한다 — 단조 하락 구간 한가운데서 확인."""
    closes = [100.0] * 20 + [100 - 2 * i for i in range(1, 21)]  # 계속 단조 하락
    close_series = pd.Series(closes)
    df = pd.DataFrame({"high": close_series + 0.5, "low": close_series - 0.5, "close": close_series})
    assert detect_signal(df, lookback=14, adx_threshold=None) is None


def test_detect_signal_blocked_by_adx_filter_when_regime_is_trending():
    closes = _bullish_divergence_closes()
    close_series = pd.Series(closes)
    df = pd.DataFrame({"high": close_series + 0.5, "low": close_series - 0.5, "close": close_series})
    assert detect_signal(df, lookback=14, adx_threshold=None) == "LONG"
    assert detect_signal(df, lookback=14, adx_threshold=0.0) is None  # ADX>=0은 항상 참 -> 항상 차단
