from src.core.risk import RiskDecision

DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005  # 바이낸스 BTCUSDT 최저 구간 근사치(실제는 티어별로 다름)
MIN_STOP_TO_LIQUIDATION_BUFFER = 0.20  # 손절가~청산가 거리가 진입가~청산가 거리의 최소 20%는 되어야 함


def leveraged_position_size(margin_equity: float, entry_price: float, stop_loss_price: float,
                             leverage: int, risk_per_trade: float, max_notional: float | None = None) -> float:
    """리스크 기반 수량 계산은 레버리지와 무관하다 — 손절 시 잃을 금액(margin_equity * risk_per_trade)을
    가격 변동폭(entry-stop)으로 나누면 그대로 나온다. 레버리지는 '얼마나 큰 포지션을 열 수 있는가'라는
    별도의 상한으로만 작용하므로, 그 상한을 넘지 않도록 min()으로 캡을 씌운다.

    max_notional: 거래소가 이 leverage에서 허용하는 최대 포지션 명목가치(USDT, 심볼별 레버리지
    구간에 따라 다름 — get_notional_cap 참고). 계좌 자산이 커지면 리스크 기반 수량이 이 상한을
    넘을 수 있는데(예: TSLA는 5배에서 cap이 $5000뿐이라 계좌가 $5000만 넘어도 걸림), 이걸 몰랐던
    탓에 거래소가 -2027으로 주문을 거부하는 사고가 있었다(2026-08-18). None이면 이 상한은
    적용하지 않는다(예: 호출자가 조회에 실패한 경우)."""
    if margin_equity <= 0 or entry_price <= 0:
        return 0.0
    per_unit_risk = abs(entry_price - stop_loss_price)
    if per_unit_risk == 0:
        return 0.0

    risk_based_quantity = (margin_equity * risk_per_trade) / per_unit_risk
    max_quantity_by_leverage = (margin_equity * leverage) / entry_price
    quantity = min(risk_based_quantity, max_quantity_by_leverage)
    if max_notional is not None:
        quantity = min(quantity, max_notional / entry_price)
    return max(0.0, quantity)


def required_margin(quantity: float, entry_price: float, leverage: int) -> float:
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    return (quantity * entry_price) / leverage


def estimate_liquidation_price(entry_price: float, leverage: int, side: str,
                                maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE) -> float:
    """격리 마진 기준 근사 청산가. 실제 청산가는 바이낸스가 유지증거금 구간(티어)별로 다르게 계산하므로
    포지션 진입 후에는 반드시 거래소가 돌려주는 실제 liquidationPrice 값을 사용해야 한다 — 이 함수는
    진입 전 스탑로스 폭을 정할 때 대략적인 안전 마진을 가늠하기 위한 추정치일 뿐이다."""
    if side == "long":
        return entry_price * (1 - 1 / leverage + maintenance_margin_rate)
    elif side == "short":
        return entry_price * (1 + 1 / leverage - maintenance_margin_rate)
    raise ValueError(f"invalid side: {side!r}")


def check_stop_before_liquidation(entry_price: float, stop_loss_price: float, side: str,
                                   liquidation_price: float,
                                   min_buffer: float = MIN_STOP_TO_LIQUIDATION_BUFFER) -> RiskDecision:
    """손절 주문이 청산가보다 먼저(유리한 가격에서) 체결되고, 청산가까지 최소한의 여유가 있는지 검증한다.
    이게 없으면 시장 슬리피지로 손절이 늦게 체결되어 청산으로 이어질 수 있다."""
    if side == "long":
        if stop_loss_price <= liquidation_price:
            return RiskDecision(False, "손절가가 청산가 이하 — 청산이 손절보다 먼저 발생할 수 있음")
        distance_entry_to_liq = entry_price - liquidation_price
        distance_stop_to_liq = stop_loss_price - liquidation_price
    elif side == "short":
        if stop_loss_price >= liquidation_price:
            return RiskDecision(False, "손절가가 청산가 이상 — 청산이 손절보다 먼저 발생할 수 있음")
        distance_entry_to_liq = liquidation_price - entry_price
        distance_stop_to_liq = liquidation_price - stop_loss_price
    else:
        raise ValueError(f"invalid side: {side!r}")

    if distance_entry_to_liq <= 0:
        return RiskDecision(False, "진입가와 청산가 관계가 올바르지 않음")

    buffer_ratio = distance_stop_to_liq / distance_entry_to_liq
    if buffer_ratio < min_buffer:
        return RiskDecision(
            False,
            f"손절가~청산가 여유가 {buffer_ratio:.0%}로 최소 {min_buffer:.0%} 미만 — 레버리지를 낮추거나 손절폭을 좁혀야 함",
        )
    return RiskDecision(True, "정상")
