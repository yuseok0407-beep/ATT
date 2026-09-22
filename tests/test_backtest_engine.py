import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import run_backtest
from src.core.indicators import atr as atr_indicator

# 이 파일의 대부분은 "진입 이후의 청산 로직"(손절/익절/손익분기/최대보유/부분익절)을 검증한다.
# 그런데 2026-09-22부터 엔진이 실거래와 같은 진입 게이트(레짐 SMA400 숏차단·저변동 차단)와
# 수수료를 기본으로 적용하므로, 60봉짜리 합성 데이터에서는 워밍업(400봉)조차 못 채워서 진입이
# 아예 안 생긴다. 청산 로직을 보려면 그 세 개를 끈 상태로 불러야 한다 — 게이트 자체의 검증은
# 이 파일 아래쪽 "진입 게이트" 절에서 run_backtest를 직접 불러서 따로 한다.
EXIT_ONLY = dict(regime_sma_period=0, min_atr_to_stop_ratio=0.0, fee_pct_per_side=0.0)


def run_exit_only(df, **kwargs):
    """진입 게이트와 수수료를 끈 run_backtest — 청산 로직만 보는 테스트용."""
    return run_backtest(df, **{**EXIT_ONLY, **kwargs})


def _cross_up_df(n=60):
    """완만한 상승 추세 위에서 마지막 봉에 SMA20을 새로 상향 돌파하는 시나리오.
    tests/test_futures_strategy.py의 동일한 헬퍼와 같은 구성 — 여기서는 백테스트 엔진이
    entry 이후의 청산 로직만 검증하면 되므로 진입 자체는 이 고정 패턴으로 항상 트리거시킨다."""
    closes = 100 + np.arange(n) * 0.5
    closes[-2] -= 5
    closes[-1] = closes[-3] + 5
    return pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})


def _append(df, rows):
    return pd.concat([df, pd.DataFrame(rows)], ignore_index=True)


def test_no_trend_market_produces_no_trades():
    flat = pd.DataFrame({"close": np.full(60, 100.0)})
    flat["high"] = flat["close"] + 0.1
    flat["low"] = flat["close"] - 0.1
    trades = run_exit_only(flat)  # 기본 ADX 임계값 그대로 -> 추세 없음 -> 신호 없음
    assert trades == []


def test_take_profit_hit_before_stop():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    trades = run_exit_only(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)

    assert len(trades) == 1
    assert trades[0]["reason"] == "take_profit"
    assert trades[0]["pnl_r"] == pytest.approx(2.0)


def test_stop_loss_hit():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.005, "low": entry_price * 0.98, "close": entry_price * 0.985,
    }])
    trades = run_exit_only(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)

    assert len(trades) == 1
    assert trades[0]["reason"] == "stop_loss"
    assert trades[0]["pnl_r"] == pytest.approx(-1.0)


def test_breakeven_move_turns_a_would_be_loss_into_a_scratch():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    # 1봉째: +1R까지 찍고 손익분기 이동 트리거 / 2봉째: 진입가 아래로 되돌림
    extra = _append(df, [
        {"high": entry_price * 1.015, "low": entry_price * 1.005, "close": entry_price * 1.01},
        {"high": entry_price * 1.005, "low": entry_price * 0.995, "close": entry_price * 0.998},
    ])
    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        use_breakeven=True, breakeven_at_r=1.0,
    )

    assert len(trades) == 1
    assert trades[0]["reason"] == "breakeven_stop"
    assert trades[0]["exit_price"] == pytest.approx(entry_price)
    assert trades[0]["pnl_r"] == pytest.approx(0.0)


def test_max_hold_forces_exit_when_neither_stop_nor_target_hit():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [
        {"high": entry_price * 1.005, "low": entry_price * 0.995, "close": entry_price * 1.001},
        {"high": entry_price * 1.005, "low": entry_price * 0.995, "close": entry_price * 1.002},
    ])
    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        use_max_hold=True, max_hold_bars=2,
    )

    assert len(trades) == 1
    assert trades[0]["reason"] == "max_hold"
    assert trades[0]["hold_bars"] == 2
    assert trades[0]["exit_price"] == pytest.approx(entry_price * 1.002)


def test_atr_stop_mode_sizes_stop_from_atr_not_fixed_pct():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    expected_atr = atr_indicator(df, 14).iloc[59]
    atr_multiplier = 1.5
    expected_stop_price = entry_price - atr_multiplier * expected_atr

    extra = _append(df, [{
        "high": entry_price + 1, "low": expected_stop_price, "close": expected_stop_price + 0.1,
    }])
    trades = run_exit_only(extra, stop_mode="atr", atr_multiplier=atr_multiplier, adx_threshold=1)

    assert len(trades) == 1
    assert trades[0]["reason"] == "stop_loss"
    assert trades[0]["exit_price"] == pytest.approx(expected_stop_price)


def test_fee_reduces_pnl_r_by_round_trip_cost():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    no_fee = run_exit_only(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)
    with_fee = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        fee_pct_per_side=0.0004,
    )
    expected_fee_r = (2 * 0.0004 * entry_price) / (entry_price * 0.01)
    assert with_fee[0]["pnl_r"] == pytest.approx(no_fee[0]["pnl_r"] - expected_fee_r)


def test_signal_fn_override_bypasses_default_trend_strategy():
    # 기본 전략(추세추종)이라면 절대 신호를 안 낼 완전 평평한 시장이라도, signal_fn을 넘기면
    # 그 콜러블이 반환하는 신호로 그대로 진입해야 한다(엔진의 청산 시뮬레이션만 재사용).
    closes = np.full(60, 100.0)
    highs = closes + 0.1
    lows = closes - 0.1
    highs[40] = 103.0  # 진입(i=35, entry=100) 이후 target_price=102를 넘겨 익절 트리거
    flat = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    calls = []

    def always_long_once(window):
        calls.append(window)
        return "LONG" if len(calls) == 1 else None  # 청산 후 재호출 시 재진입 방지 위해 None

    trades = run_exit_only(
        flat, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, signal_fn=always_long_once,
    )

    assert len(trades) == 1
    assert trades[0]["side"] == "long"
    assert trades[0]["reason"] == "take_profit"
    assert len(calls) >= 1


def test_target_series_overrides_fixed_target_with_moving_value():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    # 고정 손익비 목표(2R)로는 절대 안 닿을 만큼 작은 폭만 반등시키되, target_series를 그보다
    # 낮게 줘서 그 동적 목표에는 닿게 만든다 -> target_series가 실제로 쓰였다는 증거.
    extra = _append(df, [{
        "high": entry_price * 1.003, "low": entry_price * 0.999, "close": entry_price * 1.002,
    }])
    dynamic_target_price = entry_price * 1.002  # 고정 2R 목표(entry*1.02)보다 훨씬 낮음
    target_series = pd.Series([dynamic_target_price] * len(extra))

    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        target_series=target_series,
    )

    assert len(trades) == 1
    assert trades[0]["reason"] == "take_profit"
    assert trades[0]["exit_price"] == pytest.approx(dynamic_target_price)


def test_partial_tp_then_final_target_blends_r_by_fraction():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [
        {"high": entry_price * 1.015, "low": entry_price * 0.999, "close": entry_price * 1.01},  # 부분익절(1R) 도달
        {"high": entry_price * 1.03, "low": entry_price * 1.005, "close": entry_price * 1.025},  # 잔여 물량 최종익절(2R) 도달
    ])
    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.5,
    )

    assert len(trades) == 1
    assert trades[0]["reason"] == "take_profit"
    # 절반은 1R에, 나머지 절반은 2R에 청산 -> 블렌디드 0.5*1 + 0.5*2 = 1.5R
    assert trades[0]["pnl_r"] == pytest.approx(1.5)


def test_partial_tp_then_breakeven_stop_still_locks_in_partial_gain():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [
        {"high": entry_price * 1.015, "low": entry_price * 0.999, "close": entry_price * 1.01},  # 부분익절(1R) 도달
        {"high": entry_price * 1.005, "low": entry_price * 0.999, "close": entry_price * 1.0},  # 잔여 물량 손익분기(진입가)로 되돌림
    ])
    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.5,
    )

    assert len(trades) == 1
    assert trades[0]["reason"] == "breakeven_stop"
    assert trades[0]["exit_price"] == pytest.approx(entry_price)
    # 절반은 1R 확정, 나머지 절반은 손익분기(0R) -> 블렌디드 0.5*1 + 0.5*0 = 0.5R
    # (예전에 기각했던 "전체 포지션 손익분기 이동"과 달리, 부분익절은 최악의 경우에도 0R이 아니라
    # 플러스로 남는다는 게 핵심 차이)
    assert trades[0]["pnl_r"] == pytest.approx(0.5)


def test_partial_tp_full_stop_before_any_partial_behaves_like_normal_stop():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.005, "low": entry_price * 0.98, "close": entry_price * 0.985,
    }])
    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.5,
    )

    assert len(trades) == 1
    assert trades[0]["reason"] == "stop_loss"
    assert trades[0]["pnl_r"] == pytest.approx(-1.0)


def test_partial_tp_fee_scales_by_fraction_on_each_leg():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [
        {"high": entry_price * 1.015, "low": entry_price * 0.999, "close": entry_price * 1.01},
        {"high": entry_price * 1.03, "low": entry_price * 1.005, "close": entry_price * 1.025},
    ])
    fee_pct_per_side = 0.0004
    trades = run_exit_only(
        extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
        use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.5, fee_pct_per_side=fee_pct_per_side,
    )

    fee_r_full = (2 * fee_pct_per_side) / 0.01
    expected = 0.5 * (1.0 - fee_r_full) + 0.5 * (2.0 - fee_r_full)
    assert trades[0]["pnl_r"] == pytest.approx(expected)


def test_no_reentry_on_the_same_bar_a_position_closes():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    # 손절로 청산되는 바로 그 봉 자체가 우연히 또 다른 신호처럼 보여도 같은 봉에 재진입하면 안 된다
    extra = _append(df, [{
        "high": entry_price * 1.005, "low": entry_price * 0.98, "close": entry_price * 0.985,
    }])
    trades = run_exit_only(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)
    assert len(trades) == 1


# ---------------------------------------------------------------- 진입 게이트
# 2026-09-22까지 엔진에는 이 두 게이트가 없었다 — 레짐 필터는 signal_fn을 손으로 주입하는
# 스크립트 하나만 썼고 저변동 필터는 어떤 백테스트 경로에도 없었다. 그래서 run_backtest의
# 기본 경로가 "실거래가 거부하는 진입"을 포함한 다른 전략을 측정하고 있었다. 아래 테스트는
# 기본값(=config의 실거래값)으로 부른 엔진이 실거래와 같은 판단을 하는지를 고정한다.

_REGIME_BARS = 400  # config 기본 RULE_REGIME_SMA_PERIOD와 같은 길이


def _regime_df(*, tail_bars, tail_slope, short_cross=True, resolve_pct=0.05):
    """"장기 상승 뒤 최근 구간"으로 레짐과 신호 방향을 따로 조절할 수 있는 데이터.

    숏 신호를 상승 레짐에서 만들려면 이 두 층이 필요하다 — 단순 상승 추세의 마지막 두 봉만
    꺾어서는 RSI 확인(숏은 RSI<=50)을 통과하지 못해 애초에 신호가 안 난다. 400봉 상승으로
    SMA400을 아래쪽에 깔아두고, 최근 구간의 길이/기울기로 종가가 그 선 위/아래에서 끝나게 한다.

    마지막에 붙이는 봉은 진입을 청산시키기 위한 것 — 없으면 마지막 봉에서 진입만 하고 끝나서
    (청산된 거래만 돌려주는) run_backtest가 빈 목록을 주고, 게이트가 막은 것과 구분이 안 된다."""
    rise = 100 + np.arange(_REGIME_BARS) * 1.0
    tail = rise[-1] + np.arange(1, tail_bars + 1) * tail_slope
    closes = np.concatenate([rise, tail]).astype(float)
    if short_cross:
        closes[-2] += 5
        closes[-1] = closes[-3] - 5
    else:
        closes[-2] -= 5
        closes[-1] = closes[-3] + 5
    df = pd.DataFrame({"high": closes + 0.5, "low": closes - 0.5, "close": closes})

    entry = float(closes[-1])
    return _append(df, [{"high": entry * (1 + resolve_pct),
                          "low": entry * (1 - resolve_pct), "close": entry}])


def _sides_after_warmup(trades, side):
    """워밍업 구간(봉 400 이전)에서 난 거래는 뺀 진입 위치 목록.

    레짐 필터를 켜면 엔진이 SMA가 확정된 봉부터 순회하므로 **거래 총수가 워밍업 때문에도
    줄어든다.** 그 차이를 필터 효과로 착각하지 않으려면, 두 설정 모두가 순회하는 구간
    (봉 400 이후)만 비교해야 한다."""
    return [t["entry_index"] for t in trades
            if t["side"] == side and t["entry_index"] >= _REGIME_BARS]


# stop_loss_pct는 일부러 넣지 않는다 — 저변동 게이트 테스트가 그 값을 바꿔서 비율이 규칙을
# 따라오는지 보기 때문에, 여기 기본값이 있으면 키워드가 중복된다.
_GATE_BASE = dict(adx_threshold=1, fee_pct_per_side=0.0, stop_mode="fixed", take_profit_rr=2.0)
_NO_VOL_GATE = dict(_GATE_BASE, min_atr_to_stop_ratio=0.0, stop_loss_pct=0.01)


def test_regime_gate_blocks_shorts_above_the_long_sma_by_default():
    """상승 레짐(종가 > SMA400)에서 나온 숏 신호는 기본 설정으로 진입되지 않아야 한다 —
    실거래 봇 _evaluate_symbol의 apply_regime_filter와 같은 판단."""
    df = _regime_df(tail_bars=60, tail_slope=-0.5)

    allowed = run_backtest(df, **_NO_VOL_GATE, regime_sma_period=0)
    blocked = run_backtest(df, **_NO_VOL_GATE)

    assert _sides_after_warmup(allowed, "short"), "게이트를 끄면 숏이 나와야 하는 시나리오"
    assert _sides_after_warmup(blocked, "short") == []


def test_regime_gate_does_not_touch_shorts_below_the_long_sma():
    """하락 레짐(종가 < SMA400)에서는 숏을 막지 않는다 — 필터는 역행 숏만 걸러낸다.
    같은 구간에서 게이트 유무가 숏 목록을 바꾸지 않아야 한다."""
    rng = np.random.default_rng(7)
    closes = (500 - np.arange(560) * 0.5 + rng.normal(0, 2, 560)).astype(float)
    df = pd.DataFrame({"high": closes + 2, "low": closes - 2, "close": closes})

    on = run_backtest(df, **_NO_VOL_GATE)
    off = run_backtest(df, **_NO_VOL_GATE, regime_sma_period=0)

    assert _sides_after_warmup(on, "short"), "하락 레짐에서 숏이 나와야 하는 시나리오"
    assert _sides_after_warmup(on, "short") == _sides_after_warmup(off, "short")


def test_regime_gate_leaves_longs_alone():
    """레짐 필터는 숏만 막는다(롱은 안 건드림) — 실거래 규칙과 동일."""
    df = _regime_df(tail_bars=60, tail_slope=0.5, short_cross=False)

    trades = run_backtest(df, **_NO_VOL_GATE)

    assert _sides_after_warmup(trades, "long")


def test_volatility_floor_gate_blocks_low_atr_entries_by_default():
    """ATR이 손절폭 대비 하한 미만이면 진입하지 않아야 한다. 이 게이트는 그동안 어떤 백테스트
    경로에도 없었으므로, 켠 쪽과 끈 쪽이 달라지는 것으로 실제 적용을 확인한다."""
    # 손절폭을 크게 잡으면 ATR/손절폭 비율이 작아져 저변동 게이트가 걸린다. 그만큼 브라켓도
    # 멀어지므로 청산용 봉의 폭(resolve_pct)도 같이 키워야 거래가 닫힌다.
    df = _regime_df(tail_bars=60, tail_slope=0.5, short_cross=False, resolve_pct=0.6)
    common = dict(_GATE_BASE, regime_sma_period=0, stop_loss_pct=0.5)

    off = run_backtest(df, **common, min_atr_to_stop_ratio=0.0)
    on = run_backtest(df, **common, min_atr_to_stop_ratio=0.64)

    assert off, "게이트를 끄면 진입이 있어야 하는 시나리오"
    assert on == []


def test_volatility_floor_uses_ratio_to_stop_not_absolute_atr():
    """같은 데이터에서 손절폭만 좁히면 같은 ATR이 하한을 통과해야 한다 — 규칙이 ATR% 절대값이
    아니라 손절폭 대비 비율이라는 것(STOP_LOSS_PCT를 바꿔도 조건이 따라오는 이유)."""
    df = _regime_df(tail_bars=60, tail_slope=0.5, short_cross=False, resolve_pct=0.6)
    common = dict(_GATE_BASE, regime_sma_period=0, min_atr_to_stop_ratio=0.64)

    wide_stop = run_backtest(df, **common, stop_loss_pct=0.5)
    tight_stop = run_backtest(df, **common, stop_loss_pct=0.002)

    assert wide_stop == []
    assert tight_stop


def test_fee_is_charged_by_default_now():
    """기본값이 무수수료(0.0)였던 것이 낙관적 결과의 직접 원인이었다 — 이제 기본으로 차감된다."""
    df = _regime_df(tail_bars=60, tail_slope=0.5, short_cross=False)
    common = dict(adx_threshold=1, regime_sma_period=0, min_atr_to_stop_ratio=0.0,
                  stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0)

    default_fee = run_backtest(df, **common)
    zero_fee = run_backtest(df, **common, fee_pct_per_side=0.0)

    assert default_fee and zero_fee
    assert default_fee[0]["pnl_r"] < zero_fee[0]["pnl_r"]


def test_regime_gate_warmup_skips_bars_where_the_filter_could_not_apply():
    """레짐 필터가 켜져 있으면 그 SMA가 확정된 뒤부터만 진입한다 — 실거래 봇은 항상 필터가
    적용된 상태이므로, warm-up 구간(필터 미적용)에서 진입하면 그 구간만 다른 전략이 된다."""
    short_df = _append(_cross_up_df(60), [{"high": 1e6, "low": 0.0, "close": 100.0}])

    assert run_backtest(short_df, **_NO_VOL_GATE) == []
    # 게이트를 끄면 같은 데이터에서 진입이 나온다 — 막힌 이유가 워밍업이라는 것.
    assert run_backtest(short_df, **_NO_VOL_GATE, regime_sma_period=0)


# ------------------------------------------------- 체결 슬리피지 (2026-09-22)
# 백테스트는 신호 봉 종가에 즉시 체결된다고 보지만, 실거래 봇은 봉이 마감된 뒤 시장가로 들어가고
# 신호가에서 MAX_ENTRY_PRICE_DRIFT_R(0.5R)까지 벌어져도 진입을 허용한다 — 구조적으로 허용된
# 괴리가 건당 기대값(+0.12R)보다 크다. 그 민감도를 재려면 엔진이 이걸 모델링해야 한다.


def test_entry_slippage_reduces_r_without_moving_the_bracket():
    """체결가만 불리해지고 손절/익절 가격은 신호 봉 종가 기준 그대로여야 한다 — 실거래에서
    벌어지는 일이 정확히 이것이다(브라켓은 신호가로 계산되는데 진입은 더 늦게 된다)."""
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    common = dict(stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
                  regime_sma_period=0, min_atr_to_stop_ratio=0.0, fee_pct_per_side=0.0)

    clean = run_backtest(extra, **common)
    slipped = run_backtest(extra, **common, slippage_r_per_side=0.1)

    assert clean[0]["reason"] == slipped[0]["reason"] == "take_profit"
    # 익절가(= +2R 지점)는 그대로이므로 청산가도 같다 — 달라지는 건 R뿐이다.
    assert slipped[0]["exit_price"] == pytest.approx(clean[0]["exit_price"])
    # 진입 0.1R 불리 + 청산 0.1R 불리 = 0.2R 손실.
    assert slipped[0]["pnl_r"] == pytest.approx(clean[0]["pnl_r"] - 0.2)


def test_slippage_hurts_shorts_in_the_same_direction():
    """숏은 더 낮게 팔고 더 높게 되사는 것이 불리하다 — 부호를 방향에 맞춰야 한다."""
    rng = np.random.default_rng(7)
    closes = (500 - np.arange(560) * 0.5 + rng.normal(0, 2, 560)).astype(float)
    df = pd.DataFrame({"high": closes + 2, "low": closes - 2, "close": closes})
    common = dict(_GATE_BASE, min_atr_to_stop_ratio=0.0, stop_loss_pct=0.01)

    clean = run_backtest(df, **common)
    slipped = run_backtest(df, **common, slippage_r_per_side=0.1)

    shorts_clean = [t for t in clean if t["side"] == "short"]
    shorts_slipped = [t for t in slipped if t["side"] == "short"]
    assert shorts_clean and len(shorts_clean) == len(shorts_slipped)
    assert sum(t["pnl_r"] for t in shorts_slipped) < sum(t["pnl_r"] for t in shorts_clean)


def test_slippage_denominator_stays_the_signal_based_risk():
    """R의 분모는 신호가~손절가로 고정돼야 한다 — 체결가로 분모를 재면 슬리피지가 리스크를
    키우는 것처럼 계산되어 손실이 오히려 작아 보인다(실거래 realized_r과도 기준이 어긋난다)."""
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    # 손절에 닿는 봉 — 신호가 기준 정확히 -1R 지점에서 청산된다.
    extra = _append(df, [{
        "high": entry_price * 1.001, "low": entry_price * 0.98, "close": entry_price * 0.985,
    }])
    common = dict(stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
                  regime_sma_period=0, min_atr_to_stop_ratio=0.0, fee_pct_per_side=0.0)

    slipped = run_backtest(extra, **common, slippage_r_per_side=0.1)

    # 분모가 신호 기준이면 정확히 -1.2R(진입 -0.1, 청산 -0.1). 체결가로 분모를 재면 -1.09R쯤
    # 나와서 슬리피지가 손실을 줄인 것처럼 보인다.
    assert slipped[0]["pnl_r"] == pytest.approx(-1.2)


def test_slippage_defaults_to_zero_so_existing_results_are_unchanged():
    """수수료와 달리 관측값이 없으므로 기본값은 0이다 — 켜는 것은 명시적 선택이어야 한다."""
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    common = dict(stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1,
                  regime_sma_period=0, min_atr_to_stop_ratio=0.0, fee_pct_per_side=0.0)

    assert run_backtest(extra, **common)[0]["pnl_r"] == pytest.approx(
        run_backtest(extra, **common, slippage_r_per_side=0.0)[0]["pnl_r"])


def test_engine_defaults_match_the_live_config():
    """엔진을 인자 없이 부르면 **실거래와 같은 규칙**이어야 한다.

    이게 깨졌던 적이 두 번 있다: (1) `sma_period`의 하드코딩 기본값이 20이라 실거래값(10)과
    다른 전략이 조용히 측정됐다(UPDATE_LOG 2026-08-23), (2) `fee_pct_per_side` 기본값이 0.0이라
    수수료 없는 결과가 나왔고 저변동/레짐 게이트는 아예 없었다(2026-09-22). 기본값이 config에서
    떨어져 나가는 것을 테스트로 고정한다."""
    import inspect

    from src.core import config
    from src.core.risk import MAX_CONSECUTIVE_LOSSES  # noqa: F401  (존재 확인용)

    defaults = {name: param.default
                for name, param in inspect.signature(run_backtest).parameters.items()}

    assert defaults["sma_period"] == config.RULE_SMA_PERIOD
    assert defaults["adx_threshold"] == config.RULE_ADX_THRESHOLD
    assert defaults["stop_loss_pct"] == config.STOP_LOSS_PCT
    assert defaults["take_profit_rr"] == config.TAKE_PROFIT_RR
    assert defaults["fee_pct_per_side"] == config.FEE_PCT_PER_SIDE
    assert defaults["regime_sma_period"] == config.RULE_REGIME_SMA_PERIOD
    assert defaults["min_atr_to_stop_ratio"] == config.MIN_ATR_TO_STOP_RATIO


def test_gated_signals_defaults_match_run_backtest_defaults():
    """두 함수의 진입 관련 기본값이 어긋나면, 포트폴리오 시뮬레이션과 단일 종목 백테스트가
    서로 다른 전략을 재게 된다(둘이 이 함수를 공유하는 의미가 없어진다)."""
    import inspect

    from src.backtest.engine import gated_signals

    a = {n: p.default for n, p in inspect.signature(run_backtest).parameters.items()}
    b = {n: p.default for n, p in inspect.signature(gated_signals).parameters.items()}

    for key in ("stop_loss_pct", "atr_period", "adx_threshold", "regime_sma_period",
                "min_atr_to_stop_ratio", "sma_period", "rsi_period", "rsi_threshold",
                "require_rsi_confirm"):
        assert a[key] == b[key], key
