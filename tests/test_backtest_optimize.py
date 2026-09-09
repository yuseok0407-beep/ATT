import numpy as np
import pandas as pd
import pytest

from src.backtest.optimize import BASELINE_LABEL, default_score, run_grid_search, run_walk_forward


def _profitable_uptrend_segment():
    """tests/test_backtest_engine.py의 _cross_up_df + take_profit 패턴과 동일한 61봉짜리
    세그먼트 — SMA20 상향돌파 신호 후 바로 다음 봉에서 target_price까지 도달해 항상 +2R로 익절한다."""
    n = 60
    closes = 100 + np.arange(n) * 0.5
    closes[-2] -= 5
    closes[-1] = closes[-3] + 5
    df = pd.DataFrame({"close": closes, "high": closes + 0.5, "low": closes - 0.5})
    entry_price = float(df["close"].iloc[-1])
    extra = pd.DataFrame([{
        "high": entry_price * 1.03, "low": entry_price * 0.995, "close": entry_price * 1.02,
    }])
    return pd.concat([df, extra], ignore_index=True)


def _flat_segment(n=61):
    """추세가 전혀 없는 61봉 — ADX가 낮게 유지되어 진입 신호 자체가 안 뜬다."""
    closes = np.full(n, 100.0)
    return pd.DataFrame({"close": closes, "high": closes + 0.1, "low": closes - 0.1})


FIXED = dict(stop_mode="fixed", stop_loss_pct=0.01, take_profit_rr=2.0)


def test_run_grid_search_always_includes_baseline_even_when_not_in_grid():
    df = _profitable_uptrend_segment()
    results = run_grid_search({"BTC": df}, {"adx_threshold": [1, 2]}, fixed_params=FIXED)

    assert len(results) == 3
    labels = [r["label"] for r in results]
    assert sum(l.startswith(BASELINE_LABEL) for l in labels) == 1


def test_run_grid_search_baseline_not_duplicated_when_already_in_grid():
    from src.core.config import RULE_ADX_THRESHOLD

    df = _profitable_uptrend_segment()
    results = run_grid_search({"BTC": df}, {"adx_threshold": [1, RULE_ADX_THRESHOLD]}, fixed_params=FIXED)

    assert len(results) == 2
    labels = [r["label"] for r in results]
    assert sum(l.startswith(BASELINE_LABEL) for l in labels) == 1


def test_run_grid_search_unregistered_param_raises():
    df = _flat_segment()
    with pytest.raises(ValueError):
        run_grid_search({"BTC": df}, {"unknown_param": [1, 2]})


def test_run_grid_search_fixed_params_bypass_baseline_registry():
    # fixed_params의 키는 param_grid에 없으므로 BASELINE_PARAMS에 등록 안 돼 있어도 에러가 나면 안 된다.
    df = _profitable_uptrend_segment()
    results = run_grid_search(
        {"BTC": df}, {"adx_threshold": [1]},
        fixed_params=dict(FIXED, use_breakeven=False),
    )
    assert len(results) == 2  # adx_threshold=1 + 베이스라인


def test_default_score_disqualifies_below_min_trades():
    assert default_score({"num_trades": 5, "total_r": 10.0, "max_drawdown_r": -1.0}, min_trades=20) is None


def test_default_score_applies_drawdown_penalty():
    stats = {"num_trades": 25, "total_r": 10.0, "max_drawdown_r": -2.0}
    assert default_score(stats, min_trades=20) == pytest.approx(10.0)
    assert default_score(stats, min_trades=20, drawdown_penalty=1.0) == pytest.approx(8.0)


def test_run_walk_forward_fails_when_only_first_split_is_profitable():
    df = pd.concat([_profitable_uptrend_segment(), _flat_segment()], ignore_index=True)
    results = run_walk_forward({"BTC": df}, {"adx_threshold": [1]}, n_splits=2, fixed_params=FIXED)

    result = next(r for r in results if r["params"]["adx_threshold"] == 1)
    assert result["passed_oos"] is False
    assert result["splits"][0]["total_r"] > 0
    assert result["splits"][1]["total_r"] == 0.0


def test_run_walk_forward_passes_when_both_splits_are_profitable():
    df = pd.concat(
        [_profitable_uptrend_segment(), _profitable_uptrend_segment()], ignore_index=True,
    )
    results = run_walk_forward({"BTC": df}, {"adx_threshold": [1]}, n_splits=2, fixed_params=FIXED)

    result = next(r for r in results if r["params"]["adx_threshold"] == 1)
    assert result["passed_oos"] is True
    assert result["splits"][0]["total_r"] > 0
    assert result["splits"][1]["total_r"] > 0
    assert result["stats"]["total_r"] == pytest.approx(
        result["splits"][0]["total_r"] + result["splits"][1]["total_r"]
    )


def test_run_walk_forward_rejects_n_splits_below_two():
    with pytest.raises(ValueError):
        run_walk_forward({"BTC": _flat_segment()}, {"adx_threshold": [1]}, n_splits=1)
