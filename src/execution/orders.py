from src.core.config import USE_TESTNET

VALID_SIDES = ("buy", "sell")


def execute_order(client, symbol: str, side: str, quantity: float, confirm_live: bool = False) -> dict:
    """시장가 주문을 실행한다. 실거래 모드(USE_TESTNET=false)에서는 confirm_live=True를
    명시적으로 넘기지 않으면 무조건 차단한다 (설정 실수로 인한 실거래 방지용 이중 안전장치)."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")

    if not USE_TESTNET and not confirm_live:
        raise RuntimeError(
            "실거래 모드(USE_TESTNET=false)에서 confirm_live=True 없이 주문 실행이 차단되었습니다."
        )

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    order = client.create_order(symbol, type="market", side=side, amount=quantity)
    return {"status": "filled", "symbol": symbol, "side": side, "quantity": quantity, "raw": order}
