import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import run_backtest
from src.core.indicators import atr as atr_indicator


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
    trades = run_backtest(flat)  # 기본 ADX 임계값 그대로 -> 추세 없음 -> 신호 없음
    assert trades == []


def test_take_profit_hit_before_stop():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    trades = run_backtest(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)

    assert len(trades) == 1
    assert trades[0]["reason"] == "take_profit"
    assert trades[0]["pnl_r"] == pytest.approx(2.0)


def test_stop_loss_hit():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.005, "low": entry_price * 0.98, "close": entry_price * 0.985,
    }])
    trades = run_backtest(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)

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
    trades = run_backtest(
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
    trades = run_backtest(
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
    trades = run_backtest(extra, stop_mode="atr", atr_multiplier=atr_multiplier, adx_threshold=1)

    assert len(trades) == 1
    assert trades[0]["reason"] == "stop_loss"
    assert trades[0]["exit_price"] == pytest.approx(expected_stop_price)


def test_fee_reduces_pnl_r_by_round_trip_cost():
    df = _cross_up_df()
    entry_price = float(df["close"].iloc[59])
    extra = _append(df, [{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    no_fee = run_backtest(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)
    with_fee = run_backtest(
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

    trades = run_backtest(
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

    trades = run_backtest(
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
    trades = run_backtest(
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
    trades = run_backtest(
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
    trades = run_backtest(
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
    trades = run_backtest(
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
    trades = run_backtest(extra, stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0, adx_threshold=1)
    assert len(trades) == 1
