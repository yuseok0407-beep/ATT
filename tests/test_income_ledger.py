"""거래소 수입 내역 원장(src/execution/income_ledger.py) — 2026-10-03."""
from unittest.mock import MagicMock

import pytest

from src.execution import income_ledger


def _row(kind, income, *, symbol="BTCUSDT", t=0, asset="USDT"):
    return {"incomeType": kind, "income": str(income), "asset": asset, "symbol": symbol, "time": t}


def test_totals_split_trading_from_transfers_and_measure_the_rebate():
    """실계좌 실측과 같은 모양 — 리베이트가 수수료의 약 20%이고 입금은 거래 손익이 아니다."""
    rows = [_row("TRANSFER", 675.31), _row("REALIZED_PNL", -42.15), _row("COMMISSION", -95.91),
            _row("COMMISSION_REBATE", 18.89), _row("FUNDING_FEE", 1.76)]

    t = income_ledger.totals(rows)

    assert t["transfers"] == pytest.approx(675.31)
    assert t["trading"] == pytest.approx(-117.41)
    assert t["rebate_ratio"] == pytest.approx(18.89 / 95.91)


def test_non_usdt_amounts_are_not_added_to_usdt():
    """BNB로 낸 수수료를 USDT 숫자에 그대로 더하면 단위가 틀린다(외부 검토 3.3)."""
    t = income_ledger.totals([_row("COMMISSION", -0.01, asset="BNB"), _row("COMMISSION", -1.0)])

    assert t["by_type"]["COMMISSION"] == pytest.approx(-1.0)
    assert t["non_usdt_rows"] == 1


def test_funding_is_attributed_by_symbol_and_holding_window():
    rows = [_row("FUNDING_FEE", -0.5, t=100), _row("FUNDING_FEE", -0.2, t=300),
            _row("FUNDING_FEE", -9.0, symbol="ETHUSDT", t=150), _row("COMMISSION", -1.0, t=150)]

    assert income_ledger.funding_between(rows, "BTCUSDT", 50, 200) == pytest.approx(-0.5)


def test_fetch_income_pages_until_a_short_batch():
    client = MagicMock()
    first = [{"time": i, "incomeType": "COMMISSION", "income": "0", "asset": "USDT"} for i in range(1000)]
    client.fapiPrivateGetIncome.side_effect = [first, [{"time": 2000, "incomeType": "X", "income": "0"}]]

    rows = income_ledger.fetch_income(client, 0, pause=0)

    assert len(rows) == 1001
    assert client.fapiPrivateGetIncome.call_args_list[1].args[0]["startTime"] == 1000
