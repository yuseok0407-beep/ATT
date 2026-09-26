import numpy as np
import pandas as pd
import pytest

from src.core.futures_strategy import compute_bracket_prices, detect_signal
from src.core.indicators import rsi as rsi_indicator


def _trending_df(n=60, start=100, step=1.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    closes = start + np.arange(n) * step + rng.normal(0, noise, n)
    return pd.DataFrame({
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
    })


def _cross_up_df(n=60):
    """완만한 상승 추세 위에서, 직전 봉만 SMA20 아래로 살짝 눌렸다가 마지막 봉에서 강하게 튀어올라
    SMA20을 새로 돌파하는 시나리오. SMA20은 뒤의 19개 봉이 지배하므로 한두 봉의 급격한 흔들림으로는
    거의 움직이지 않는다는 점을 이용해, "직전엔 SMA 아래(prev_diff<=0) -> 지금은 SMA 위(curr_diff>0)"를
    확실하게 만든다."""
    closes = 100 + np.arange(n) * 0.5
    closes[-2] -= 5  # 직전 봉: 추세 대비 급락 -> SMA20보다 낮아짐
    closes[-1] = closes[-3] + 5  # 마지막 봉: 급등 -> SMA20보다 확실히 높아짐
    return pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})


def test_detect_signal_returns_none_with_too_few_bars():
    df = _trending_df(n=10)
    assert detect_signal(df) is None


def test_detect_signal_returns_none_when_trend_too_weak():
    df = _trending_df(n=60, step=0.0, noise=0.05)  # 거의 평평 -> ADX 낮음
    assert detect_signal(df) is None


def test_detect_signal_long_on_bullish_cross_with_trend():
    df = _cross_up_df(n=60)
    signal = detect_signal(df, adx_threshold=1)  # 임계값 낮춰서 크로스 로직 자체를 검증
    assert signal == "LONG"


def test_detect_signal_short_on_bearish_cross_with_trend():
    up_df = _cross_up_df(n=60)
    # 상승 시나리오를 첫 값 기준으로 대칭 반전시켜 하락 돌파 시나리오 생성
    closes = (2 * up_df["close"].iloc[0] - up_df["close"]).to_numpy()
    df = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})
    signal = detect_signal(df, adx_threshold=1)
    assert signal == "SHORT"


def test_detect_signal_no_signal_when_already_above_sma_no_fresh_cross():
    # 계속 SMA 위에 머무는 완만한 상승 추세 -> 크로스가 반복적으로 발생하면 안 됨
    df = _trending_df(n=60, step=0.3, noise=0.0)
    # 마지막 두 봉 다 SMA 위에 있어야 신호가 없어야 정상(순수 레벨이 아니라 크로스만 신호)
    signal = detect_signal(df, adx_threshold=1)
    assert signal is None


def test_detect_signal_without_rsi_confirm_ignores_momentum():
    # RSI 확인을 끄면 크로스 방향만으로 신호가 나야 한다 — RSI가 반대 방향이어도 무시
    df = _cross_up_df(n=60)
    signal = detect_signal(df, adx_threshold=1, require_rsi_confirm=False)
    assert signal == "LONG"


def test_detect_signal_stricter_rsi_threshold_can_block_signal():
    """RSI 분기를 재는 테스트이므로 `direction_filter="rsi"`를 명시한다 — 기본값이 설정에서
    오므로(2026-09-24 실거래가 "di"로 전환) 명시하지 않으면 이 테스트가 재려는 분기를
    아예 안 탄다."""
    df = _cross_up_df(n=60)
    latest_rsi = rsi_indicator(df["close"], 14).iloc[-1]
    # 실제 RSI보다 높은 임계값을 요구하면 신호가 막혀야 한다
    signal = detect_signal(df, adx_threshold=1, direction_filter="rsi",
                           rsi_threshold=float(latest_rsi) + 10)
    assert signal is None


def test_detect_signal_custom_sma_period_changes_cross_detection():
    df = _cross_up_df(n=60)
    # SMA20에서는 크로스로 잡히던 것이 훨씬 긴 SMA(예: 50)에서는 아직 그 아래일 수 있다
    signal_sma20 = detect_signal(df, adx_threshold=1, sma_period=20)
    signal_sma50 = detect_signal(df, adx_threshold=1, sma_period=50)
    assert signal_sma20 == "LONG"
    assert signal_sma50 is None


def test_compute_bracket_prices_long():
    stop, tp = compute_bracket_prices(100, "long", stop_loss_pct=0.01, take_profit_rr=2.0)
    assert stop == pytest.approx(99.0)
    assert tp == pytest.approx(102.0)


def test_compute_bracket_prices_short():
    stop, tp = compute_bracket_prices(100, "short", stop_loss_pct=0.01, take_profit_rr=2.0)
    assert stop == pytest.approx(101.0)
    assert tp == pytest.approx(98.0)


def test_compute_bracket_prices_invalid_side():
    with pytest.raises(ValueError):
        compute_bracket_prices(100, "up")


def _regime_df(n=400, *, uptrend=True):
    """장기 SMA 대비 종가가 확실히 위(상승 레짐) 또는 아래(하락 레짐)에 있는 df."""
    step = 0.5 if uptrend else -0.5
    closes = 500 + np.arange(n) * step
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
        "high": closes + 0.5, "low": closes - 0.5, "close": closes,
    })


def test_regime_filter_allows_shorts_in_downtrend(monkeypatch):
    from src.core import futures_strategy
    from src.core.futures_strategy import make_regime_filtered_signal_fn

    df = _regime_df(uptrend=False)
    monkeypatch.setattr(futures_strategy, "detect_signal", lambda window, **kw: "SHORT")
    signal_fn = make_regime_filtered_signal_fn(df, 200)
    assert signal_fn(df.iloc[-100:]) == "SHORT"


def test_regime_filter_blocks_short_when_price_above_long_sma(monkeypatch):
    from src.core import futures_strategy
    from src.core.futures_strategy import make_regime_filtered_signal_fn

    df = _regime_df(uptrend=True)
    monkeypatch.setattr(futures_strategy, "detect_signal", lambda window, **kw: "SHORT")
    signal_fn = make_regime_filtered_signal_fn(df, 200)
    assert signal_fn(df.iloc[-100:]) is None


def test_regime_filter_leaves_longs_alone_by_default(monkeypatch):
    from src.core import futures_strategy
    from src.core.futures_strategy import make_regime_filtered_signal_fn

    df = _regime_df(uptrend=False)  # 종가가 장기선 아래 = 하락 레짐
    monkeypatch.setattr(futures_strategy, "detect_signal", lambda window, **kw: "LONG")
    assert make_regime_filtered_signal_fn(df, 200)(df.iloc[-100:]) == "LONG"
    assert make_regime_filtered_signal_fn(df, 200, filter_longs=True)(df.iloc[-100:]) is None


def test_regime_filter_allows_entry_while_long_sma_is_warming_up(monkeypatch):
    """레짐 SMA가 아직 NaN인 구간에서는 필터를 적용하지 않고 원래 신호를 그대로 통과시킨다."""
    from src.core import futures_strategy
    from src.core.futures_strategy import make_regime_filtered_signal_fn

    df = _regime_df(n=400, uptrend=True)
    monkeypatch.setattr(futures_strategy, "detect_signal", lambda window, **kw: "SHORT")
    signal_fn = make_regime_filtered_signal_fn(df, 200)
    assert signal_fn(df.iloc[:100]) == "SHORT"   # warm-up 구간 -> 통과
    assert signal_fn(df.iloc[-100:]) is None      # warm-up 끝난 뒤 -> 차단


def test_regime_filter_survives_reset_index_slices(monkeypatch):
    """run_walk_forward처럼 df를 잘라 reset_index(drop=True)한 조각으로 호출해도 timestamp 기준
    조회라 레짐 판정이 어긋나지 않는다."""
    from src.core import futures_strategy
    from src.core.futures_strategy import make_regime_filtered_signal_fn

    df = _regime_df(n=400, uptrend=True)
    monkeypatch.setattr(futures_strategy, "detect_signal", lambda window, **kw: "SHORT")
    signal_fn = make_regime_filtered_signal_fn(df, 200)
    second_half = df.iloc[200:].reset_index(drop=True)
    assert signal_fn(second_half.iloc[-100:]) is None


def _vol_df(n=60, close=100.0, hl_pct=0.01):
    closes = np.full(n, close)
    return pd.DataFrame({
        "high": closes * (1 + hl_pct), "low": closes * (1 - hl_pct), "close": closes,
    })


def test_atr_to_stop_ratio_measures_atr_in_units_of_the_stop_distance():
    from src.core.futures_strategy import atr_to_stop_ratio

    # 고가-저가 = 종가의 2% -> ATR ≈ 2%, 손절폭 1% -> 비율 ≈ 2.0
    ratio = atr_to_stop_ratio(_vol_df(hl_pct=0.01), stop_loss_pct=0.01)
    assert ratio == pytest.approx(2.0, rel=0.05)
    # 손절폭을 두 배로 하면 비율은 절반
    assert atr_to_stop_ratio(_vol_df(hl_pct=0.01), stop_loss_pct=0.02) == pytest.approx(1.0, rel=0.05)


def test_atr_to_stop_ratio_returns_none_while_warming_up():
    from src.core.futures_strategy import atr_to_stop_ratio

    assert atr_to_stop_ratio(_vol_df(n=5)) is None


def test_volatility_floor_blocks_only_below_the_threshold():
    from src.core.futures_strategy import passes_volatility_floor

    assert passes_volatility_floor(0.63, 0.64) is False
    assert passes_volatility_floor(0.64, 0.64) is True
    assert passes_volatility_floor(2.0, 0.64) is True


def test_volatility_floor_is_permissive_when_unknown_or_disabled():
    """ATR warm-up 부족이나 필터 off는 '변동성이 부족하다'는 근거가 아니므로 통과시킨다
    (레짐 필터가 above_long_sma=None을 다루는 방식과 같은 방침)."""
    from src.core.futures_strategy import passes_volatility_floor

    assert passes_volatility_floor(None, 0.64) is True
    assert passes_volatility_floor(0.01, 0) is True


# ---------- direction_filter (2026-09-24) ----------
# ADX와 **같은 계산**에서 나오는 +DI/-DI가 지금까지 버려지고 있었다(indicators.adx가 돌려주는데
# detect_signal은 adx 값만 썼다). 방향 확인을 RSI 대신 그쪽으로 돌릴 수 있게 열어둔 손잡이라,
# 여기서 지키는 것은 **기본값이 실거래 동작 그대로인가**와 **선택지가 실제로 다르게 도는가**다.

def test_direction_filter_default_comes_from_config():
    """기본값의 정의는 `config.RULE_DIRECTION_FILTER` 한 곳이어야 한다 — 코드에 따로 박아두면
    .env를 고쳐도 백테스트는 옛 규칙을 돌아서 둘이 갈라진다(이 저장소에서 두 번 난 사고 유형)."""
    from src.core import config, futures_strategy as fs
    assert fs.DEFAULT_DIRECTION_FILTER == config.RULE_DIRECTION_FILTER
    assert config.RULE_DIRECTION_FILTER in fs.VALID_DIRECTION_FILTERS
    assert set(fs.VALID_DIRECTION_FILTERS) == {"rsi", "di", "none"}


def test_direction_filter_is_recorded_in_the_journal_config():
    """저널의 config_changed에 안 실리면 "이 거래가 어느 규칙에서 나왔는지"를 나중에 알 수 없다."""
    from src.core import config
    from src.futures_rule_bot import current_strategy_config
    assert current_strategy_config()["direction_filter"] == config.RULE_DIRECTION_FILTER


def _crossing_up(n=60, base=100.0):
    """마지막 봉에서 종가가 SMA를 상향 돌파하도록 만든 프레임."""
    import numpy as np
    close = np.linspace(base, base * 0.97, n)      # 계속 내려오다가
    close[-1] = close[-2] * 1.05                    # 마지막에 급반등 -> 상향 크로스
    high = close * 1.01
    low = close * 0.99
    return pd.DataFrame({"high": high, "low": low, "close": close})


def _falling_then_bouncing(n=60, drop=0.20, bounce=0.02):
    """길게 하락하다 마지막 봉에서 SMA를 위로 뚫는 프레임 — 돌파는 있지만 -DI가 아직 우세하다."""
    import numpy as np
    close = np.linspace(100, 100 * (1 - drop), n)
    close[-1] = close[-2] * (1 + bounce)
    return pd.DataFrame({"high": close * 1.002, "low": close * 0.998, "close": close})


def test_di_filter_blocks_a_cross_while_minus_di_still_dominates():
    """DI 분기가 실제로 +DI/-DI를 본다는 것 — 돌파만 보는 "none"은 통과시키는 자리에서
    "di"는 막아야 한다(이 프레임에서 -DI 52.3 > +DI 19.4)."""
    from src.core.futures_strategy import detect_signal
    from src.core.indicators import adx

    df = _falling_then_bouncing()
    di = adx(df, 14)
    assert di["minus_di"].iloc[-1] > di["plus_di"].iloc[-1]   # 전제 확인

    assert detect_signal(df, adx_threshold=1, sma_period=10, direction_filter="none") == "LONG"
    assert detect_signal(df, adx_threshold=1, sma_period=10, direction_filter="di") is None


def test_none_matches_the_old_require_rsi_confirm_false():
    """`require_rsi_confirm=False`는 이제 direction_filter="none"과 같은 뜻이다 —
    옛 호출자가 그대로 돌아야 한다."""
    from src.core.futures_strategy import detect_signal
    df = _crossing_up()
    assert (detect_signal(df, adx_threshold=1, sma_period=10, require_rsi_confirm=False)
            == detect_signal(df, adx_threshold=1, sma_period=10, direction_filter="none"))


# --- aoa 전략 (2026-09-26, demo2) -----------------------------------------------------

import numpy as np  # noqa: E402

from src.core import futures_strategy as fs  # noqa: E402


def _aoa_df(direction=-1, n=120, move=4.0):
    """평평하다가 마지막 4봉에 direction 쪽으로 move만큼 움직인다. 꼬리는 반대쪽에 달아서
    마지막 종가가 24봉 범위의 끝에 오게 한다(-1이면 바닥, +1이면 천장)."""
    closes = np.full(n, 100.0)
    highs, lows = closes + 0.5, closes - 0.5
    for k, i in enumerate(range(n - 4, n)):
        c = 100.0 + direction * move * (k + 1) / 4
        closes[i] = c
        highs[i] = c + (1.5 if direction < 0 else 0.2)
        lows[i] = c - (0.2 if direction < 0 else 1.5)
    return pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
                         "open": closes, "high": highs, "low": lows, "close": closes, "volume": 1.0})


def test_aoa_goes_long_after_a_sharp_drop_to_the_bottom_of_the_range():
    df = _aoa_df(-1)
    features = fs.aoa_features(df)
    assert features["range_pos"] <= 0.2 and features["move_atr"] <= -1.0 and features["vol_ratio"] >= 1.0
    assert fs.detect_aoa_signal(df) == "LONG"


def test_aoa_goes_short_after_a_sharp_rise_to_the_top_of_the_range():
    assert fs.detect_aoa_signal(_aoa_df(+1)) == "SHORT"


def test_aoa_ignores_a_small_move_even_at_the_edge_of_the_range():
    """범위 끝이어도 ATR 1배 미만의 움직임이면 들어가지 않는다."""
    df = _aoa_df(-1, move=0.6)
    assert fs.aoa_features(df)["range_pos"] <= 0.2
    assert fs.detect_aoa_signal(df) is None


def test_aoa_ignores_quiet_markets():
    features = {"range_pos": 0.05, "move_atr": -2.0, "vol_ratio": 0.9}
    assert fs.aoa_signal_from_features(features) is None
    assert fs.aoa_signal_from_features({**features, "vol_ratio": 1.0}) == "LONG"


def test_aoa_needs_enough_closed_bars():
    df = _aoa_df(-1)
    assert fs.aoa_features(df.iloc[-(fs.aoa_min_bars() - 1):]) is None
    assert fs.aoa_features(df.iloc[-fs.aoa_min_bars():]) is not None


def test_aoa_uses_only_the_bars_it_is_given():
    """마지막 봉을 잘라내면 판정이 그 전 봉 기준으로 바뀐다 — 실거래가 진행 중 봉을 버리는
    트리밍이 이 함수에서도 그대로 의미를 갖는지 확인."""
    df = _aoa_df(-1)
    assert fs.detect_aoa_signal(df) == "LONG"
    assert fs.aoa_features(df.iloc[:-4])["move_atr"] == pytest.approx(0.0)


def test_aoa_bracket_is_three_percent_stop_one_percent_target():
    stop, target = fs.strategy_bracket_prices("aoa", 100.0, "long")
    assert stop == pytest.approx(97.0) and target == pytest.approx(101.0)
    stop, target = fs.strategy_bracket_prices("aoa", 100.0, "short")
    assert stop == pytest.approx(103.0) and target == pytest.approx(99.0)


def test_trend_bracket_is_unchanged():
    assert fs.strategy_bracket_prices("trend", 100.0, "long") == compute_bracket_prices(100.0, "long")


def test_unknown_strategy_is_an_error_not_a_trend_fallback():
    """.env 오타가 조용히 trend로 돌면 demo2가 데모와 같은 전략을 돌며 "다른 전략 시험 중"이라고
    믿게 된다."""
    with pytest.raises(ValueError):
        fs.check_strategy("aoaa")
    with pytest.raises(ValueError):
        fs.strategy_bracket_prices("aoaa", 100.0, "long")
