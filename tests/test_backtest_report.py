import pytest

from src.backtest.report import aggregate_stats, summarize


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


def test_aggregate_stats_empty_list():
    result = aggregate_stats([])
    assert result["num_trades"] == 0
    assert result["win_rate"] is None
    assert result["total_r"] == 0.0


def test_aggregate_stats_ignores_zero_trade_entries():
    # 거래가 없던 종목(summarize([]) 결과, win_rate=None)이 섞여 있으면 그대로 가중합에 넣었을 때
    # TypeError가 나므로, aggregate_stats가 이런 항목을 미리 걸러내는지 확인한다.
    no_trades = summarize([])
    some_trades = summarize([{"pnl_r": 2.0, "hold_bars": 4}, {"pnl_r": -1.0, "hold_bars": 2}])
    result = aggregate_stats([no_trades, some_trades])
    assert result["num_trades"] == 2
    assert result["total_r"] == pytest.approx(1.0)


def test_aggregate_stats_weights_by_trade_count():
    # 종목A: 4거래, 총R 8 (평균R 2) / 종목B: 1거래, 총R -3 (평균R -3)
    stats_a = {"num_trades": 4, "win_rate": 1.0, "avg_r": 2.0, "total_r": 8.0,
               "max_drawdown_r": -1.0, "avg_hold_bars": 3.0}
    stats_b = {"num_trades": 1, "win_rate": 0.0, "avg_r": -3.0, "total_r": -3.0,
               "max_drawdown_r": -5.0, "avg_hold_bars": 10.0}
    result = aggregate_stats([stats_a, stats_b])

    assert result["num_trades"] == 5
    assert result["total_r"] == pytest.approx(5.0)
    assert result["avg_r"] == pytest.approx(1.0)  # 5거래 합쳐서 다시 계산한 평균과 동치
    assert result["win_rate"] == pytest.approx(0.8)  # (4*1.0 + 1*0.0) / 5
    # 최대낙폭은 종목별 최솟값(가장 나쁜 값)을 그대로 쓴다 — 합산하지 않음
    assert result["max_drawdown_r"] == pytest.approx(-5.0)
