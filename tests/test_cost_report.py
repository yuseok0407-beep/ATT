from src.execution.cost_report import MIN_SAMPLES_FOR_DECISION, distribution, summarize_costs


def _row(env="live", reason="take_profit", side="long", symbol="BTC/USDT:USDT",
         entry=0.0, exit_=0.0, fee=0.05):
    cost = None if entry is None or exit_ is None else entry + exit_ + fee
    return {"env": env, "reason": reason, "side": side, "symbol": symbol,
            "entry_slippage_r": entry, "exit_slippage_r": exit_, "fee_r": fee,
            "execution_cost_r": cost}


def test_distribution_reports_tail_not_just_mean():
    """평균만 보면 가끔 크게 터지는 꼬리가 가려진다 — 최악값과 상위 퍼센타일을 같이 낸다."""
    dist = distribution([0.0] * 9 + [1.0])

    assert dist["n"] == 10
    assert dist["median"] == 0.0
    assert dist["mean"] == 0.1
    assert dist["worst"] == 1.0
    assert dist["p95"] == 1.0


def test_distribution_ignores_missing_values_and_returns_none_when_empty():
    assert distribution([None, None]) is None
    assert distribution([None, 0.2])["n"] == 1


def test_summarize_costs_compares_measured_slippage_with_the_stress_assumption():
    rows = [_row(entry=0.0, exit_=0.02), _row(entry=0.02, exit_=0.3)]

    summary = summarize_costs(rows, stress_slippage_r_per_side=0.05)
    stress = summary["stress_assumption"]

    assert stress["per_trade_r"] == 0.1
    # 거래당 실측 슬리피지 0.02, 0.32 -> 평균 0.17 > 0.10
    assert abs(stress["measured_per_trade"]["mean"] - 0.17) < 1e-9
    assert stress["mean_within_assumption"] is False


def test_summarize_costs_counts_only_fully_measured_trades_toward_the_sample_goal():
    rows = [_row(), _row(entry=None)]

    summary = summarize_costs(rows, 0.05)

    assert summary["trades"] == 2
    assert summary["complete_samples"] == 1
    assert summary["enough_samples"] is (1 >= MIN_SAMPLES_FOR_DECISION)


def test_summarize_costs_groups_by_exit_reason():
    rows = [_row(reason="stop_loss", exit_=0.0), _row(reason="take_profit", exit_=0.2)]

    by_reason = summarize_costs(rows, 0.05)["by_reason"]

    assert by_reason["stop_loss"]["exit_slippage_r"]["mean"] == 0.0
    assert by_reason["take_profit"]["exit_slippage_r"]["mean"] == 0.2
