import pytest

from src.core.futures_risk import (
    check_stop_before_liquidation,
    estimate_liquidation_price,
    leveraged_position_size,
    required_margin,
)


def test_leveraged_position_size_matches_spot_formula_when_under_leverage_cap():
    # margin 10000, risk 2% = 200. entry 65000, stop 64350 (1% away) -> per-unit-risk 650 -> qty ~0.3077
    qty = leveraged_position_size(10_000, entry_price=65_000, stop_loss_price=64_350, leverage=10, risk_per_trade=0.02)
    assert qty == pytest.approx(200 / 650, rel=1e-6)


def test_leveraged_position_size_capped_by_leverage():
    # 아주 촘촘한 손절(1달러 폭)이면 리스크 기준 수량이 레버리지 한도를 초과할 수 있음 -> 캡 적용
    qty = leveraged_position_size(1_000, entry_price=65_000, stop_loss_price=64_999, leverage=10, risk_per_trade=0.02)
    max_by_leverage = (1_000 * 10) / 65_000
    assert qty == pytest.approx(max_by_leverage)


def test_leveraged_position_size_zero_for_zero_margin():
    qty = leveraged_position_size(0, entry_price=65_000, stop_loss_price=64_000, leverage=10, risk_per_trade=0.02)
    assert qty == 0.0


def test_leveraged_position_size_zero_when_stop_equals_entry():
    qty = leveraged_position_size(10_000, entry_price=65_000, stop_loss_price=65_000, leverage=10, risk_per_trade=0.02)
    assert qty == 0.0


def test_required_margin_basic():
    margin = required_margin(quantity=0.1, entry_price=65_000, leverage=10)
    assert margin == pytest.approx(650.0)


def test_required_margin_rejects_zero_leverage():
    with pytest.raises(ValueError):
        required_margin(quantity=0.1, entry_price=65_000, leverage=0)


def test_liquidation_price_long_below_entry():
    liq = estimate_liquidation_price(65_000, leverage=10, side="long")
    assert liq < 65_000
    # 대략 진입가의 90.5% 근방 (1 - 1/10 + 0.005)
    assert liq == pytest.approx(65_000 * 0.905, rel=1e-6)


def test_liquidation_price_short_above_entry():
    liq = estimate_liquidation_price(65_000, leverage=10, side="short")
    assert liq > 65_000
    assert liq == pytest.approx(65_000 * 1.095, rel=1e-6)


def test_liquidation_price_invalid_side_raises():
    with pytest.raises(ValueError):
        estimate_liquidation_price(65_000, leverage=10, side="sideways")


def test_stop_before_liquidation_passes_with_recommended_1pct_stop():
    # 10배 레버리지, 진입가 대비 1.25% 손절 -> 청산가(약 9.5% 밖)까지 충분한 여유가 있어야 함
    entry = 65_000
    liq = estimate_liquidation_price(entry, leverage=10, side="long")
    stop = entry * (1 - 0.0125)
    decision = check_stop_before_liquidation(entry, stop, "long", liq)
    assert decision.allowed is True


def test_stop_before_liquidation_fails_when_stop_past_liquidation():
    entry = 65_000
    liq = estimate_liquidation_price(entry, leverage=10, side="long")
    stop = liq - 100  # 청산가보다 더 낮은 손절가 (있을 수 없는 설정)
    decision = check_stop_before_liquidation(entry, stop, "long", liq)
    assert decision.allowed is False


def test_stop_before_liquidation_fails_when_too_close_to_liquidation():
    entry = 65_000
    liq = estimate_liquidation_price(entry, leverage=10, side="long")
    # 청산가 바로 위, 거의 붙어있는 손절가 -> 버퍼 부족
    stop = liq + (entry - liq) * 0.05
    decision = check_stop_before_liquidation(entry, stop, "long", liq)
    assert decision.allowed is False


def test_stop_before_liquidation_short_side():
    entry = 65_000
    liq = estimate_liquidation_price(entry, leverage=10, side="short")
    stop = entry * (1 + 0.0125)
    decision = check_stop_before_liquidation(entry, stop, "short", liq)
    assert decision.allowed is True
