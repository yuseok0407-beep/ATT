import pytest

from src.backtest.sizing import equity_path, max_drawdown_pct, months_to_recover, sizing_stats


def _trade(pnl_r, step):
    return {"pnl_r": pnl_r, "exit_step": step}


def test_equity_compounds_on_the_current_balance():
    """봇은 매 진입마다 그때 자산의 x%를 건다 — 두 번 +1R이면 1.01 x 1.01이지 1.02가 아니다."""
    path = equity_path([_trade(1.0, 0), _trade(1.0, 1)], risk_per_trade=0.01)
    assert path == pytest.approx([1.0, 1.01, 1.0201])


def test_equity_follows_exit_order_not_list_order():
    """자산이 실제로 바뀌는 순간은 청산이다 — 목록 순서가 아니라 청산 순서로 쌓아야 낙폭이 맞다."""
    trades = [_trade(2.0, 5), _trade(-1.0, 1), _trade(-1.0, 2)]
    path = equity_path(trades, risk_per_trade=0.1)
    assert path == pytest.approx([1.0, 0.9, 0.81, 0.972])


def test_max_drawdown_is_measured_from_the_running_peak():
    assert max_drawdown_pct([1.0, 1.2, 0.9, 1.3, 1.17]) == pytest.approx(0.25)


def test_months_to_recover_at_one_percent_a_month():
    # -10%를 되찾으려면 +11.1% — 월 1% 복리로 약 10.6개월(OBJECTIVE.md 표와 같은 계산).
    assert months_to_recover(0.10) == pytest.approx(10.59, abs=0.01)
    assert months_to_recover(0.0) == 0.0


def test_risk_scales_results_but_cannot_change_their_sign():
    """리스크 비율은 볼륨 손잡이다 — 지는 전략은 어느 비율에서도 진다."""
    losing = [_trade(-1.0, i) if i % 2 else _trade(0.9, i) for i in range(40)]
    small = sizing_stats(losing, 0.0025, months=1)
    large = sizing_stats(losing, 0.0075, months=1)

    assert small["total_return"] < 0 and large["total_return"] < 0
    assert large["max_drawdown"] > small["max_drawdown"]


def test_monthly_return_is_the_compound_rate():
    trades = [_trade(1.0, i) for i in range(12)]
    stats = sizing_stats(trades, 0.01, months=12)
    assert stats["monthly_return"] == pytest.approx(0.01)


def test_concurrent_positions_are_sized_from_equity_at_entry():
    """외부 검토 3.7(2026-10-03): 위험 10%로 **동시에** 연 두 포지션이 둘 다 -1R이면 자산은
    1 - 0.1 - 0.1 = 0.80이다. 청산 순서로 복리를 곱하면 0.9 x 0.9 = 0.81이 나온다(틀림)."""
    trades = [{"pnl_r": -1.0, "entry_step": 0, "exit_step": 1},
              {"pnl_r": -1.0, "entry_step": 0, "exit_step": 2}]

    assert equity_path(trades, 0.10)[-1] == pytest.approx(0.80)


def test_sequential_positions_still_compound():
    """앞 거래가 닫힌 뒤 진입한 거래는 줄어든 자산으로 건다 — 복리는 그대로다."""
    trades = [{"pnl_r": -1.0, "entry_step": 0, "exit_step": 1},
              {"pnl_r": -1.0, "entry_step": 2, "exit_step": 3}]

    assert equity_path(trades, 0.10)[-1] == pytest.approx(0.81)
