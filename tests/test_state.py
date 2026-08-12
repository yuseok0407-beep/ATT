import json

from src.core.state import get_daily_pnl_pct


def test_first_call_today_sets_baseline_and_returns_zero(tmp_path):
    path = tmp_path / "daily_equity.json"
    pnl = get_daily_pnl_pct(10_000, path=str(path))
    assert pnl == 0.0
    saved = json.loads(path.read_text())
    assert saved["start_equity"] == 10_000


def test_same_day_computes_pnl_against_baseline(tmp_path):
    path = tmp_path / "daily_equity.json"
    get_daily_pnl_pct(10_000, path=str(path))
    pnl = get_daily_pnl_pct(9_500, path=str(path))
    assert pnl == -0.05


def test_new_day_resets_baseline(tmp_path):
    path = tmp_path / "daily_equity.json"
    path.write_text(json.dumps({"date": "2000-01-01", "start_equity": 5_000}))
    pnl = get_daily_pnl_pct(9_000, path=str(path))
    assert pnl == 0.0
    saved = json.loads(path.read_text())
    assert saved["start_equity"] == 9_000


def test_zero_baseline_does_not_divide_by_zero(tmp_path):
    path = tmp_path / "daily_equity.json"
    path.write_text(json.dumps({"date": "2099-01-01", "start_equity": 0}))
    import unittest.mock as mock

    with mock.patch("src.core.state.date") as mock_date:
        mock_date.today.return_value.isoformat.return_value = "2099-01-01"
        pnl = get_daily_pnl_pct(1000, path=str(path))
    assert pnl == 0.0
