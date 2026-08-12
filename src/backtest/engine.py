from typing import Callable

import pandas as pd

from src.core.config import RULE_ADX_THRESHOLD, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.core.futures_strategy import compute_bracket_prices, detect_signal
from src.core.indicators import atr

# detect_signal 자체가 요구하는 최소 워밍업(35봉)
MIN_WARMUP_BARS = 35
# 실거래 봇이 매 사이클 신호 계산에 쓰는 캔들 개수(_evaluate_symbol의 fetch_ohlcv_df limit=100)와
# 동일하게 맞춰서, 백테스트가 실제 운영 로직을 최대한 그대로 재현하게 한다.
SIGNAL_LOOKBACK_BARS = 100


def _pnl_r(entry_price: float, original_stop_price: float, side: str, exit_price: float) -> float:
    risk = abs(entry_price - original_stop_price)
    if risk == 0:
        return 0.0
    diff = exit_price - entry_price
    if side == "short":
        diff = -diff
    return diff / risk


def _check_exit(bar: pd.Series, position: dict, use_breakeven: bool, breakeven_at_r: float,
                 dynamic_target: float | None = None):
    """이 봉에서 청산이 일어나는지 확인한다. (사유, 청산가) 또는 (None, None).

    가정: 봉 하나 안에서 손절가와 익절가가 둘 다 닿을 수 있는 경우, 어느 쪽이 먼저인지는
    1시간봉 OHLC만으로는 알 수 없다 — 보수적으로 손절이 먼저 체결됐다고 가정한다.
    손익분기 이동도 같은 봉 안에서 트리거가 먼저 닿았다고 가정하고 그 봉의 손절 판정에 바로 반영한다.

    dynamic_target: 주어지면(예: 볼린저밴드 중간선처럼 매 봉 움직이는 목표가) position의 고정
    target_price 대신 이 값을 익절 기준으로 쓴다 — 평균회귀처럼 "고정 손익비"가 아니라 "중간값
    복귀"가 자연스러운 목표인 전략을 위한 옵션."""
    side = position["side"]
    entry = position["entry_price"]
    original_risk = abs(entry - position["original_stop_price"])
    high, low = bar["high"], bar["low"]

    if use_breakeven and not position["breakeven_moved"] and original_risk > 0:
        trigger = entry + breakeven_at_r * original_risk if side == "long" else entry - breakeven_at_r * original_risk
        reached = (high >= trigger) if side == "long" else (low <= trigger)
        if reached:
            position["stop_price"] = entry
            position["breakeven_moved"] = True

    stop_price = position["stop_price"]
    target_price = dynamic_target if dynamic_target is not None else position["target_price"]
    stop_hit = (low <= stop_price) if side == "long" else (high >= stop_price)
    target_hit = (high >= target_price) if side == "long" else (low <= target_price)

    if stop_hit:
        return ("breakeven_stop" if position["breakeven_moved"] else "stop_loss"), stop_price
    if target_hit:
        return "take_profit", target_price
    return None, None


def _check_exit_partial(bar: pd.Series, position: dict, fee_pct_per_side: float,
                         partial_at_r: float, partial_fraction: float, breakeven_after_partial: bool):
    """부분 익절(스케일 아웃) 모드에서 이 봉의 청산 이벤트를 확인한다. (사유, 청산가, 최종 R배수)를
    돌려주되, 포지션이 아직 완전히 안 닫혔으면(부분 익절만 발생했거나 아무 일도 없으면) 전부 None.

    R배수 산정 방식: 각 구간(부분/잔여)의 손익을 "그 구간이 전체 포지션이었다면 몇 R이었을지"로
    계산한 뒤, 그 구간이 차지하는 비중(fraction)을 곱해서 더한다 — "원래 리스크 1R을 걸었는데
    최종적으로 몇 R을 벌었나"라는 표준적인 블렌디드 R 계산과 동일하다. 수수료도 각 구간의 비중만큼만
    반영한다(그 구간만큼만 실제로 체결되므로).

    가정: 손절과 (부분/최종) 목표가가 같은 봉에서 동시에 닿을 수 있는 경우, 보수적으로 손절이
    먼저라고 가정한다(기존 _check_exit과 동일한 가정)."""
    side = position["side"]
    entry = position["entry_price"]
    original_stop = position["original_stop_price"]
    original_risk = abs(entry - original_stop)
    high, low = bar["high"], bar["low"]
    fee_r_full = (2 * fee_pct_per_side * entry) / original_risk if (fee_pct_per_side and original_risk) else 0.0

    if not position["partial_taken"]:
        stop_price = position["stop_price"]
        stop_hit = (low <= stop_price) if side == "long" else (high >= stop_price)
        if stop_hit:
            pnl_r = _pnl_r(entry, original_stop, side, stop_price) - fee_r_full
            return "stop_loss", stop_price, pnl_r

        partial_target = position["partial_target_price"]
        partial_hit = (high >= partial_target) if side == "long" else (low <= partial_target)
        if partial_hit:
            position["banked_r"] += partial_fraction * (partial_at_r - fee_r_full)
            position["partial_taken"] = True
            if breakeven_after_partial:
                position["stop_price"] = entry
        return None, None, None

    remaining_fraction = 1 - partial_fraction
    stop_price = position["stop_price"]
    target_price = position["target_price"]
    stop_hit = (low <= stop_price) if side == "long" else (high >= stop_price)
    target_hit = (high >= target_price) if side == "long" else (low <= target_price)

    if stop_hit:
        leg_pnl_r = _pnl_r(entry, original_stop, side, stop_price)
        total_r = position["banked_r"] + remaining_fraction * (leg_pnl_r - fee_r_full)
        reason = "breakeven_stop" if stop_price == entry else "stop_loss"
        return reason, stop_price, total_r
    if target_hit:
        leg_pnl_r = _pnl_r(entry, original_stop, side, target_price)
        total_r = position["banked_r"] + remaining_fraction * (leg_pnl_r - fee_r_full)
        return "take_profit", target_price, total_r
    return None, None, None


def run_backtest(
    df: pd.DataFrame,
    *,
    stop_mode: str = "fixed",  # "fixed" | "atr"
    stop_loss_pct: float = STOP_LOSS_PCT,
    atr_period: int = 14,
    atr_multiplier: float = 2.0,
    take_profit_rr: float = TAKE_PROFIT_RR,
    use_breakeven: bool = False,
    breakeven_at_r: float = 1.0,
    use_max_hold: bool = False,
    max_hold_bars: int = 72,
    adx_threshold: float = RULE_ADX_THRESHOLD,
    fee_pct_per_side: float = 0.0,
    sma_period: int = 20,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    require_rsi_confirm: bool = True,
    signal_fn: Callable[[pd.DataFrame], str | None] | None = None,
    target_series: pd.Series | None = None,
    use_partial_tp: bool = False,
    partial_at_r: float = 1.0,
    partial_fraction: float = 0.5,
    breakeven_after_partial: bool = True,
) -> list[dict]:
    """df(1시간봉 OHLCV)를 바 단위로 순회하며 진입 신호를 찾고, 청산 규칙(고정 손익비 vs ATR 기반
    손절, 손익분기 이동, 최대 보유시간)에 따라 가상 매매를 시뮬레이션한다. 결과는 청산된 거래
    목록(각 거래의 R배수 포함).

    signal_fn: 생략하면(기본값) 실거래 봇과 동일한 추세추종 진입 로직(detect_signal, adx_threshold
    등 아래 파라미터들을 그대로 사용)을 쓴다. 다른 전략(예: 볼린저밴드 평균회귀)을 테스트하려면
    df 윈도우 하나를 받아 "LONG"/"SHORT"/None을 돌려주는 콜러블을 넘기면 되고, 이 경우
    adx_threshold/sma_period/rsi_period/rsi_threshold/require_rsi_confirm은 무시된다 — 청산
    시뮬레이션(손절/익절/손익분기/최대보유) 로직은 전략과 무관하게 그대로 재사용된다.

    fee_pct_per_side: 편도 수수료율(예: 바이낸스 선물 테이커 0.0004=0.04%). 왕복 비용을 진입가
    기준 노셔널로 근사해 R배수에서 차감한다 — 0으로 두면(기본값) 수수료 없는 이상적인 결과.

    target_series: 주어지면 각 봉의 값을 그 시점의 익절 목표가로 쓴다(예: 볼린저밴드 중간선) —
    df와 같은 인덱스를 가져야 한다. stop_mode로 계산된 target_price는 이 경우 무시되고 손절가만
    그대로 쓰인다.

    use_partial_tp: True면 부분 익절(스케일 아웃)을 시뮬레이션한다 — partial_at_r(원래 리스크의
    몇 배)에 도달하면 포지션의 partial_fraction만큼 청산해서 그만큼의 R을 확정하고,
    breakeven_after_partial=True(기본값)면 나머지 물량의 손절을 진입가로 옮긴 뒤, 나머지는
    (stop_mode로 계산된) 최종 target_price까지 계속 보유한다. use_breakeven/target_series와는
    동시에 쓸 수 없다(단순화를 위해 조합 검증은 안 함 — 호출자 책임)."""
    if stop_mode not in ("fixed", "atr"):
        raise ValueError(f"invalid stop_mode: {stop_mode!r}")

    atr_series = atr(df, atr_period) if stop_mode == "atr" else None

    trades: list[dict] = []
    position: dict | None = None

    for i in range(MIN_WARMUP_BARS, len(df)):
        bar = df.iloc[i]

        if position is not None:
            if use_partial_tp:
                reason, exit_price, pnl_r = _check_exit_partial(
                    bar, position, fee_pct_per_side, partial_at_r, partial_fraction, breakeven_after_partial,
                )
                if reason is None and use_max_hold and (i - position["entry_index"]) >= max_hold_bars:
                    exit_price = float(bar["close"])
                    remaining_fraction = (1 - partial_fraction) if position["partial_taken"] else 1.0
                    leg_pnl_r = _pnl_r(position["entry_price"], position["original_stop_price"], position["side"], exit_price)
                    original_risk = abs(position["entry_price"] - position["original_stop_price"])
                    fee_r_full = (2 * fee_pct_per_side * position["entry_price"]) / original_risk \
                        if (fee_pct_per_side and original_risk) else 0.0
                    pnl_r = position["banked_r"] + remaining_fraction * (leg_pnl_r - fee_r_full)
                    reason = "max_hold"
            else:
                dynamic_target = float(target_series.iloc[i]) if target_series is not None else None
                reason, exit_price = _check_exit(bar, position, use_breakeven, breakeven_at_r, dynamic_target)
                if reason is None and use_max_hold and (i - position["entry_index"]) >= max_hold_bars:
                    reason, exit_price = "max_hold", float(bar["close"])
                if reason is not None:
                    pnl_r = _pnl_r(position["entry_price"], position["original_stop_price"], position["side"], exit_price)
                    if fee_pct_per_side:
                        risk = abs(position["entry_price"] - position["original_stop_price"])
                        fee_r = (2 * fee_pct_per_side * position["entry_price"]) / risk if risk else 0.0
                        pnl_r -= fee_r

            if reason is not None:
                trades.append({
                    "side": position["side"], "entry_index": position["entry_index"], "exit_index": i,
                    "entry_price": position["entry_price"], "exit_price": exit_price, "reason": reason,
                    "pnl_r": pnl_r, "hold_bars": i - position["entry_index"],
                })
                position = None
            continue

        window = df.iloc[max(0, i - SIGNAL_LOOKBACK_BARS + 1):i + 1]
        if signal_fn is not None:
            signal = signal_fn(window)
        else:
            signal = detect_signal(
                window, adx_threshold=adx_threshold, sma_period=sma_period, rsi_period=rsi_period,
                rsi_threshold=rsi_threshold, require_rsi_confirm=require_rsi_confirm,
            )
        if signal is None:
            continue

        entry_price = float(bar["close"])
        side = "long" if signal == "LONG" else "short"

        if stop_mode == "fixed":
            stop_price, target_price = compute_bracket_prices(entry_price, side, stop_loss_pct, take_profit_rr)
        else:
            latest_atr = atr_series.iloc[i]
            if pd.isna(latest_atr) or latest_atr <= 0:
                continue
            stop_distance = atr_multiplier * latest_atr
            take_profit_distance = stop_distance * take_profit_rr
            if side == "long":
                stop_price = entry_price - stop_distance
                target_price = entry_price + take_profit_distance
            else:
                stop_price = entry_price + stop_distance
                target_price = entry_price - take_profit_distance

        position = {
            "side": side, "entry_price": entry_price, "entry_index": i,
            "stop_price": stop_price, "target_price": target_price,
            "original_stop_price": stop_price, "breakeven_moved": False,
        }
        if use_partial_tp:
            original_risk = abs(entry_price - stop_price)
            partial_target_price = entry_price + partial_at_r * original_risk if side == "long" \
                else entry_price - partial_at_r * original_risk
            position.update({
                "partial_taken": False, "partial_target_price": partial_target_price, "banked_r": 0.0,
            })

    return trades
