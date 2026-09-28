from typing import Callable

import pandas as pd

from src.core.config import (
    FEE_PCT_PER_SIDE,
    MAKER_FEE_PCT_PER_SIDE,
    MIN_ATR_TO_STOP_RATIO,
    RULE_ADX_THRESHOLD,
    RULE_REGIME_SMA_PERIOD,
    RULE_SMA_PERIOD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_ORDER_TYPE,
    TAKE_PROFIT_RR,
)
from src.core.futures_strategy import (
    DEFAULT_DIRECTION_FILTER,
    apply_regime_filter,
    compute_bracket_prices,
    detect_signal,
    passes_volatility_floor,
)
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


def exit_pnl_r(side: str, entry_price: float, exit_price: float, risk: float, reason: str, *,
               fee_pct_per_side: float, slippage_r_per_side: float,
               take_profit_order_type: str, maker_fee_pct_per_side: float) -> float:
    """포지션 전체가 한 번에 청산될 때의 R(수수료·슬리피지 차감 후). `run_backtest`와
    `portfolio.simulate_portfolio`가 이 함수 하나를 쓴다 — 전에는 두 곳에 같은 식이 복사돼 있었다.

    청산 체결 방식이 사유마다 다르다(실거래와 같게, 2026-09-28):
      - 손절/최대보유/손익분기: 시장가 → 테이커 수수료, 청산 슬리피지를 문다.
      - 익절 + `take_profit_order_type="limit"`: 익절가에 걸어 둔 지정가 → **메이커 수수료**,
        익절가보다 나쁘게 체결될 수 없으므로 **청산 슬리피지 없음**. 메이커율은 테이커율을 넘지
        않게 자른다(수수료 0으로 돌리는 호출이 익절에만 수수료를 무는 일이 없게).
      - 익절 + "market": 옛 방식(조건부 시장가) → 손절과 같다.
    지정가의 대가(가격이 익절가를 스치기만 하면 안 팔림)는 1시간봉으로 재현할 수 없어 여기에 없다 —
    실거래 저널의 익절 체결로 따로 확인한다.

    진입 쪽 슬리피지는 호출자가 이미 entry_price를 불리하게 옮겨 두었다. R의 분모 `risk`는
    신호가~손절가다(실거래 realized_r과 같은 기준)."""
    limit_take_profit = reason == "take_profit" and take_profit_order_type == "limit"
    fill_exit = exit_price
    if slippage_r_per_side and risk and not limit_take_profit:
        slip = slippage_r_per_side * risk
        fill_exit = exit_price - slip if side == "long" else exit_price + slip
    move = fill_exit - entry_price
    if side == "short":
        move = -move
    pnl_r = move / risk if risk else 0.0
    if risk:
        exit_fee = min(maker_fee_pct_per_side, fee_pct_per_side) if limit_take_profit else fee_pct_per_side
        pnl_r -= ((fee_pct_per_side + exit_fee) * entry_price) / risk
    return pnl_r


def _check_exit_partial(bar: pd.Series, position: dict, fee_pct_per_side: float,
                         partial_at_r: float, partial_fraction: float, breakeven_after_partial: bool,
                         slippage_r_per_side: float = 0.0, risk: float | None = None):
    """부분 익절(스케일 아웃) 모드에서 이 봉의 청산 이벤트를 확인한다. (사유, 청산가, 최종 R배수)를
    돌려주되, 포지션이 아직 완전히 안 닫혔으면(부분 익절만 발생했거나 아무 일도 없으면) 전부 None.

    R배수 산정 방식: 각 구간(부분/잔여)의 손익을 "그 구간이 전체 포지션이었다면 몇 R이었을지"로
    계산한 뒤, 그 구간이 차지하는 비중(fraction)을 곱해서 더한다 — "원래 리스크 1R을 걸었는데
    최종적으로 몇 R을 벌었나"라는 표준적인 블렌디드 R 계산과 동일하다. 수수료도 각 구간의 비중만큼만
    반영한다(그 구간만큼만 실제로 체결되므로).

    가정: 손절과 (부분/최종) 목표가가 같은 봉에서 동시에 닿을 수 있는 경우, 보수적으로 손절이
    먼저라고 가정한다(기존 _check_exit과 동일한 가정).

    risk: R의 분모. 안 넘기면 진입가~최초 손절가를 쓴다(옛 동작). 실거래 `realized_r`과 같은
    기준으로 재려면 **신호가~손절가**를 넘긴다 — 체결이 나빠진 것은 분모를 키우는 게 아니라
    손익을 깎는 것으로 나타나야 하고, 그래야 비부분 경로와 같은 규약이 된다.

    slippage_r_per_side: **청산 쪽** 슬리피지만 여기서 부과한다(진입 쪽은 호출자가 이미
    entry_price를 불리하게 옮겨 놓는다 — 비부분 경로와 같다). 수수료와 같은 모양으로 각 다리에
    비중만큼 걸리므로 합계는 편도 1회분이다. **2026-09-24 이전에는 이 경로가 슬리피지를 아예
    무시했다** — 그대로 게이트에 넣으면 부분 익절만 비용을 안 내는 셈이라 스트레스 판정이
    무의미해진다."""
    side = position["side"]
    entry = position["entry_price"]
    original_stop = position["original_stop_price"]
    original_risk = risk if risk is not None else abs(entry - original_stop)
    high, low = bar["high"], bar["low"]
    fee_r_full = (2 * fee_pct_per_side * entry) / original_risk if (fee_pct_per_side and original_risk) else 0.0
    # 아래 계산에서 fee_r_full이 쓰이는 자리는 전부 "그 다리가 무는 왕복 비용"이다.
    fee_r_full += slippage_r_per_side

    def _leg_r(exit_price: float) -> float:
        """넘겨받은 risk를 분모로 쓰는 R — `_pnl_r`은 분모를 진입가로 고정해서 못 쓴다."""
        diff = exit_price - entry
        if side == "short":
            diff = -diff
        return diff / original_risk if original_risk else 0.0

    if not position["partial_taken"]:
        stop_price = position["stop_price"]
        stop_hit = (low <= stop_price) if side == "long" else (high >= stop_price)
        if stop_hit:
            pnl_r = _leg_r(stop_price) - fee_r_full
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
        leg_pnl_r = _leg_r(stop_price)
        total_r = position["banked_r"] + remaining_fraction * (leg_pnl_r - fee_r_full)
        reason = "breakeven_stop" if stop_price == entry else "stop_loss"
        return reason, stop_price, total_r
    if target_hit:
        leg_pnl_r = _leg_r(target_price)
        total_r = position["banked_r"] + remaining_fraction * (leg_pnl_r - fee_r_full)
        return "take_profit", target_price, total_r
    return None, None, None


def entry_start_bar(df_len: int, regime_sma_period: int) -> int:
    """진입 판단을 시작할 봉 위치.

    레짐 필터가 켜져 있으면 그 SMA가 확정된 뒤부터다 — 실거래 봇은 항상 SMA기간+2봉을 조회해서
    **모든 판단에 필터가 적용된 상태**인데, warm-up 구간(필터 미적용)부터 돌면 그 구간만 다른
    전략이 되어 비교가 깨진다."""
    start = MIN_WARMUP_BARS
    if regime_sma_period > 0:
        start = max(start, regime_sma_period)
    return min(start, df_len)


def gated_signals(
    df: pd.DataFrame,
    *,
    stop_loss_pct: float = STOP_LOSS_PCT,
    atr_period: int = 14,
    adx_threshold: float = RULE_ADX_THRESHOLD,
    regime_sma_period: int = RULE_REGIME_SMA_PERIOD,
    min_atr_to_stop_ratio: float = MIN_ATR_TO_STOP_RATIO,
    sma_period: int = RULE_SMA_PERIOD,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    require_rsi_confirm: bool = True,
    direction_filter: str = DEFAULT_DIRECTION_FILTER,
    signal_fn: Callable[[pd.DataFrame], str | None] | None = None,
) -> dict[int, str]:
    """봉 위치 -> 진입 게이트를 모두 통과한 신호("LONG"/"SHORT"). 통과 못 한 봉은 아예 없다.

    **포지션 상태와 무관하게** 계산한다 — 그래서 단일 종목 백테스트(`run_backtest`)와 포트폴리오
    시뮬레이션(`portfolio.simulate_portfolio`)이 이 함수 하나를 공유할 수 있다. 동시보유 한도
    때문에 어떤 진입이 버려지는지는 포트폴리오 쪽에서 결정할 일이고, "그 봉에 진입 후보가
    있었는가"는 그 결정과 무관하게 같아야 한다.

    순서는 실거래 봇 `_evaluate_symbol`과 같다: 신호 -> 레짐 게이트 -> 저변동 게이트. 판정은
    `futures_strategy`의 같은 함수를 그대로 부른다(조건을 두 군데 적지 않기 위함).

    각 시점의 레짐 SMA와 ATR은 그 시점까지의 종가만으로 계산되므로 미래 정보를 쓰지 않는다."""
    regime_above = regime_valid = None
    if regime_sma_period > 0:
        regime_sma = df["close"].rolling(regime_sma_period).mean()
        regime_above = df["close"] > regime_sma
        regime_valid = ~regime_sma.isna()

    vol_ratio = None
    if min_atr_to_stop_ratio > 0 and stop_loss_pct > 0:
        # atr_to_stop_ratio()와 같은 정의(ATR / 종가 / 손절폭%)를 시리즈로 한 번에 계산한다 —
        # 봉마다 그 함수를 부르면 매번 ATR 전체를 다시 계산해서 종목당 수만 번이 된다.
        vol_ratio = atr(df, atr_period) / df["close"] / stop_loss_pct

    out: dict[int, str] = {}
    for i in range(entry_start_bar(len(df), regime_sma_period), len(df)):
        window = df.iloc[max(0, i - SIGNAL_LOOKBACK_BARS + 1):i + 1]
        if signal_fn is not None:
            signal = signal_fn(window)
        else:
            signal = detect_signal(
                window, adx_threshold=adx_threshold, sma_period=sma_period, rsi_period=rsi_period,
                rsi_threshold=rsi_threshold, require_rsi_confirm=require_rsi_confirm,
                direction_filter=direction_filter,
            )
        if signal is None:
            continue

        if regime_above is not None:
            above = bool(regime_above.iloc[i]) if regime_valid.iloc[i] else None
            signal = apply_regime_filter(signal, above)
            if signal is None:
                continue

        if vol_ratio is not None:
            ratio = vol_ratio.iloc[i]
            ratio = None if pd.isna(ratio) else float(ratio)
            if not passes_volatility_floor(ratio, min_atr_to_stop_ratio):
                continue

        out[i] = signal
    return out


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
    fee_pct_per_side: float = FEE_PCT_PER_SIDE,
    slippage_r_per_side: float = 0.0,
    take_profit_order_type: str = TAKE_PROFIT_ORDER_TYPE,
    maker_fee_pct_per_side: float = MAKER_FEE_PCT_PER_SIDE,
    regime_sma_period: int = RULE_REGIME_SMA_PERIOD,
    min_atr_to_stop_ratio: float = MIN_ATR_TO_STOP_RATIO,
    sma_period: int = RULE_SMA_PERIOD,
    rsi_period: int = 14,
    rsi_threshold: float = 50.0,
    require_rsi_confirm: bool = True,
    direction_filter: str = DEFAULT_DIRECTION_FILTER,
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

    fee_pct_per_side: 편도 수수료율. 왕복 비용을 진입가 기준 노셔널로 근사해 R배수에서
    차감한다. **기본값은 config의 FEE_PCT_PER_SIDE**(2026-09-22 이전에는 0.0이었다) — 손절폭
    1.25%에서 왕복 수수료는 0.064R이고 이 전략의 건당 기대값과 같은 크기라, 기본값이 0이면
    엔진을 직접 부르는 모든 코드가 조용히 낙관적인 결과를 낸다. 수수료 없는 결과를 일부러
    보려면 명시적으로 0을 넘길 것.

    take_profit_order_type / maker_fee_pct_per_side: 익절 청산의 체결 방식(`exit_pnl_r` 참고).
    기본값은 실거래 설정 — "limit"이면 익절 청산만 메이커 수수료에 청산 슬리피지가 없다.
    부분 익절 경로(use_partial_tp)는 이 구분 없이 전부 시장가로 계산한다(실거래에 없는 모드).

    slippage_r_per_side: 진입/청산 각각에 **손절폭의 몇 배**만큼 불리한 체결을 가정한다
    (0.1이면 편도 0.1R씩, 왕복 0.2R). R로 받는 이유는 수수료와 같은 단위로 더해서 "이 전략이
    비용을 얼마까지 감당하는지"를 한 축으로 볼 수 있기 때문.

    **왜 필요한가:** 백테스트는 신호 봉 **종가에 즉시** 체결된다고 보지만 실거래 봇은 그 봉이
    마감된 뒤 최대 `POLL_INTERVAL_SECONDS`만큼 지나서 시장가로 들어가고, 신호가에서 현재가가
    `MAX_ENTRY_PRICE_DRIFT_R`(기본 0.5R)까지 벌어져도 진입을 허용한다 — 즉 **구조적으로 최대
    0.5R까지 불리한 체결이 허용된다.** 이 전략의 백테스트 건당 기대값이 +0.12R이므로 그 절반의
    괴리만 있어도 기대값이 사라진다. 손절/익절 가격은 실거래와 같이 **신호 봉 종가 기준**으로
    두고 체결가만 불리하게 옮기므로, 이 값이 재는 것은 정확히 그 구조적 괴리다.

    수수료와 달리 기본값이 0인 이유: 관측값이 없다. 저널의 `realized_r`은 신호 봉 종가를
    진입가로 써서 진입 슬리피지를 애초에 못 재고 있었다(2026-09-22부터 실제 체결가를 같이
    남기기 시작했으므로, 표본이 쌓이면 그 실측으로 기본값을 정할 수 있다).

    regime_sma_period / min_atr_to_stop_ratio: **실거래 봇이 detect_signal 뒤에 거는 두 개의
    진입 게이트**(레짐 숏차단·저변동 차단)를 백테스트에서도 그대로 적용한다. 기본값은 config의
    실거래값이라, 인자를 안 넘기면 백테스트가 실거래와 같은 규칙을 돈다. 0으로 두면 그 게이트만
    꺼진다.

    2026-09-22 이전에는 이 두 게이트가 엔진에 아예 없었다 — 레짐 필터는 `signal_fn`을 손으로
    주입하는 스크립트 하나(`run_regime_filter_walkforward.py`)만 썼고 **저변동 필터는 어떤
    백테스트 경로에도 없었다.** 그래서 `run_backtest`/`run_grid_search`/`run_walk_forward`의
    기본 경로는 실거래가 거부하는 진입을 포함한 다른 전략을 측정하고 있었다.

    두 게이트는 `futures_strategy.apply_regime_filter`/`passes_volatility_floor`를 그대로 부른다
    (실거래 봇과 같은 함수 — 조건을 두 군데 적지 않기 위함). 각 시점의 레짐 SMA와 ATR은 그
    시점까지의 종가만으로 계산되므로 미래 정보를 쓰지 않는다.

    target_series: 주어지면 각 봉의 값을 그 시점의 익절 목표가로 쓴다(예: 볼린저밴드 중간선) —
    df와 같은 인덱스를 가져야 한다. stop_mode로 계산된 target_price는 이 경우 무시되고 손절가만
    그대로 쓰인다.

    use_partial_tp: True면 부분 익절(스케일 아웃)을 시뮬레이션한다(2026-09-24부터 이 경로도
    수수료와 슬리피지를 비부분 경로와 **같은 규약**으로 문다 — 그 전에는 슬리피지를 아예 무시해서
    부분 익절만 비용을 안 내는 결과가 나왔다). — partial_at_r(원래 리스크의
    몇 배)에 도달하면 포지션의 partial_fraction만큼 청산해서 그만큼의 R을 확정하고,
    breakeven_after_partial=True(기본값)면 나머지 물량의 손절을 진입가로 옮긴 뒤, 나머지는
    (stop_mode로 계산된) 최종 target_price까지 계속 보유한다. use_breakeven/target_series와는
    동시에 쓸 수 없다(단순화를 위해 조합 검증은 안 함 — 호출자 책임)."""
    if stop_mode not in ("fixed", "atr"):
        raise ValueError(f"invalid stop_mode: {stop_mode!r}")

    atr_series = atr(df, atr_period) if stop_mode == "atr" else None

    # 진입 후보를 한 번에 계산한다 — 포트폴리오 시뮬레이터(`portfolio.simulate_portfolio`)가
    # **같은 함수**를 쓰므로 단일 종목 결과와 포트폴리오 결과의 진입 규칙이 어긋날 수 없다.
    signals = gated_signals(
        df, stop_loss_pct=stop_loss_pct, atr_period=atr_period, adx_threshold=adx_threshold,
        regime_sma_period=regime_sma_period, min_atr_to_stop_ratio=min_atr_to_stop_ratio,
        sma_period=sma_period, rsi_period=rsi_period, rsi_threshold=rsi_threshold,
        require_rsi_confirm=require_rsi_confirm, direction_filter=direction_filter,
        signal_fn=signal_fn,
    )

    trades: list[dict] = []
    position: dict | None = None

    for i in range(entry_start_bar(len(df), regime_sma_period), len(df)):
        bar = df.iloc[i]

        if position is not None:
            if use_partial_tp:
                # R의 분모는 비부분 경로와 같은 **신호가~손절가**다 — 두 경로가 다른 규약을
                # 쓰면 원장의 행끼리 비교가 안 된다.
                risk = abs(position["signal_price"] - position["original_stop_price"])
                reason, exit_price, pnl_r = _check_exit_partial(
                    bar, position, fee_pct_per_side, partial_at_r, partial_fraction,
                    breakeven_after_partial, slippage_r_per_side, risk,
                )
                if reason is None and use_max_hold and (i - position["entry_index"]) >= max_hold_bars:
                    exit_price = float(bar["close"])
                    remaining_fraction = (1 - partial_fraction) if position["partial_taken"] else 1.0
                    move = exit_price - position["entry_price"]
                    if position["side"] == "short":
                        move = -move
                    leg_pnl_r = move / risk if risk else 0.0
                    cost_r = ((2 * fee_pct_per_side * position["entry_price"]) / risk
                              if (fee_pct_per_side and risk) else 0.0) + slippage_r_per_side
                    pnl_r = position["banked_r"] + remaining_fraction * (leg_pnl_r - cost_r)
                    reason = "max_hold"
            else:
                dynamic_target = float(target_series.iloc[i]) if target_series is not None else None
                reason, exit_price = _check_exit(bar, position, use_breakeven, breakeven_at_r, dynamic_target)
                if reason is None and use_max_hold and (i - position["entry_index"]) >= max_hold_bars:
                    reason, exit_price = "max_hold", float(bar["close"])
                if reason is not None:
                    # R의 분모는 **신호가~손절가**다(실거래 realized_r과 같은 기준) — 체결이
                    # 나빠진 것은 분모를 키우는 게 아니라 손익을 깎는 것으로 나타나야 한다.
                    risk = abs(position["signal_price"] - position["original_stop_price"])
                    pnl_r = exit_pnl_r(
                        position["side"], position["entry_price"], exit_price, risk, reason,
                        fee_pct_per_side=fee_pct_per_side, slippage_r_per_side=slippage_r_per_side,
                        take_profit_order_type=take_profit_order_type,
                        maker_fee_pct_per_side=maker_fee_pct_per_side)

            if reason is not None:
                trades.append({
                    "side": position["side"], "entry_index": position["entry_index"], "exit_index": i,
                    "entry_price": position["entry_price"], "exit_price": exit_price, "reason": reason,
                    "pnl_r": pnl_r, "hold_bars": i - position["entry_index"],
                })
                position = None
            continue

        signal = signals.get(i)
        if signal is None:
            continue

        signal_price = float(bar["close"])
        entry_price = signal_price
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

        # 손절/익절 가격은 신호 봉 종가 기준으로 확정한 뒤(실거래와 동일), 체결가만 불리하게
        # 옮긴다 — 실거래에서 벌어지는 일이 정확히 이것이다(브라켓은 신호가로 계산되는데
        # 시장가 진입은 그보다 늦게, 최대 MAX_ENTRY_PRICE_DRIFT_R만큼 벌어진 가격에 된다).
        if slippage_r_per_side:
            drift = slippage_r_per_side * abs(signal_price - stop_price)
            entry_price = signal_price + drift if side == "long" else signal_price - drift

        position = {
            "side": side, "entry_price": entry_price, "entry_index": i,
            "stop_price": stop_price, "target_price": target_price,
            # R의 기준 리스크는 **신호가~손절가**다(실거래의 realized_r과 같은 분모) — 체결이
            # 나빠진 것은 분모를 키우는 게 아니라 손익을 깎는 것으로 나타나야 한다.
            "original_stop_price": stop_price, "signal_price": signal_price,
            "breakeven_moved": False,
        }
        if use_partial_tp:
            # 부분 익절 목표가도 **신호가~손절가**를 1R로 잡는다 — banked_r이 명목 partial_at_r을
            # 그대로 적립하므로, 목표가를 다른 분모로 잡으면 "적립한 R"과 "실제로 간 R"이 어긋난다.
            original_risk = abs(signal_price - stop_price)
            partial_target_price = entry_price + partial_at_r * original_risk if side == "long" \
                else entry_price - partial_at_r * original_risk
            position.update({
                "partial_taken": False, "partial_target_price": partial_target_price, "banked_r": 0.0,
            })

    return trades
