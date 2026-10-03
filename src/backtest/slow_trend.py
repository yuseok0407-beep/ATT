"""대안 전략 #2: 일봉 채널 돌파 추세 추종(사전 등록 `docs/ALT_SLOW_TREND.md`, 2026-10-04).

그날 종가가 직전 N일 최고가를 넘으면 롱, 최저가 아래면 숏. 반대쪽 N/2일 채널을 깨거나 ATR 2배
손절에 닿으면 청산. 거래당 위험은 자산의 일정 비율(손절까지의 손실 금액)로 고정한다.

미래 정보 차단: 날짜 t의 판단은 t까지의 일봉(진입은 t 종가)만 쓰고, 후보군은 t **이전** 데이터로
정한다. 손절은 진입 다음 날부터 본다. `tests/test_slow_trend.py`가 확인한다.

자산은 매일 **시가평가**(열린 포지션의 평가손익 포함)로 기록한다 — 1시간봉 전략의 낙폭이 실현 손익
기준이라 과소평가된다는 외부 검토 3.7 지적을 여기서는 처음부터 피한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Params:
    entry_days: int = 20
    exit_days: int = 10
    atr_days: int = 20
    stop_atr: float = 2.0
    risk_per_trade: float = 0.005
    max_positions: int = 10
    universe_size: int = 30
    volume_days: int = 30
    min_history_days: int = 90
    fee_pct_per_side: float = 0.0005


def daily_ohlc(hourly: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """1시간봉 -> UTC 일봉 패널 {open, high, low, close, qv}. 24봉이 다 있는 날만."""
    cols = {k: {} for k in ("open", "high", "low", "close", "qv")}
    for symbol, df in hourly.items():
        if df.empty:
            continue
        frame = df.set_index("timestamp")
        day = frame.index.floor("D")
        g = frame.groupby(day)
        full = g.size()[lambda s: s == 24].index
        cols["open"][symbol] = g["open"].first().reindex(full)
        cols["high"][symbol] = g["high"].max().reindex(full)
        cols["low"][symbol] = g["low"].min().reindex(full)
        cols["close"][symbol] = g["close"].last().reindex(full)
        cols["qv"][symbol] = (frame["close"] * frame["volume"]).groupby(day).sum().reindex(full)
    return {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}


def _atr(p: dict[str, pd.DataFrame], days: int) -> pd.DataFrame:
    prev = p["close"].shift(1)
    tr = pd.concat([(p["high"] - p["low"]), (p["high"] - prev).abs(), (p["low"] - prev).abs()]).groupby(level=0).max()
    return tr.reindex(p["close"].index).rolling(days, min_periods=days).mean()


def universe(p: dict[str, pd.DataFrame], i: int, params: Params) -> list[str]:
    """i번째 날의 후보군 — i **이전** 날들의 데이터만. 거래대금 순으로 정렬해 돌려준다."""
    if i < 1:
        return []
    past_close = p["close"].iloc[:i]
    counts = past_close.notna().sum()
    alive = past_close.iloc[-1].notna()
    eligible = counts.index[(counts >= params.min_history_days) & alive]
    qv = p["qv"].iloc[max(0, i - params.volume_days):i][eligible].sum()
    return list(qv.sort_values(ascending=False).index[:params.universe_size])


def simulate(p: dict[str, pd.DataFrame], funding: pd.DataFrame, params: Params = Params()) -> dict:
    """일별 시가평가 자산 경로와 거래 목록."""
    close, high, low, open_ = p["close"], p["high"], p["low"], p["open"]
    dates = close.index
    hi_n = high.shift(1).rolling(params.entry_days, min_periods=params.entry_days).max()
    lo_n = low.shift(1).rolling(params.entry_days, min_periods=params.entry_days).min()
    hi_x = high.shift(1).rolling(params.exit_days, min_periods=params.exit_days).max()
    lo_x = low.shift(1).rolling(params.exit_days, min_periods=params.exit_days).min()
    atr = _atr(p, params.atr_days)
    fund = funding.reindex(index=dates, columns=close.columns).fillna(0.0)
    fee = params.fee_pct_per_side

    cash = 1.0
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_rows = []
    started = None

    def close_position(symbol, pos, fill, date, reason):
        nonlocal cash
        cash += pos["side"] * pos["qty"] * (fill - pos["entry"]) - fee * pos["qty"] * fill
        trades.append({"symbol": symbol, "side": pos["side"], "entry_date": pos["date"], "exit_date": date,
                       "entry": pos["entry"], "exit": fill, "reason": reason,
                       "pnl": pos["side"] * pos["qty"] * (fill - pos["entry"]) - fee * pos["qty"] * (fill + pos["entry"])
                              + pos["funding"],
                       "risk": pos["risk"]})

    for i, date in enumerate(dates):
        # 1) 펀딩(보유 중인 포지션) — 그날 묶음
        for symbol, pos in positions.items():
            amount = -pos["side"] * pos["qty"] * pos["entry"] * fund.at[date, symbol]
            cash += amount
            pos["funding"] += amount

        # 2) 청산 — 손절(갭이면 시가) 먼저, 그다음 채널
        for symbol in list(positions):
            pos = positions[symbol]
            o, h, l, c = open_.at[date, symbol], high.at[date, symbol], low.at[date, symbol], close.at[date, symbol]
            if pd.isna(c):
                continue
            if pos["side"] > 0 and l <= pos["stop"]:
                close_position(symbol, pos, min(pos["stop"], o), date, "stop")
            elif pos["side"] < 0 and h >= pos["stop"]:
                close_position(symbol, pos, max(pos["stop"], o), date, "stop")
            elif pos["side"] > 0 and pd.notna(lo_x.at[date, symbol]) and c < lo_x.at[date, symbol]:
                close_position(symbol, pos, c, date, "channel")
            elif pos["side"] < 0 and pd.notna(hi_x.at[date, symbol]) and c > hi_x.at[date, symbol]:
                close_position(symbol, pos, c, date, "channel")
            else:
                continue
            del positions[symbol]

        # 3) 시가평가 자산
        unrealized = sum(pos["side"] * pos["qty"] * (close.at[date, s] - pos["entry"])
                         for s, pos in positions.items() if pd.notna(close.at[date, s]))
        equity = cash + unrealized

        # 4) 진입 — 후보군(오늘 이전 데이터)에서 거래대금 순
        members = universe(p, i, params)
        if members and started is None:
            started = date
        for symbol in members:
            if len(positions) >= params.max_positions:
                break
            if symbol in positions:
                continue
            c, a = close.at[date, symbol], atr.at[date, symbol]
            if pd.isna(c) or pd.isna(a) or a <= 0:
                continue
            side = 1 if (pd.notna(hi_n.at[date, symbol]) and c > hi_n.at[date, symbol]) else \
                -1 if (pd.notna(lo_n.at[date, symbol]) and c < lo_n.at[date, symbol]) else 0
            if side == 0:
                continue
            risk = params.risk_per_trade * equity
            dist = params.stop_atr * a
            qty = risk / dist
            cash -= fee * qty * c
            positions[symbol] = {"side": side, "entry": c, "stop": c - side * dist, "qty": qty,
                                 "risk": risk, "date": date, "funding": 0.0}

        equity_rows.append((date, cash + sum(pos["side"] * pos["qty"] * (close.at[date, s] - pos["entry"])
                                             for s, pos in positions.items() if pd.notna(close.at[date, s]))))

    # 데이터 끝: 마지막 종가로 정리
    last = dates[-1]
    for symbol, pos in list(positions.items()):
        series = close[symbol].dropna()
        close_position(symbol, pos, float(series.iloc[-1]), last, "end_of_data")
    if equity_rows:
        equity_rows[-1] = (last, cash)

    equity = pd.Series(dict(equity_rows))
    if started is not None:
        equity = equity.loc[equity.index >= started]
    return {"equity": equity, "trades": pd.DataFrame(trades)}


def summary(result: dict) -> dict:
    equity = result["equity"]
    trades = result["trades"]
    if equity.empty:
        return {"trades": 0, "total_return": 0.0}
    rets = equity.pct_change().dropna()
    mid = equity.index[len(equity) // 2]
    first = float(equity.loc[mid] / equity.iloc[0] - 1)
    second = float(equity.iloc[-1] / equity.loc[mid] - 1)
    monthly = equity.resample("ME").last().pct_change()
    monthly.iloc[0] = equity.resample("ME").last().iloc[0] / equity.iloc[0] - 1
    r_mult = (trades["pnl"] / trades["risk"]) if len(trades) else pd.Series(dtype=float)
    return {
        "trades": len(trades),
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1),
        "first_half": first, "second_half": second,
        "sharpe": float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else None,
        "max_drawdown": float((1 - equity / equity.cummax()).max()),
        "win_rate": float((trades["pnl"] > 0).mean()) if len(trades) else None,
        "avg_r": float(r_mult.mean()) if len(r_mult) else None,
        "monthly": {str(k.to_period("M")): round(float(v), 4) for k, v in monthly.items()},
        "start": str(equity.index[0].date()),
    }
