import json

from src.core.state import compute_consecutive_losses, get_daily_pnl_pct


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


def _closed(realized_pnl):
    return {"event": "closed", "realized_pnl": realized_pnl}


class TestComputeConsecutiveLosses:
    def test_no_entries_returns_zero(self):
        assert compute_consecutive_losses([]) == 0

    def test_ignores_non_closed_events(self):
        entries = [{"event": "entered"}, _closed(-10.0), {"event": "holding_position"}]
        assert compute_consecutive_losses(entries) == 1

    def test_counts_trailing_losses_only(self):
        entries = [_closed(-5.0), _closed(20.0), _closed(-1.0), _closed(-2.0)]
        assert compute_consecutive_losses(entries) == 2

    def test_win_immediately_after_a_loss_resets_to_zero(self):
        entries = [_closed(-5.0), _closed(3.0)]
        assert compute_consecutive_losses(entries) == 0

    def test_zero_pnl_close_counts_as_a_stop_not_a_loss(self):
        entries = [_closed(-5.0), _closed(0.0)]
        assert compute_consecutive_losses(entries) == 0

    def test_all_losses_counts_every_one(self):
        entries = [_closed(-1.0), _closed(-2.0), _closed(-3.0)]
        assert compute_consecutive_losses(entries) == 3
