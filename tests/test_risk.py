import pytest

from src.core.risk import (
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS_PCT,
    MAX_SINGLE_ORDER_PCT,
    check_circuit_breaker,
    position_size,
    validate_order,
)


def test_position_size_basic():
    # 포트폴리오 10000, 거래당 위험 2% = 200. 진입 100, 손절 90 -> 단위당 위험 10 -> 수량 20
    qty = position_size(10_000, entry_price=100, stop_loss_price=90, risk_per_trade=0.02)
    assert qty == pytest.approx(20.0)


def test_position_size_zero_when_stop_equals_entry():
    qty = position_size(10_000, entry_price=100, stop_loss_price=100)
    assert qty == 0.0


def test_position_size_zero_when_no_portfolio_value():
    qty = position_size(0, entry_price=100, stop_loss_price=90)
    assert qty == 0.0


def test_circuit_breaker_allows_normal_conditions():
    decision = check_circuit_breaker(daily_pnl_pct=0.01, consecutive_losses=0)
    assert decision.allowed is True


def test_circuit_breaker_blocks_daily_loss_limit():
    decision = check_circuit_breaker(daily_pnl_pct=-MAX_DAILY_LOSS_PCT, consecutive_losses=0)
    assert decision.allowed is False
    assert "손실" in decision.reason


def test_circuit_breaker_blocks_consecutive_losses():
    decision = check_circuit_breaker(daily_pnl_pct=0.0, consecutive_losses=MAX_CONSECUTIVE_LOSSES)
    assert decision.allowed is False


def test_circuit_breaker_just_under_thresholds_allows():
    decision = check_circuit_breaker(
        daily_pnl_pct=-(MAX_DAILY_LOSS_PCT - 0.001),
        consecutive_losses=MAX_CONSECUTIVE_LOSSES - 1,
    )
    assert decision.allowed is True


def test_validate_order_within_limit():
    decision = validate_order(order_value=1000, portfolio_value=10_000)
    assert decision.allowed is True


def test_validate_order_exceeds_limit():
    decision = validate_order(order_value=6000, portfolio_value=10_000)
    assert decision.allowed is False


def test_validate_order_negative_value_uses_absolute():
    # 매도 주문(음수)도 동일한 규모 한도를 적용해야 함
    decision = validate_order(order_value=-6000, portfolio_value=10_000)
    assert decision.allowed is False


def test_validate_order_zero_portfolio_blocked():
    decision = validate_order(order_value=0, portfolio_value=0)
    assert decision.allowed is False
