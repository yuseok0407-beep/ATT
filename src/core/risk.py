from dataclasses import dataclass

MAX_RISK_PER_TRADE = 0.02  # 거래당 포트폴리오의 최대 2%까지만 위험 노출
MAX_DAILY_LOSS_PCT = 0.05  # 하루 5% 손실 시 그날은 신규 거래 전면 중단
MAX_CONSECUTIVE_LOSSES = 5  # 연속 5연패 시 중단 (전략/시장 미스매치 신호). 2026-08-11 데모 검증
# 당시 3으로 설정했다가, 2026-08-22 실계좌에서 3연패(사실상 노이즈성 손실 몰림, UPDATE_LOG 참고)로
# 너무 쉽게 걸리는 걸 확인 — 데모 22거래 68% 승률 실적을 감안해 사용자 판단으로 5로 상향.
MAX_SINGLE_ORDER_PCT = 0.5  # 배분 로직 버그가 있어도 포트폴리오의 절반을 넘는 단일 주문은 무조건 차단


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


def position_size(portfolio_value: float, entry_price: float, stop_loss_price: float,
                   risk_per_trade: float = MAX_RISK_PER_TRADE) -> float:
    """리스크 기반 포지션 사이징: (포트폴리오 * 거래당 위험 비율) / (진입가-손절가 거리) = 매수 수량.
    stop_loss_price가 entry_price와 같으면(손절폭 0) 리스크를 정의할 수 없으므로 0을 반환한다."""
    if portfolio_value <= 0:
        return 0.0
    per_unit_risk = abs(entry_price - stop_loss_price)
    if per_unit_risk == 0:
        return 0.0
    risk_amount = portfolio_value * risk_per_trade
    return risk_amount / per_unit_risk


def check_circuit_breaker(daily_pnl_pct: float, consecutive_losses: int) -> RiskDecision:
    """당일 실현 손익률(daily_pnl_pct, 음수=손실)과 연속 손실 횟수를 보고 신규 거래 허용 여부를 판단."""
    if daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
        return RiskDecision(False, f"일일 손실 한도 초과 ({daily_pnl_pct:.1%} <= -{MAX_DAILY_LOSS_PCT:.0%})")
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        return RiskDecision(False, f"연속 손실 {consecutive_losses}회로 임계치 도달")
    return RiskDecision(True, "정상")


def validate_order(order_value: float, portfolio_value: float) -> RiskDecision:
    """상위 로직(배분/의사결정)의 버그로 비정상적으로 큰 주문이 내려와도 최종 방어선에서 차단."""
    if portfolio_value <= 0:
        return RiskDecision(False, "포트폴리오 평가금액이 0 이하")
    if abs(order_value) > portfolio_value * MAX_SINGLE_ORDER_PCT:
        return RiskDecision(
            False,
            f"단일 주문 규모({order_value:.2f})가 포트폴리오의 {MAX_SINGLE_ORDER_PCT:.0%} 한도를 초과",
        )
    return RiskDecision(True, "정상")
