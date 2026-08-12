CASH = "USDT"

# 레짐별 목표 비중. 모든 프리셋은 CASH를 포함해 합이 1.0이 되어야 한다(테스트에서 검증).
REGIME_PRESETS = {
    "TRENDING_UP": {"BTC/USDT": 0.40, "ETH/USDT": 0.30, CASH: 0.30},
    "TRENDING_DOWN": {"BTC/USDT": 0.10, "ETH/USDT": 0.10, CASH: 0.80},
    "HIGH_VOLATILITY": {"BTC/USDT": 0.15, "ETH/USDT": 0.15, CASH: 0.70},
    "RANGING": {"BTC/USDT": 0.25, "ETH/USDT": 0.25, CASH: 0.50},
}

REBALANCE_THRESHOLD = 0.05  # 목표 비중과 5%p 이상 벌어진 자산만 리밸런싱 (잦은 매매/수수료 방지)


def target_allocation(regime_label: str) -> dict:
    if regime_label not in REGIME_PRESETS:
        raise ValueError(f"unknown regime label: {regime_label}")
    return dict(REGIME_PRESETS[regime_label])


def compute_rebalance(
    asset_values: dict,
    target_weights: dict,
    threshold: float = REBALANCE_THRESHOLD,
) -> dict:
    """현재 자산별 평가금액(asset_values)을 목표 비중(target_weights)에 맞추기 위한
    매매 금액을 계산한다. 양수는 매수, 음수는 매도. 편차가 threshold 미만인 자산은 건드리지 않는다.
    CASH(USDT)는 매매 대상이 아니라 잔여 자산으로 취급하여 결과에서 제외한다.
    """
    total_value = sum(asset_values.values())
    if total_value <= 0:
        return {}

    orders = {}
    for asset, target_weight in target_weights.items():
        if asset == CASH:
            continue
        current_value = asset_values.get(asset, 0.0)
        current_weight = current_value / total_value
        deviation = target_weight - current_weight
        if abs(deviation) < threshold:
            continue
        orders[asset] = round(deviation * total_value, 2)

    return orders
