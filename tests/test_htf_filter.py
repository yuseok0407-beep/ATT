"""상위봉 방향 일치 필터(`futures_strategy.htf_direction_series`/`apply_htf_filter`).

멀티 타임프레임 백테스트에서 가장 흔하고 비싼 실수는 **아직 안 끝난 상위봉을 쓰는 것**이다 —
그러면 백테스트가 앞으로 몇 시간의 가격을 미리 본 셈이라 결과가 부풀고 실거래는 재현을 못 한다.
그래서 이 파일의 핵심은 "뒤의 데이터를 바꿔도 앞의 판정이 안 바뀐다"는 테스트들이다.
"""
import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import gated_signals
from src.core.futures_strategy import (
    HTF_LOOKBACK_BARS,
    apply_htf_filter,
    closed_bars_needed,
    htf_direction_series,
    htf_required_bars,
    latest_htf_direction,
)


def _walk(n=600, seed=7, start="2026-01-01 00:00"):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "timestamp": pd.date_range(start, periods=n, freq="h"),
        "open": closes, "high": closes * 1.005, "low": closes * 0.995, "close": closes,
        "volume": 1.0,
    })


def test_no_lookahead_every_row_matches_a_prefix_only_computation():
    """행 i의 값은 df.iloc[:i+1]만 가지고 계산한 값과 **같아야** 한다 — 실거래 봇이 그 시점에
    볼 수 있는 것이 정확히 그 접두사다."""
    df = _walk()
    full = htf_direction_series(df, 4)
    for i in range(len(df)):
        prefix = htf_direction_series(df.iloc[:i + 1], 4).iloc[-1]
        assert (np.isnan(full.iloc[i]) and np.isnan(prefix)) or full.iloc[i] == prefix, i


def test_a_4h_bar_is_used_only_from_its_last_hour():
    """00:00 4시간봉은 03:00 1시간봉이 마감되는 04:00에야 쓸 수 있다. 그 전 행(00~02시)은
    그 4시간봉의 데이터를 바꿔도 값이 안 변하고, 03시 행부터 변할 수 있다."""
    df = _walk(n=400)
    base = htf_direction_series(df, 4)

    # 뒤쪽의 한 4시간 묶음(인덱스 360~363 = 00:00~03:00)을 크게 뒤집는다
    bucket = df.index[(df["timestamp"].dt.hour // 4 == 0) & (df.index >= 360)][:4]
    assert list(df.loc[bucket, "timestamp"].dt.hour) == [0, 1, 2, 3]
    tampered = df.copy()
    tampered.loc[bucket, ["high", "low", "close"]] *= 0.5
    after = htf_direction_series(tampered, 4)

    before_rows = df.index < bucket[-1]
    pd.testing.assert_series_equal(base[before_rows], after[before_rows])
    # 급락을 넣었으니 그 묶음이 쓰이는 첫 행(03:00)에서 방향이 하락으로 바뀐다
    assert after.loc[bucket[-1]] == -1.0


def test_partial_bucket_at_the_start_is_ignored():
    """조회가 묶음 중간(02:00)에서 시작하면 첫 묶음은 반쪽이다 — 거래소의 4시간봉과 고가/저가가
    다르므로 버려야 한다. 그 두 봉을 아무리 바꿔도 결과가 같아야 한다."""
    df = _walk(n=402, start="2026-01-01 02:00")
    base = htf_direction_series(df, 4)
    tampered = df.copy()
    tampered.loc[:1, ["high", "low", "close"]] *= 3.0
    pd.testing.assert_series_equal(base, htf_direction_series(tampered, 4))


def test_direction_follows_the_higher_timeframe_trend():
    n = 400
    up = np.linspace(100, 150, n)
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
                       "high": up * 1.002, "low": up * 0.998, "close": up})
    assert htf_direction_series(df, 4).iloc[-1] == 1.0

    down = up[::-1]
    df2 = df.assign(high=down * 1.002, low=down * 0.998, close=down)
    assert htf_direction_series(df2, 4).iloc[-1] == -1.0


def test_off_or_warmup_means_no_judgement():
    df = _walk(n=50)
    assert htf_direction_series(df, 0).isna().all()
    # 50시간 = 4시간봉 12개 — DI(14) warm-up에 모자란다
    assert htf_direction_series(df, 4).isna().all()
    assert latest_htf_direction(df, 0) is None
    assert latest_htf_direction(df, 4) is None


def test_htf_must_be_a_whole_multiple_of_the_signal_bar():
    df = _walk(n=100)
    df["timestamp"] = pd.date_range("2026-01-01", periods=100, freq="3h")
    with pytest.raises(ValueError):
        htf_direction_series(df, 4)


@pytest.mark.parametrize("signal,direction,expected", [
    ("LONG", 1.0, "LONG"), ("LONG", -1.0, None),
    ("SHORT", -1.0, "SHORT"), ("SHORT", 1.0, None),
    ("LONG", None, "LONG"), ("SHORT", float("nan"), "SHORT"),
    (None, 1.0, None),
])
def test_apply_htf_filter_blocks_only_counter_trend_signals(signal, direction, expected):
    assert apply_htf_filter(signal, direction) == expected


def test_bars_needed_covers_signal_regime_and_htf_warmup():
    assert htf_required_bars(0) == 0
    assert htf_required_bars(4) == (HTF_LOOKBACK_BARS + 1) * 4
    # 필터가 꺼져 있으면 지금까지와 같은 개수(레짐 400 → 마감봉 401)
    assert closed_bars_needed(400, 0) == 401
    assert closed_bars_needed(0, 0) == 100
    assert closed_bars_needed(400, 8) == htf_required_bars(8)
    # 거래소는 한 번에 1000개까지만 준다 — 요청(마감봉 + 진행 중 1개)이 그걸 넘지 않는다
    assert htf_required_bars(12) + 1 == 1000


def test_gated_signals_with_htf_is_a_subset_that_drops_only_counter_trend():
    """필터는 신호를 **빼기만** 한다. 빠진 신호는 전부 상위봉 방향이 반대였어야 한다."""
    df = _walk(n=1500, seed=3)
    off = gated_signals(df, htf_hours=0, regime_sma_period=0, min_atr_to_stop_ratio=0,
                        adx_threshold=15)
    on = gated_signals(df, htf_hours=4, regime_sma_period=0, min_atr_to_stop_ratio=0,
                       adx_threshold=15)
    assert off, "테스트 데이터에 신호가 있어야 의미가 있다"
    assert set(on.items()) <= set(off.items())

    direction = htf_direction_series(df, 4)
    for i, signal in off.items():
        kept = i in on
        assert kept == (apply_htf_filter(signal, direction.iloc[i]) is not None), i
