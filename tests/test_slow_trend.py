"""대안 전략 #2(src/backtest/slow_trend.py) — 미래 정보 차단, 손절 체결, 사이징, 펀딩 부호."""
import numpy as np
import pandas as pd
import pytest

from src.backtest import slow_trend as st

P = st.Params(entry_days=5, exit_days=3, atr_days=5, min_history_days=10, universe_size=5, volume_days=5)


def _panel(closes: dict[str, list[float]], spread=0.5):
    idx = pd.date_range("2026-01-01", periods=len(next(iter(closes.values()))), freq="D")
    c = pd.DataFrame(closes, index=idx, dtype=float)
    return {"open": c.shift(1).fillna(c), "high": c + spread, "low": c - spread, "close": c,
            "qv": pd.DataFrame(1.0, index=idx, columns=c.columns)}


def _no_funding(p):
    return pd.DataFrame(0.0, index=p["close"].index, columns=p["close"].columns)


def test_universe_uses_only_days_before_the_decision():
    p = _panel({"A": list(range(100, 140)), "B": list(range(100, 140))})
    before = st.universe(p, 20, P)
    p["qv"].iloc[20:, 0] = 1e9   # 결정일 이후 거래대금 폭증
    assert st.universe(p, 20, P) == before


def test_a_breakout_enters_long_and_risks_the_configured_fraction():
    closes = [100.0] * 15 + [110.0] + [111.0] * 5
    p = _panel({"A": closes})
    result = st.simulate(p, _no_funding(p), st.Params(**{**P.__dict__, "fee_pct_per_side": 0.0}))
    trade = result["trades"].iloc[0]
    assert trade["side"] == 1 and trade["entry"] == 110.0
    assert trade["risk"] == pytest.approx(P.risk_per_trade, rel=1e-6)


def test_a_gap_through_the_stop_fills_at_the_open():
    closes = [100.0] * 15 + [110.0] + [80.0] * 3
    p = _panel({"A": closes})
    p["open"].iloc[16, 0] = 80.0   # 진입 다음 날 손절가 아래로 갭
    result = st.simulate(p, _no_funding(p), st.Params(**{**P.__dict__, "fee_pct_per_side": 0.0}))
    trade = result["trades"].iloc[0]
    assert trade["reason"] == "stop" and trade["exit"] == 80.0
    assert trade["pnl"] / trade["risk"] < -1.5   # 계획 손실(1R)보다 크게 잃는다


def test_positive_funding_costs_a_long():
    closes = [100.0] * 15 + [110.0] + [111.0] * 5
    p = _panel({"A": closes})
    funding = _no_funding(p) + 0.001
    no_fee = st.Params(**{**P.__dict__, "fee_pct_per_side": 0.0})
    with_funding = st.simulate(p, funding, no_fee)["trades"].iloc[0]["pnl"]
    without = st.simulate(p, _no_funding(p), no_fee)["trades"].iloc[0]["pnl"]
    assert with_funding < without


def test_entry_decision_ignores_later_prices():
    rng = np.random.default_rng(0)
    closes = list(100 * np.cumprod(1 + rng.normal(0, 0.03, 60)))
    p = _panel({"A": closes})
    first = st.simulate(p, _no_funding(p), P)["trades"]
    cut = first["entry_date"].iloc[0]
    tampered = {k: v.copy() for k, v in p.items()}
    for k in ("open", "high", "low", "close"):
        tampered[k].loc[tampered[k].index > cut] *= 3.0
    again = st.simulate(tampered, _no_funding(tampered), P)["trades"]
    assert again["entry_date"].iloc[0] == cut and again["entry"].iloc[0] == first["entry"].iloc[0]
