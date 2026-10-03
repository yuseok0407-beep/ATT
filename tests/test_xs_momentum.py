"""대안 전략 백테스트(src/backtest/xs_momentum.py) — 미래 정보 차단과 비용 계산."""
import numpy as np
import pandas as pd
import pytest

from src.backtest import xs_momentum as xm


def _panels(n_days=200, n_syms=12, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-05", periods=n_days, freq="D")   # 월요일 시작
    closes = pd.DataFrame({f"S{i}": 100 * np.cumprod(1 + rng.normal(0.001 * (i - 6), 0.02, n_days))
                           for i in range(n_syms)}, index=idx)
    volumes = pd.DataFrame({c: 1e6 * (1 + i) for i, c in enumerate(closes)}, index=idx)
    return closes, volumes


P = xm.Params(lookback_days=14, universe_size=10, volume_days=10, min_history_days=20, k=2)


def test_selection_ignores_prices_on_and_after_the_rebalance_date():
    """d 이후의 가격을 마구 바꿔도 d의 선택은 같아야 한다 — 미래 정보를 쓰지 않는다."""
    closes, volumes = _panels()
    date = closes.index[100]
    before = xm.select(closes, volumes, date, P)

    tampered = closes.copy()
    tampered.loc[tampered.index >= date] *= np.random.default_rng(1).uniform(0.1, 10, tampered.loc[tampered.index >= date].shape)
    assert xm.select(tampered, volumes, date, P) == before


def test_longs_are_the_recent_winners_and_shorts_the_losers():
    closes, volumes = _panels()
    longs, shorts = xm.select(closes, volumes, closes.index[150], P)
    past = closes.loc[closes.index < closes.index[150]]
    ret = past.iloc[-1] / past.iloc[-1 - P.lookback_days] - 1
    universe = volumes.columns[::-1][:P.universe_size]
    assert set(longs) == set(ret[universe].nlargest(2).index)
    assert set(shorts) == set(ret[universe].nsmallest(2).index)


def test_symbols_without_enough_history_are_not_eligible():
    closes, volumes = _panels()
    closes.loc[closes.index < closes.index[95], "S11"] = np.nan   # 가장 거래대금 큰 종목이 늦게 상장
    longs, shorts = xm.select(closes, volumes, closes.index[100], P)
    assert "S11" not in longs + shorts


def test_holding_the_same_book_costs_no_fee_and_funding_has_the_right_sign():
    """같은 종목을 계속 들면 수수료는 첫 주와 흘러간 비중 조정분뿐이고, 양의 펀딩은 롱이 내고 숏이 받는다."""
    idx = pd.date_range("2026-01-05", periods=60, freq="D")
    closes = pd.DataFrame({"UP": np.linspace(100, 200, 60), "DN": np.linspace(200, 100, 60)}, index=idx)
    volumes = pd.DataFrame({"UP": 1.0, "DN": 1.0}, index=idx)
    funding = pd.DataFrame({"UP": 0.001, "DN": 0.0}, index=idx)   # 롱(UP)이 하루 0.1%씩 낸다
    params = xm.Params(lookback_days=7, universe_size=2, volume_days=7, min_history_days=10, k=1, gross=1.0)

    weekly = xm.backtest(closes, volumes, funding, params)
    active = weekly[weekly["longs"] != ""]

    assert (active["longs"] == "UP").all() and (active["shorts"] == "DN").all()
    assert active["fee"].iloc[0] == pytest.approx(params.fee_pct_per_side * 1.0)   # 0 -> 0.5 + 0.5
    assert active["fee"].iloc[1] < active["fee"].iloc[0] / 5
    assert active["funding"].iloc[1] == pytest.approx(-0.5 * 0.001 * 7)
