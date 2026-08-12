import pytest

from src.core.allocation import REGIME_PRESETS, compute_rebalance, target_allocation


@pytest.mark.parametrize("regime", REGIME_PRESETS.keys())
def test_all_presets_sum_to_one(regime):
    weights = target_allocation(regime)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_unknown_regime_raises():
    with pytest.raises(ValueError):
        target_allocation("NOT_A_REGIME")


def test_rebalance_buys_underweight_asset():
    # 목표: BTC 40%, 현재: BTC 0% (전액 현금) -> 큰 폭 매수 필요
    asset_values = {"BTC/USDT": 0.0, "ETH/USDT": 0.0}
    target = {"BTC/USDT": 0.40, "ETH/USDT": 0.30, "USDT": 0.30}
    # total_value must come from somewhere with cash included for weight calc
    asset_values_with_cash_denominator = {"BTC/USDT": 0.0, "ETH/USDT": 0.0, "USDT": 1000.0}
    orders = compute_rebalance(asset_values_with_cash_denominator, target)
    assert orders["BTC/USDT"] == pytest.approx(400.0)
    assert orders["ETH/USDT"] == pytest.approx(300.0)


def test_rebalance_skips_within_threshold():
    # 목표 25%, 현재 24% -> 편차 1%p, 기본 임계값(5%p) 미만이라 주문 없어야 함
    asset_values = {"BTC/USDT": 240.0, "USDT": 760.0}
    target = {"BTC/USDT": 0.25, "USDT": 0.75}
    orders = compute_rebalance(asset_values, target)
    assert "BTC/USDT" not in orders


def test_rebalance_sells_overweight_asset():
    asset_values = {"BTC/USDT": 900.0, "USDT": 100.0}
    target = {"BTC/USDT": 0.25, "USDT": 0.75}
    orders = compute_rebalance(asset_values, target)
    assert orders["BTC/USDT"] < 0


def test_rebalance_empty_portfolio_returns_no_orders():
    orders = compute_rebalance({}, {"BTC/USDT": 0.4, "USDT": 0.6})
    assert orders == {}
