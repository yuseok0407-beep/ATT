import pytest

from src.backtest.report import aggregate_stats, portfolio_stats, summarize


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


# -------------------------------------------------- 포트폴리오 낙폭 (2026-09-22)
# aggregate_stats의 max_drawdown_r은 "종목별 낙폭 중 가장 나쁜 하나"다 — 손실이 여러 종목에
# 동시에 오면 실제로 겪는 낙폭을 크게 과소평가한다. portfolio_stats는 청산 시각 순으로 하나의
# 곡선에 합쳐서 그 값을 제대로 낸다.


def _trade(pnl_r, exit_index):
    return {"pnl_r": pnl_r, "exit_index": exit_index, "hold_bars": 1}


def test_portfolio_drawdown_exceeds_worst_single_symbol_when_losses_overlap():
    """세 종목이 같은 구간에 각각 -2R씩 빠지면 실제 낙폭은 -6R인데, 종목별 최악값은 -2R이다."""
    trades_by_symbol = {
        "A": [_trade(-2.0, 10)],
        "B": [_trade(-2.0, 11)],
        "C": [_trade(-2.0, 12)],
    }

    portfolio = portfolio_stats(trades_by_symbol)
    per_symbol = aggregate_stats([summarize(t) for t in trades_by_symbol.values()])

    assert portfolio["max_drawdown_r"] == pytest.approx(-6.0)
    assert per_symbol["max_drawdown_r"] == pytest.approx(-2.0)  # 과소평가되는 쪽
    assert portfolio["total_r"] == per_symbol["total_r"]  # 총R은 같아야 한다


def test_portfolio_orders_by_exit_index_not_by_symbol():
    """순서 기준은 청산 봉이다 — R은 청산 시점에 확정되므로 곡선에 얹히는 시각도 그때다.
    종목별로 이어붙이면 같은 거래 집합인데 낙폭이 달라진다."""
    # 시간순: +3(A) -> -4(B) -> +3(A). 곡선 3 -> -1 -> 2 이므로 낙폭 -4.
    trades_by_symbol = {"A": [_trade(3.0, 1), _trade(3.0, 3)], "B": [_trade(-4.0, 2)]}

    assert portfolio_stats(trades_by_symbol)["max_drawdown_r"] == pytest.approx(-4.0)


def test_portfolio_recovery_trades_counts_until_the_peak_is_regained():
    trades_by_symbol = {"A": [_trade(2.0, 1), _trade(-1.0, 2), _trade(-1.0, 3), _trade(2.0, 4)]}

    stats = portfolio_stats(trades_by_symbol)

    assert stats["max_drawdown_r"] == pytest.approx(-2.0)
    assert stats["recovery_trades"] == 3  # 고점(0번) -> 회복(3번)


def test_portfolio_recovery_is_none_while_still_underwater():
    """아직 고점을 회복하지 못했으면 회복 기간을 단정하지 않는다."""
    trades_by_symbol = {"A": [_trade(2.0, 1), _trade(-3.0, 2)]}

    assert portfolio_stats(trades_by_symbol)["recovery_trades"] is None


def test_portfolio_stats_on_no_trades():
    stats = portfolio_stats({"A": [], "B": []})

    assert stats["num_trades"] == 0
    assert stats["total_r"] == 0.0
    assert stats["max_drawdown_r"] == 0.0
    assert stats["equity_curve"] == []


def test_aggregate_stats_exposes_the_worst_symbol_drawdown_under_a_clear_name():
    """옛 이름(max_drawdown_r)이 포트폴리오 낙폭으로 오해돼 왔으므로, 같은 값을 뜻이 분명한
    이름으로도 같이 돌려준다."""
    stats = aggregate_stats([summarize([_trade(-2.0, 1)]), summarize([_trade(-5.0, 2)])])

    assert stats["max_drawdown_r_worst_symbol"] == pytest.approx(-5.0)
    assert stats["max_drawdown_r"] == stats["max_drawdown_r_worst_symbol"]


def test_portfolio_prefers_exit_step_over_bar_index_for_ordering():
    """봉 인덱스는 종목마다 상장일이 달라 비교할 수 없다 — 통합 타임라인 위치(exit_step)가
    있으면 그걸 써야 자산곡선의 순서가 맞다.

    아래 두 거래는 봉 인덱스로는 A(10) -> B(5)의 역순이지만, 실제 시각으로는 B가 먼저다.
    시각 순이면 곡선이 -3 -> 1이라 낙폭 -3이고, 봉 인덱스 순이면 4 -> 1이라 낙폭 -3이 아니다."""
    trades_by_symbol = {
        "A": [{"pnl_r": 4.0, "exit_index": 10, "exit_step": 200, "hold_bars": 1}],
        "B": [{"pnl_r": -3.0, "exit_index": 5, "exit_step": 100, "hold_bars": 1}],
    }

    assert portfolio_stats(trades_by_symbol)["max_drawdown_r"] == pytest.approx(-3.0)


def test_portfolio_falls_back_to_exit_index_without_exit_step():
    """단일 종목 run_backtest 결과만 넘기는 경우에는 봉 인덱스로 충분하다."""
    trades_by_symbol = {"A": [_trade(-2.0, 1), _trade(3.0, 2)]}

    stats = portfolio_stats(trades_by_symbol)

    assert stats["max_drawdown_r"] == pytest.approx(-2.0)
    assert stats["total_r"] == pytest.approx(1.0)
