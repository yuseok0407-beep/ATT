"""대안 전략: 저회전 횡단면 모멘텀(사전 등록 `docs/ALT_XS_MOMENTUM.md`, 2026-10-03).

매주 월요일 00:00 UTC에 **그 전까지의 데이터만으로** 후보(거래대금 상위)를 정하고, 직전 N일 수익률
상위 k개 롱 / 하위 k개 숏을 같은 금액으로 들고 일주일 보유한다. 지금 전략(1시간봉 돌파)과 규칙을
하나도 공유하지 않으므로 그쪽 엔진을 쓰지 않는다.

미래 정보를 쓰지 않는 것이 이 모듈의 핵심 규칙이다: 날짜 d의 결정은 d보다 **이전** 날짜의 일봉만
본다(`_history_before`). `tests/test_xs_momentum.py`가 "d 이후 가격을 바꿔도 d의 선택이 안 바뀐다"를
확인한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Params:
    lookback_days: int = 28        # 순위 기간
    universe_size: int = 30        # 거래대금 상위 몇 개를 후보로
    volume_days: int = 30          # 거래대금을 재는 기간
    min_history_days: int = 90     # 이만큼 일봉이 있어야 후보
    k: int = 5                     # 롱/숏 각각 몇 종목
    gross: float = 1.0             # 총 포지션 / 자산
    fee_pct_per_side: float = 0.0005


def daily_panels(hourly: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """1시간봉 -> UTC 일봉 (종가, 거래대금) 패널. 하루 24봉이 다 있어야 그날을 쓴다(진행 중인 날 제외)."""
    closes, volumes = {}, {}
    for symbol, df in hourly.items():
        if df.empty:
            continue
        frame = df.set_index("timestamp")
        day = frame.index.floor("D")
        counts = frame["close"].groupby(day).size()
        full = counts[counts == 24].index
        closes[symbol] = frame["close"].groupby(day).last().reindex(full)
        volumes[symbol] = (frame["close"] * frame["volume"]).groupby(day).sum().reindex(full)
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(volumes).sort_index()


def funding_panel(funding: dict[str, list[dict]], index: pd.DatetimeIndex) -> pd.DataFrame:
    """종목별 펀딩 이력({"t": ms, "rate": 비율}) -> 날짜별 펀딩 합계(그날 00:00 초과 ~ 다음 날 00:00 이하)."""
    out = {}
    for symbol, rows in funding.items():
        if not rows:
            continue
        s = pd.Series([r["rate"] for r in rows],
                      index=pd.to_datetime([r["t"] for r in rows], unit="ms"))
        # 정각(00:00) 펀딩은 그 전날 보유분에 붙인다 — 00:00에 교체하는 포지션이 그 펀딩을 낸다.
        day = (s.index - pd.Timedelta(milliseconds=1)).floor("D")
        out[symbol] = s.groupby(day).sum()
    return pd.DataFrame(out).reindex(index).fillna(0.0)


def _history_before(panel: pd.DataFrame, date: pd.Timestamp) -> pd.DataFrame:
    return panel.loc[panel.index < date]


def select(closes: pd.DataFrame, volumes: pd.DataFrame, date: pd.Timestamp,
           params: Params) -> tuple[list[str], list[str]]:
    """date(월요일 00:00) 시점의 롱·숏 종목. date 이전 일봉만 쓴다."""
    past_c, past_v = _history_before(closes, date), _history_before(volumes, date)
    if len(past_c) <= params.lookback_days:
        return [], []
    last = past_c.index[-1]
    eligible = [s for s in past_c.columns
                if past_c[s].notna().sum() >= params.min_history_days and pd.notna(past_c.at[last, s])]
    qv = past_v[eligible].iloc[-params.volume_days:].sum()
    universe = qv.sort_values(ascending=False).index[:params.universe_size]
    ref = past_c.iloc[-1 - params.lookback_days]
    ret = (past_c.iloc[-1][universe] / ref[universe] - 1.0).dropna()
    if len(ret) < 2 * params.k:
        return [], []
    ranked = ret.sort_values(ascending=False)
    return list(ranked.index[:params.k]), list(ranked.index[-params.k:])


def backtest(closes: pd.DataFrame, volumes: pd.DataFrame, funding: pd.DataFrame,
             params: Params = Params()) -> pd.DataFrame:
    """주간 결과 표. 행 = 교체일, 열 = gross(가격 손익), fee, funding, net, longs, shorts.

    체결: 교체일 직전 일봉 종가(= 교체일 00:00 가격)에 사고팔고, 7일 뒤 같은 기준으로 평가한다.
    수수료 = 편도율 × |새 비중 - 일주일 동안 흘러간 옛 비중| 합계. 펀딩 = -비중 × 그 주 펀딩 합계
    (롱은 양의 펀딩을 내고 숏은 받는다). 보유 중 비중 변화는 펀딩 계산에서 무시한다."""
    mondays = [d for d in closes.index if d.dayofweek == 0]
    rows = []
    drifted = pd.Series(dtype=float)
    for date, next_date in zip(mondays, mondays[1:]):
        if not (closes.index < date).any():
            continue   # 이전 일봉이 하나도 없다 — 들어갈 가격이 없다
        longs, shorts = select(closes, volumes, date, params)
        weights = pd.Series(dtype=float)
        if longs:
            each = params.gross / (2 * params.k)
            weights = pd.concat([pd.Series(each, index=longs), pd.Series(-each, index=shorts)])
        start_px = closes.loc[closes.index < date].iloc[-1]
        end_px = closes.loc[closes.index < next_date].iloc[-1]
        ret = (end_px / start_px - 1.0).reindex(weights.index).fillna(0.0)
        gross = float((weights * ret).sum())
        names = weights.index.union(drifted.index)
        turnover = float((weights.reindex(names, fill_value=0.0) - drifted.reindex(names, fill_value=0.0)).abs().sum())
        fee = params.fee_pct_per_side * turnover
        week = funding.loc[(funding.index >= date - pd.Timedelta(days=1)) & (funding.index < next_date - pd.Timedelta(days=1))]
        fund = float(-(weights * week.reindex(columns=weights.index, fill_value=0.0).sum()).sum()) if len(weights) else 0.0
        net = gross - fee + fund
        rows.append({"date": date, "gross": gross, "fee": fee, "funding": fund, "net": net,
                     "longs": ",".join(longs), "shorts": ",".join(shorts)})
        # 일주일 동안 가격이 움직인 뒤의 비중(자산 대비) — 다음 교체의 회전율 계산용
        drifted = weights * (1.0 + ret) / (1.0 + net) if len(weights) else pd.Series(dtype=float)
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def summary(weekly: pd.DataFrame) -> dict:
    """사전 등록 통과 기준에 필요한 값."""
    active = weekly[weekly["longs"] != ""]
    net = active["net"]
    equity = (1.0 + net).cumprod()
    drawdown = float((1.0 - equity / equity.cummax()).max()) if len(equity) else 0.0
    half = len(net) // 2
    return {
        "weeks": len(net),
        "total_return": float(equity.iloc[-1] - 1.0) if len(equity) else 0.0,
        "first_half": float((1.0 + net.iloc[:half]).prod() - 1.0),
        "second_half": float((1.0 + net.iloc[half:]).prod() - 1.0),
        "sharpe": float(net.mean() / net.std() * np.sqrt(52)) if len(net) > 1 and net.std() > 0 else None,
        "max_drawdown": drawdown,
        "fees": float(active["fee"].sum()),
        "funding": float(active["funding"].sum()),
        "worst_week": float(net.min()) if len(net) else None,
        "start": str(active.index[0].date()) if len(active) else None,
    }
