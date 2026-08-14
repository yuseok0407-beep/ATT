from src.core.config import USE_TESTNET

VALID_SIDES = ("long", "short")


def _guard_live(confirm_live: bool) -> None:
    if not USE_TESTNET and not confirm_live:
        raise RuntimeError(
            "실거래 모드(USE_TESTNET=false)에서 confirm_live=True 없이 주문 실행이 차단되었습니다."
        )


def get_bracket_prices(client, symbol: str) -> tuple[float | None, float | None]:
    """현재 걸려있는 손절/익절(algo/conditional) 주문에서 각각의 트리거 가격을 찾는다. 없으면 None.
    대시보드와 futures_rule_bot 양쪽에서 쓰던 걸 여기 하나로 합침(2026-08-14)."""
    try:
        orders = client.fetch_open_orders(symbol, params={"trigger": True})
    except Exception:
        return None, None

    stop_price, take_profit_price = None, None
    for order in orders:
        order_type = (order.get("info") or {}).get("orderType", "")
        trigger = order.get("triggerPrice") or order.get("stopPrice")
        if order_type == "STOP_MARKET":
            stop_price = trigger
        elif order_type == "TAKE_PROFIT_MARKET":
            take_profit_price = trigger
    return stop_price, take_profit_price


def cleanup_stale_orders(client, symbol: str) -> None:
    """포지션이 닫혔는데 남아있는 손절/익절(algo/conditional) 주문을 정리한다.
    우리 SL/TP는 stopLossPrice/takeProfitPrice로 넣기 때문에 바이낸스의 algo 주문 계열로 들어가서,
    일반 cancel_all_orders(symbol) 한 번으로는 지워지지 않는다 — 반드시 두 번 다 호출해야 한다.
    (SL/TP 중 하나가 체결돼 포지션이 사라지면 나머지 하나가 고아로 남는 문제를 막기 위함)"""
    client.cancel_all_orders(symbol)
    client.cancel_all_orders(symbol, params={"trigger": True})


def open_position(client, symbol: str, side: str, quantity: float, stop_loss_price: float,
                   confirm_live: bool = False) -> dict:
    """시장가로 포지션을 열고, 곧바로 반대 방향 reduceOnly STOP_MARKET 주문으로 손절을 건다.
    손절 주문이 없는 레버리지 포지션은 절대 열지 않는다 — 이게 이 함수가 존재하는 이유다."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")
    _guard_live(confirm_live)

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    entry_side = "buy" if side == "long" else "sell"
    entry_order = client.create_order(symbol, type="market", side=entry_side, amount=quantity)

    stop_side = "sell" if side == "long" else "buy"
    stop_order = client.create_order(
        symbol, type="market", side=stop_side, amount=quantity,
        params={"stopLossPrice": stop_loss_price, "reduceOnly": True},
    )

    return {
        "status": "opened", "symbol": symbol, "side": side, "quantity": quantity,
        "stop_loss_price": stop_loss_price, "entry_order": entry_order, "stop_order": stop_order,
    }


def open_position_with_bracket(client, symbol: str, side: str, quantity: float, stop_loss_price: float,
                                take_profit_price: float, confirm_live: bool = False) -> dict:
    """시장가로 포지션을 열고, 반대 방향 reduceOnly 손절(STOP_MARKET) + 익절(TAKE_PROFIT_MARKET)
    주문을 둘 다 건다. 바이낸스 선물엔 스팟 같은 네이티브 OCO가 없어서, 둘 중 하나가 체결되면
    나머지 하나는 고아로 남는다 — 감시 루프가 포지션이 사라진 걸 감지할 때마다
    cleanup_stale_orders()를 호출해 정리해야 한다."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")
    _guard_live(confirm_live)

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    entry_side = "buy" if side == "long" else "sell"
    entry_order = client.create_order(symbol, type="market", side=entry_side, amount=quantity)

    exit_side = "sell" if side == "long" else "buy"
    stop_order = client.create_order(
        symbol, type="market", side=exit_side, amount=quantity,
        params={"stopLossPrice": stop_loss_price, "reduceOnly": True},
    )
    take_profit_order = client.create_order(
        symbol, type="market", side=exit_side, amount=quantity,
        params={"takeProfitPrice": take_profit_price, "reduceOnly": True},
    )

    return {
        "status": "opened", "symbol": symbol, "side": side, "quantity": quantity,
        "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price,
        "entry_order": entry_order, "stop_order": stop_order, "take_profit_order": take_profit_order,
    }


def close_position(client, symbol: str, side: str, quantity: float, confirm_live: bool = False) -> dict:
    """보유 포지션을 시장가로 청산하고, 남아있는 손절/익절 주문을 정리한다."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")
    _guard_live(confirm_live)

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    close_side = "sell" if side == "long" else "buy"
    order = client.create_order(symbol, type="market", side=close_side, amount=quantity,
                                 params={"reduceOnly": True})
    cleanup_stale_orders(client, symbol)

    return {"status": "closed", "symbol": symbol, "side": side, "quantity": quantity, "order": order}
