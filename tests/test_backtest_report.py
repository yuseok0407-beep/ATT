import pytest

from src.backtest.report import summarize


def test_summarize_empty_trades():
    result = summarize([])
    assert result["num_trades"] == 0
    assert result["win_rate"] is None
    assert result["total_r"] == 0.0


def test_summarize_computes_win_rate_and_totals():
    trades = [
        {"pnl_r": 2.0, "hold_bars": 5},
        {"pnl_r": -1.0, "hold_bars": 3},
        {"pnl_r": -1.0, "hold_bars": 2},
        {"pnl_r": 0.0, "hold_bars": 1},
    ]
    result = summarize(trades)
    assert result["num_trades"] == 4
    assert result["win_rate"] == pytest.approx(0.25)
    assert result["total_r"] == pytest.approx(0.0)
    assert result["avg_r"] == pytest.approx(0.0)
    assert result["avg_hold_bars"] == pytest.approx(2.75)


def test_summarize_max_drawdown_tracks_peak_to_trough():
    # 누적 R: +3, +1(peak 3->3), -4(peak 3, 여기서 -1) -> drawdown = -4? 계산해보면 cum: 3,1,-3 peak:3,3,3 dd: 0,-2,-6
    trades = [{"pnl_r": 3.0, "hold_bars": 1}, {"pnl_r": -2.0, "hold_bars": 1}, {"pnl_r": -4.0, "hold_bars": 1}]
    result = summarize(trades)
    assert result["max_drawdown_r"] == pytest.approx(-6.0)
