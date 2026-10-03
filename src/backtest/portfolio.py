"""여러 종목을 **한 계좌에서** 굴렸다면 어떻게 됐는지 — 동시보유 한도와 서킷브레이커를 적용한
통합 진입 시뮬레이션.

왜 종목별 백테스트를 합산하는 것으로는 안 되는가:

1. **동시보유 한도가 진입을 버린다.** `run_backtest`를 종목마다 따로 돌려 합치면 12종목이
   동시에 신호를 냈을 때 12개가 전부 진입된 것으로 세는데, 실거래 봇은 `MAX_CONCURRENT_POSITIONS`
   개만 받고 나머지는 `skipped_max_positions`로 버린다. 버려진 진입 때문에 그 종목의 **이후**
   거래 순서도 달라진다(포지션을 안 들고 있으면 다음 신호를 받을 수 있다) — 그래서 사후에
   거래 목록을 걸러내는 것으로는 재현할 수 없고, 봉을 따라가며 다시 시뮬레이션해야 한다.
2. **서킷브레이커는 "그 시점에 알 수 있었던 것"만 봐야 한다.** 연속손실은 **청산이 기록된
   뒤에만** 알 수 있다. 진입 시각 기준으로 세면 동시보유를 늘릴수록 결과가 좋아지는 것처럼
   보인다 — 이 오류로 실제로 상한 12를 잘못 권고한 적이 있다(UPDATE_LOG 2026-09-09).
   이 모듈은 청산 시각 기준으로만 연속손실/일일손실을 갱신한다.
3. 낙폭은 거래를 시간순으로 합쳐야 나온다(`report.portfolio_stats` 참고).

**연속손실 서킷브레이커는 실거래에서 "걸쇠"다.** `state.compute_consecutive_losses`는 저널을
뒤에서부터 훑어 손실이 아닌 청산을 만날 때까지 센다 — 한도에 닿으면 신규 진입이 막히고, 막히면
새 청산이 안 생기므로 **카운터가 저절로 내려가지 않는다.** 풀리는 경로는 두 개뿐이다: (a) 이미
열려 있던 포지션이 이익으로 닫히거나, (b) 사람이 대시보드/텔레그램에서 수동 리셋
(`futures_rule_bot.reset_consecutive_losses`)을 누르는 것. 실제 저널에 그 수동 리셋이 6주간
6~7회 찍혀 있다(2026-09-22 확인: 데모 6회, 실계좌 7회 — 평균 6~7일에 한 번).

그래서 **365일을 사람 없이 시뮬레이션하면 첫 5연패에서 영구 정지된다.** 이걸 모델링하지 않으면
동시보유 상한을 6/8/10/12로 바꿔도 결과가 완전히 동일하게 나온다(정지 이후엔 상한이 무의미하므로) —
실제로 그렇게 나왔고, 그건 상한에 대해 아무것도 말해주지 않는 숫자다. `breaker_reset` 파라미터로
"사람이 언제 리셋하는가"를 명시적인 가정으로 만들고, 그 가정에 결과가 얼마나 민감한지를 같이
보고한다.

진입 후보는 `engine.gated_signals`를 그대로 쓴다 — 단일 종목 백테스트와 **같은 함수**라서 진입
규칙이 어긋날 수 없다. 청산도 `engine._check_exit`을 그대로 쓴다.

**같은 봉에 여러 종목이 신호를 내면 누가 자리를 받는가**는 결과를 바꾸는 임의적 선택이다.
실거래 봇은 `FUTURES_SYMBOLS` 순서로 순회하므로 그것이 기본값이고, `seed`를 주면 봉마다 순서를
섞는다 — 여러 seed로 돌려 분포를 보는 것이 한 번의 결과보다 정직하다(`simulate_many`).
"""

import random
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from src.backtest.engine import (
    _check_exit,
    _check_exit_partial,
    end_of_data_exit,
    entry_start_bar,
    exit_pnl_r,
    gated_signals,
)
from src.core.config import (
    CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    FEE_PCT_PER_SIDE,
    FUTURES_RISK_PER_TRADE,
    MAKER_FEE_PCT_PER_SIDE,
    MAX_CONCURRENT_POSITIONS,
    RULE_REGIME_SMA_PERIOD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_ORDER_TYPE,
    TAKE_PROFIT_RR,
)
from src.core.futures_strategy import compute_bracket_prices
from src.core.risk import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS_PCT


def daily_loss_limit_r(max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT,
                        risk_per_trade: float = FUTURES_RISK_PER_TRADE) -> float | None:
    """일일 손실 한도를 R로 환산한다. 0 이하면 None(한도 없음).

    실거래의 한도는 **마진 자산 대비 %**인데 이 시뮬레이션은 R로만 계산하므로 환산이 필요하다.
    거래당 리스크가 자산의 `risk_per_trade`이므로 1R = 자산의 risk_per_trade%이고, 따라서
    `max_daily_loss_pct / risk_per_trade`R을 잃으면 한도에 닿는다(예: 5% / 0.75% = 6.67R).

    **이 환산은 사이징이 하루 내내 일정하다고 가정한다** — 실거래는 자산이 줄면 사이징도 줄어서
    한도에 닿기까지 조금 더 걸린다. 즉 이 시뮬레이션은 한도를 약간 **이르게** 발동시키는
    보수적인 쪽이다."""
    if max_daily_loss_pct <= 0 or risk_per_trade <= 0:
        return None
    return max_daily_loss_pct / risk_per_trade


@dataclass
class _Position:
    symbol: str
    side: str
    entry_price: float
    signal_price: float
    stop_price: float
    target_price: float
    original_stop_price: float
    entry_index: int
    entry_step: int = 0     # 공통 타임라인 위치 — 사이징이 "진입 시점의 자산"을 알아야 한다(3.7)
    # 진입 순간 열려 있던 포지션 개수(자기 자신 포함) — 동시보유가 성과와 어떻게 얽히는지
    # 보려면 거래마다 남아 있어야 한다(상한을 정하는 근거가 정확히 이 질문이다).
    concurrent_at_entry: int = 1
    breakeven_moved: bool = False
    # 부분 익절(스케일 아웃) 상태 — use_partial_tp일 때만 의미가 있다.
    partial_taken: bool = False
    banked_r: float = 0.0
    partial_target_price: float | None = None

    def as_dict(self) -> dict:
        """engine._check_exit / _check_exit_partial이 기대하는 딕셔너리 형태."""
        return {
            "side": self.side, "entry_price": self.entry_price,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "original_stop_price": self.original_stop_price,
            "signal_price": self.signal_price,
            "breakeven_moved": self.breakeven_moved,
            "partial_taken": self.partial_taken, "banked_r": self.banked_r,
            "partial_target_price": self.partial_target_price,
        }


def _timeline(df_by_symbol: dict[str, pd.DataFrame]) -> list:
    """모든 종목의 봉 시각을 합친 정렬된 타임라인.

    종목마다 상장일이 달라 길이가 다르므로(예: SOXL 3110봉 vs BTC 8760봉) 봉 **인덱스**로
    맞추면 서로 다른 시각의 봉이 같은 시점으로 묶인다. `timestamp` 열이 있으면 그것으로,
    없으면 인덱스로 맞춘다(테스트용 짧은 df)."""
    stamps = set()
    for df in df_by_symbol.values():
        keys = df["timestamp"] if "timestamp" in df.columns else df.index.to_series()
        stamps.update(keys.tolist())
    return sorted(stamps)


def _position_lookup(df_by_symbol: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """종목별 {봉 시각: 정수 위치}."""
    out = {}
    for symbol, df in df_by_symbol.items():
        keys = df["timestamp"] if "timestamp" in df.columns else df.index.to_series()
        out[symbol] = {key: i for i, key in enumerate(keys.tolist())}
    return out


def simulate_portfolio(
    df_by_symbol: dict[str, pd.DataFrame],
    *,
    max_concurrent_positions: int = MAX_CONCURRENT_POSITIONS,
    max_consecutive_losses: int | None = MAX_CONSECUTIVE_LOSSES,
    breaker_reset: str = "cooldown",
    cooldown_hours: float = CONSECUTIVE_LOSS_COOLDOWN_HOURS,
    max_daily_loss_r: float | None = None,
    stop_loss_pct: float = STOP_LOSS_PCT,
    take_profit_rr: float = TAKE_PROFIT_RR,
    fee_pct_per_side: float = FEE_PCT_PER_SIDE,
    slippage_r_per_side: float = 0.0,
    take_profit_order_type: str = TAKE_PROFIT_ORDER_TYPE,
    maker_fee_pct_per_side: float = MAKER_FEE_PCT_PER_SIDE,
    regime_sma_period: int = RULE_REGIME_SMA_PERIOD,
    use_breakeven: bool = False,
    breakeven_at_r: float = 1.0,
    use_max_hold: bool = False,
    max_hold_bars: int = 72,
    use_partial_tp: bool = False,
    partial_at_r: float = 1.0,
    partial_fraction: float = 0.5,
    breakeven_after_partial: bool = True,
    seed: int | None = None,
    signals_by_symbol: dict[str, dict[int, str]] | None = None,
    **signal_params,
) -> dict:
    """전 종목을 한 계좌에서 굴리는 시뮬레이션. 거래 목록과 차단 통계를 돌려준다.

    breaker_reset: 연속손실 카운터가 **언제 풀리는지**.
      - "cooldown"(기본, 2026-10-03): **실거래와 같은 규칙** — 손실 뒤 `cooldown_hours` 동안 새
        손실이 없으면 연속이 끊긴다(`state.compute_consecutive_losses`). 손실 사이 간격이 그보다
        짧으면 계속 센다. 정지 중엔 새 청산이 없으므로 마지막 손실 뒤 그 시간이 지나야 풀린다.
        외부 검토 3.2: 그 전 기본값 "daily"는 실거래에 없는 규칙(자정마다 0)이었다.
      - "daily": 로컬 날짜가 바뀌면 리셋(옛 기본값 — 사람이 매일 리셋한다는 가정). 사람이 하루에 한 번은 확인한다는 가정이고,
        일일 손실 한도가 리셋되는 경계와 같은 기준이라 하루의 정의가 어긋나지 않는다.
      - "never": 실거래 코드 그대로 — 이익 청산이나 수동 리셋 없이는 안 풀린다. 365일에서는
        사실상 영구 정지가 되므로 **상한 비교에는 쓸 수 없다**(그 사실을 보여주는 용도).
      - "off": 연속손실 브레이커 자체를 끈다(`max_consecutive_losses=None`과 같다).
    저널의 실제 수동 리셋 빈도는 6~7일에 한 번이라 "daily"는 낙관적인 쪽이다 — 결론을 낼 때
    이 가정에 대한 민감도를 같이 봐야 한다.

    max_daily_loss_r: 하루 누적 실현 R이 이 값만큼 마이너스가 되면 그날은 신규 진입 중단.
    None이면 `daily_loss_limit_r()`로 config에서 환산한다. 0 이하를 주면 한도를 끈다.

    signals_by_symbol: 미리 계산한 진입 후보(`engine.gated_signals` 결과). 같은 데이터로 여러
    설정을 돌릴 때 신호 계산을 재사용하기 위한 것 — 안 주면 여기서 계산한다.

    seed: 같은 봉에 여러 종목이 신호를 낼 때의 배정 순서를 섞는다. None이면 df_by_symbol의
    키 순서(= 실거래 봇의 FUTURES_SYMBOLS 순회 순서)를 쓴다.
    """
    if max_daily_loss_r is None:
        max_daily_loss_r = daily_loss_limit_r()

    symbols = list(df_by_symbol)
    if signals_by_symbol is None:
        signals_by_symbol = {
            s: gated_signals(df, stop_loss_pct=stop_loss_pct,
                              regime_sma_period=regime_sma_period, **signal_params)
            for s, df in df_by_symbol.items()
        }
    at = _position_lookup(df_by_symbol)
    starts = {s: entry_start_bar(len(df), regime_sma_period) for s, df in df_by_symbol.items()}
    rng = random.Random(seed) if seed is not None else None

    open_positions: dict[str, _Position] = {}
    trades: list[dict] = []
    blocked = {"max_positions": 0, "consecutive_losses": 0, "daily_loss": 0}

    # 서킷브레이커 상태 — **청산 시점에만** 갱신된다(진입 시점에 알 수 없는 정보를 쓰지 않기 위함).
    consecutive_losses = 0
    last_loss_at = None     # "cooldown" 판정용 — 마지막으로 센 손실의 청산 시각
    cooldown = timedelta(hours=cooldown_hours) if cooldown_hours and cooldown_hours > 0 else None
    day_r: dict[object, float] = {}
    breaker_day = None      # "daily" 리셋 판정용 — 날짜가 바뀌면 카운터를 0으로
    breaker_resets = 0

    for step, stamp in enumerate(_timeline(df_by_symbol)):
        # 하루 경계는 UTC다. 실거래의 일일 손실 한도는 PC 로컬 자정(KST면 UTC 15시)에 리셋돼
        # 9시간 어긋난다(외부 검토 3.2) — 알고 둔 차이다. 로컬 시간대로 맞추면 백테스트 결과가
        # 실행한 PC의 시간대에 따라 달라지고, 한도(자산 5% = 10R)는 거의 안 걸려 영향이 작다.
        day = stamp.date() if hasattr(stamp, "date") else None
        # 청산은 봉 **안에서** 일어나지만 1시간봉으로는 언제인지 모른다 — 봉 시작 시각을 쓴다.
        timed = cooldown is not None and hasattr(stamp, "date")

        # cooldown: 마지막 손실 뒤 그 시간이 지났으면 연속이 끊긴다(실거래가 다음 판정 때 0으로 센다).
        if (breaker_reset == "cooldown" and timed and last_loss_at is not None
                and consecutive_losses > 0 and stamp - last_loss_at >= cooldown):
            if consecutive_losses >= (max_consecutive_losses or 0) > 0:
                breaker_resets += 1
            consecutive_losses = 0

        # 날짜가 바뀌면 연속손실 카운터를 리셋한다(breaker_reset="daily") — 실거래에서 사람이
        # 수동 리셋을 누르는 개입을 "하루에 한 번"으로 모델링한 것. 안 그러면 첫 5연패에서
        # 영구 정지되어 이 시뮬레이션이 상한에 대해 아무것도 말해주지 못한다.
        if breaker_reset == "daily" and day is not None and day != breaker_day:
            if breaker_day is not None and consecutive_losses > 0:
                breaker_resets += 1
            breaker_day = day
            consecutive_losses = 0

        # 1) 먼저 청산을 처리한다. 같은 봉에서 자리가 비면 그 자리는 다음 봉부터 쓸 수 있어야
        #    하는 게 아니라 **이 봉에 바로** 쓸 수 있다 — 실거래 봇도 한 사이클에서 청산을
        #    반영한 뒤 진입을 판단한다(run_once가 청산 정리 후 _evaluate_symbol을 부른다).
        for symbol in list(open_positions):
            i = at[symbol].get(stamp)
            if i is None:
                continue  # 이 종목엔 이 시각 봉이 없다(상장 전/데이터 결측)
            position = open_positions[symbol]
            state = position.as_dict()
            bar = df_by_symbol[symbol].iloc[i]
            risk = abs(position.signal_price - position.original_stop_price)
            pnl_r = None    # 부분 익절 경로는 다리마다 비중이 달라 R을 스스로 계산해 돌려준다

            if use_partial_tp:
                reason, exit_price, pnl_r = _check_exit_partial(
                    bar, state, fee_pct_per_side, partial_at_r, partial_fraction,
                    breakeven_after_partial, slippage_r_per_side, risk)
                position.partial_taken = state["partial_taken"]
                position.banked_r = state["banked_r"]
                position.stop_price = state["stop_price"]
                if reason is None and use_max_hold and (i - position.entry_index) >= max_hold_bars:
                    exit_price = float(bar["close"])
                    remaining = (1 - partial_fraction) if position.partial_taken else 1.0
                    move = exit_price - position.entry_price
                    if position.side == "short":
                        move = -move
                    cost_r = ((2 * fee_pct_per_side * position.entry_price) / risk
                              if (fee_pct_per_side and risk) else 0.0) + slippage_r_per_side
                    pnl_r = position.banked_r + remaining * ((move / risk if risk else 0.0) - cost_r)
                    reason = "max_hold"
            else:
                reason, exit_price = _check_exit(bar, state, use_breakeven, breakeven_at_r)
                position.breakeven_moved = state["breakeven_moved"]
                position.stop_price = state["stop_price"]
                if reason is None and use_max_hold and (i - position.entry_index) >= max_hold_bars:
                    reason, exit_price = "max_hold", float(bar["close"])

            if reason is None:
                continue

            if pnl_r is None:
                pnl_r = exit_pnl_r(
                    position.side, position.entry_price, exit_price, risk, reason,
                    fee_pct_per_side=fee_pct_per_side, slippage_r_per_side=slippage_r_per_side,
                    take_profit_order_type=take_profit_order_type,
                    maker_fee_pct_per_side=maker_fee_pct_per_side)

            trades.append({
                "symbol": symbol, "side": position.side, "reason": reason,
                "entry_index": position.entry_index, "exit_index": i,
                "entry_price": position.entry_price, "exit_price": exit_price,
                "pnl_r": pnl_r, "hold_bars": i - position.entry_index,
                "entry_step": position.entry_step, "exit_step": step,
                "concurrent_at_entry": position.concurrent_at_entry,
            })
            del open_positions[symbol]

            # 서킷브레이커 상태는 여기서만 움직인다.
            if pnl_r < 0:
                if (breaker_reset == "cooldown" and timed and last_loss_at is not None
                        and stamp - last_loss_at >= cooldown):
                    consecutive_losses = 0   # 앞 손실과 간격이 쿨다운 이상 — 새 연속의 시작
                consecutive_losses += 1
                if timed:
                    last_loss_at = stamp
            else:
                consecutive_losses = 0
            if day is not None:
                day_r[day] = day_r.get(day, 0.0) + pnl_r

        # 2) 그다음 진입. 차단 사유는 실거래 봇과 같은 우선순위로 센다.
        candidates = [s for s in symbols
                       if s not in open_positions
                       and at[s].get(stamp) is not None
                       and signals_by_symbol[s].get(at[s][stamp]) is not None
                       and at[s][stamp] >= starts[s]]
        if not candidates:
            continue

        if (breaker_reset != "off" and max_consecutive_losses
                and consecutive_losses >= max_consecutive_losses):
            blocked["consecutive_losses"] += len(candidates)
            continue
        if (max_daily_loss_r and day is not None
                and day_r.get(day, 0.0) <= -max_daily_loss_r):
            blocked["daily_loss"] += len(candidates)
            continue

        if rng is not None:
            rng.shuffle(candidates)
        for symbol in candidates:
            if len(open_positions) >= max_concurrent_positions:
                blocked["max_positions"] += 1
                continue
            i = at[symbol][stamp]
            signal = signals_by_symbol[symbol][i]
            signal_price = float(df_by_symbol[symbol].iloc[i]["close"])
            side = "long" if signal == "LONG" else "short"
            stop_price, target_price = compute_bracket_prices(
                signal_price, side, stop_loss_pct, take_profit_rr)

            entry_price = signal_price
            if slippage_r_per_side:
                drift = slippage_r_per_side * abs(signal_price - stop_price)
                entry_price = signal_price + drift if side == "long" else signal_price - drift

            partial_target_price = None
            if use_partial_tp:
                # 엔진과 같은 규약: 부분 익절 목표가도 **신호가~손절가**를 1R로 잡는다.
                leg_risk = abs(signal_price - stop_price)
                partial_target_price = (entry_price + partial_at_r * leg_risk if side == "long"
                                        else entry_price - partial_at_r * leg_risk)

            position = _Position(
                symbol=symbol, side=side, entry_price=entry_price, signal_price=signal_price,
                stop_price=stop_price, target_price=target_price,
                original_stop_price=stop_price, entry_index=i, entry_step=step,
                concurrent_at_entry=len(open_positions) + 1,
                partial_target_price=partial_target_price)
            open_positions[symbol] = position

    # 구간 끝에 열려 있는 포지션은 마지막 종가로 닫아 성과에 넣는다(외부 검토 3.7) — 예전엔
    # 이름만 남기고 뺐다. 부분 익절 경로(실거래에 없는 모드)는 옛 동작 그대로 뺀다.
    still_open = sorted(open_positions)
    if not use_partial_tp:
        last_step = step if open_positions else 0
        for symbol in still_open:
            position = open_positions.pop(symbol)
            i, exit_price = end_of_data_exit(df_by_symbol[symbol])
            risk = abs(position.signal_price - position.original_stop_price)
            trades.append({
                "symbol": symbol, "side": position.side, "reason": "end_of_data",
                "entry_index": position.entry_index, "exit_index": i,
                "entry_price": position.entry_price, "exit_price": exit_price,
                "pnl_r": exit_pnl_r(
                    position.side, position.entry_price, exit_price, risk, "end_of_data",
                    fee_pct_per_side=fee_pct_per_side, slippage_r_per_side=slippage_r_per_side,
                    take_profit_order_type=take_profit_order_type,
                    maker_fee_pct_per_side=maker_fee_pct_per_side),
                "hold_bars": i - position.entry_index,
                "entry_step": position.entry_step, "exit_step": last_step,
                "concurrent_at_entry": position.concurrent_at_entry,
            })

    return {
        "trades": trades,
        "blocked": blocked,
        # 구간 끝에 열려 있던 포지션(위에서 마지막 종가로 닫아 성과에 넣었다).
        "still_open": still_open,
        "max_concurrent_positions": max_concurrent_positions,
        "max_daily_loss_r": max_daily_loss_r,
        # 가정한 수동 리셋이 실제로 몇 번 필요했는지 — 저널의 실측(6주에 6~7회)과 비교해서
        # 이 가정이 현실적인지 판단하는 데 쓴다.
        "breaker_reset": breaker_reset,
        "breaker_resets_needed": breaker_resets,
    }


def trades_by_symbol(result: dict) -> dict[str, list[dict]]:
    """simulate_portfolio 결과를 `report.portfolio_stats`/`summarize`가 받는 형태로."""
    out: dict[str, list[dict]] = {}
    for trade in result["trades"]:
        out.setdefault(trade["symbol"], []).append(trade)
    return out


def simulate_many(df_by_symbol: dict[str, pd.DataFrame], *, seeds=range(20), **kwargs) -> list[dict]:
    """배정 순서를 여러 번 바꿔가며 돌린 결과 목록.

    같은 봉에 여러 종목이 신호를 내면 누가 자리를 받는지는 임의적 선택이고 그것이 결과를 바꾼다 —
    **한 번의 숫자를 결론으로 쓰면 그 임의성을 성과로 착각한다.** 신호 계산은 한 번만 하고
    재사용한다(seed마다 다시 계산하면 20배 느려지는데 신호는 seed와 무관하다)."""
    signal_params = {k: v for k, v in kwargs.items()
                      if k not in ("signals_by_symbol", "seed")}
    signals = kwargs.get("signals_by_symbol")
    if signals is None:
        gate_keys = ("stop_loss_pct", "atr_period", "adx_threshold", "regime_sma_period",
                      "min_atr_to_stop_ratio", "sma_period", "rsi_period", "rsi_threshold",
                      "require_rsi_confirm", "direction_filter", "htf_hours", "signal_fn")
        gate_args = {k: v for k, v in signal_params.items() if k in gate_keys}
        signals = {s: gated_signals(df, **gate_args) for s, df in df_by_symbol.items()}

    return [simulate_portfolio(df_by_symbol, seed=seed, signals_by_symbol=signals, **signal_params)
            for seed in seeds]
