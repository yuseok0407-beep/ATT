"""aoa 체결 내역의 왕복 거래 재구성 — 0을 가로지르는 체결 쪼개기가 핵심이다(2026-09-26).

안 쪼개면 2018년 손익이 -1,743 XBT로 나와 지갑의 +188 XBT와 부호가 정반대가 됐다.
"""

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "analyze_aoa_trades", Path(__file__).resolve().parents[1] / "scripts" / "analyze_aoa_trades.py")
aoa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aoa)


def _fills(rows):
    return pd.DataFrame([{"side": side, "lastqty": qty, "lastpx": px,
                          "t": pd.Timestamp("2020-01-01") + pd.Timedelta(minutes=i),
                          "ordtype": "Limit", "text": "", "lastliquidityind": "AddedLiquidity"}
                         for i, (side, qty, px) in enumerate(rows)])


def test_a_long_with_one_add_is_one_trip_with_inverse_pnl():
    trips = aoa.round_trips(_fills([("Buy", 100, 10000), ("Buy", 100, 9000), ("Sell", 200, 9500)]))
    assert len(trips) == 1
    trip = trips.iloc[0]
    assert trip["dir"] == 1 and trip["adds"] == 2
    assert trip["pnl"] == pytest.approx(100 / 10000 + 100 / 9000 - 200 / 9500)
    assert trip["averaged_down"]  # 롱인데 더 싼 값에 추가 매수 = 물타기


def test_a_fill_that_crosses_zero_is_split_into_two_trips():
    trips = aoa.round_trips(_fills([("Buy", 100, 10000), ("Sell", 300, 11000), ("Buy", 200, 10500)]))
    assert list(trips["dir"]) == [1, -1]
    # 첫 거래는 100만 닫는다 — 나머지 200은 새 숏의 진입이다
    assert trips.iloc[0]["pnl"] == pytest.approx(100 / 10000 - 100 / 11000)
    assert trips.iloc[1]["pnl"] == pytest.approx(-200 / 11000 + 200 / 10500)
    assert trips.iloc[1]["entry"] == pytest.approx(11000)


def test_an_open_position_at_the_end_is_not_a_trip():
    assert aoa.round_trips(_fills([("Buy", 100, 10000)])).empty
