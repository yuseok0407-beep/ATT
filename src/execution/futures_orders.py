VALID_SIDES = ("long", "short")


def _guard_live(env: str, confirm_live: bool) -> None:
    if env == "live" and not confirm_live:
        raise RuntimeError(
            "env='live'에서 confirm_live=True 없이 주문 실행이 차단되었습니다."
        )


def get_bracket_prices(client, symbol: str, strict: bool = False) -> tuple[float | None, float | None]:
    """현재 걸려있는 손절/익절(algo/conditional) 주문에서 각각의 트리거 가격을 찾는다. 없으면 None.
    대시보드와 futures_rule_bot 양쪽에서 쓰던 걸 여기 하나로 합침(2026-08-14).

    strict=True면 조회 실패를 예외로 그대로 올린다. 기본값(False)은 조회 실패도 (None, None)로
    뭉뚱그리는데, 화면에 "-"를 띄우는 용도로는 그걸로 충분하지만 **"손절 주문이 없다"를 판정할
    때는 치명적이다** — 일시적 API 오류와 "정말로 무보호"가 구별이 안 돼서 멀쩡한 포지션에
    무보호 경보를 쏘게 된다(2026-09-09, 무보호 포지션 감지 추가하면서 분리)."""
    try:
        orders = client.fetch_open_orders(symbol, params={"trigger": True})
    except Exception:
        if strict:
            raise
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


def _close_naked_position(client, symbol: str, exit_side: str, quantity: float) -> None:
    """손절/익절 주문 중 하나라도 거부되어 보호장치 없는 포지션이 남았을 때 즉시 시장가로
    청산한다. 먼저 걸렸을 수 있는 나머지 하나(예: 손절은 성공, 익절만 실패)를 cleanup_stale_orders
    로 정리한 뒤 청산 — 안 그러면 포지션이 사라진 뒤에도 그 주문이 고아로 남는다. 이 함수 자체가
    실패해도(거래소 hiccup 등) 예외를 삼키지 않는다 — 포지션이 안 닫혔다는 걸 호출자가 알아야
    한다."""
    cleanup_stale_orders(client, symbol)
    client.create_order(symbol, type="market", side=exit_side, amount=quantity, params={"reduceOnly": True})


def open_position(client, symbol: str, side: str, quantity: float, stop_loss_price: float,
                   env: str = "demo", confirm_live: bool = False) -> dict:
    """시장가로 포지션을 열고, 곧바로 반대 방향 reduceOnly STOP_MARKET 주문으로 손절을 건다.
    손절 주문이 없는 레버리지 포지션은 절대 열지 않는다 — 이게 이 함수가 존재하는 이유다."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")
    _guard_live(env, confirm_live)

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    entry_side = "buy" if side == "long" else "sell"
    entry_order = client.create_order(symbol, type="market", side=entry_side, amount=quantity)

    stop_side = "sell" if side == "long" else "buy"
    try:
        stop_order = client.create_order(
            symbol, type="market", side=stop_side, amount=quantity,
            params={"stopLossPrice": stop_loss_price, "reduceOnly": True},
        )
    except Exception:
        # 진입은 이미 체결됐는데 손절 주문이 거부되면(예: 체결 사이 급격한 가격 변동으로 트리거
        # 가격이 이미 현재가를 넘어서 바이낸스가 -2021 "Order would immediately trigger"로 거부하는
        # 경우, 2026-08-22 실전 확인 — ETH/SOL 숏 진입이 이렇게 손절 없이 남았었음) 손절 없는
        # 포지션이 그대로 남는다 — 이 함수의 존재 이유를 위배하므로 즉시 청산하고 원래 예외를
        # 그대로 올려서 호출자(run_once의 심볼별 예외 격리)가 이 사이클을 실패로 인지하게 한다.
        _close_naked_position(client, symbol, stop_side, quantity)
        raise

    return {
        "status": "opened", "symbol": symbol, "side": side, "quantity": quantity,
        "stop_loss_price": stop_loss_price, "entry_order": entry_order, "stop_order": stop_order,
    }


def open_position_with_bracket(client, symbol: str, side: str, quantity: float, stop_loss_price: float,
                                take_profit_price: float, env: str = "demo",
                                confirm_live: bool = False) -> dict:
    """시장가로 포지션을 열고, 반대 방향 reduceOnly 손절(STOP_MARKET) + 익절(TAKE_PROFIT_MARKET)
    주문을 둘 다 건다. 바이낸스 선물엔 스팟 같은 네이티브 OCO가 없어서, 둘 중 하나가 체결되면
    나머지 하나는 고아로 남는다 — 감시 루프가 포지션이 사라진 걸 감지할 때마다
    cleanup_stale_orders()를 호출해 정리해야 한다."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")
    _guard_live(env, confirm_live)

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    entry_side = "buy" if side == "long" else "sell"
    entry_order = client.create_order(symbol, type="market", side=entry_side, amount=quantity)

    exit_side = "sell" if side == "long" else "buy"
    try:
        stop_order = client.create_order(
            symbol, type="market", side=exit_side, amount=quantity,
            params={"stopLossPrice": stop_loss_price, "reduceOnly": True},
        )
        take_profit_order = client.create_order(
            symbol, type="market", side=exit_side, amount=quantity,
            params={"takeProfitPrice": take_profit_price, "reduceOnly": True},
        )
    except Exception:
        # 진입은 이미 체결됐는데 손절/익절 중 하나라도 거부되면(예: 체결 사이 급격한 가격 변동으로
        # 트리거 가격이 이미 현재가를 넘어서 바이낸스가 -2021 "Order would immediately trigger"로
        # 거부하는 경우, 2026-08-22 실전 확인 — ETH/SOL 숏 진입이 이렇게 손절·익절 둘 다 없이
        # 남았었음) 보호장치 없는 포지션이 그대로 남는다 — 즉시 청산하고 원래 예외를 그대로 올려서
        # 호출자(run_once의 심볼별 예외 격리)가 이 사이클을 실패로 인지하게 한다.
        _close_naked_position(client, symbol, exit_side, quantity)
        raise

    return {
        "status": "opened", "symbol": symbol, "side": side, "quantity": quantity,
        "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price,
        "entry_order": entry_order, "stop_order": stop_order, "take_profit_order": take_profit_order,
    }


def close_position(client, symbol: str, side: str, quantity: float, env: str = "demo",
                    confirm_live: bool = False) -> dict:
    """보유 포지션을 시장가로 청산하고, 남아있는 손절/익절 주문을 정리한다."""
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side!r}, expected one of {VALID_SIDES}")
    _guard_live(env, confirm_live)

    if quantity <= 0:
        return {"status": "skipped", "reason": "quantity<=0", "symbol": symbol, "side": side}

    close_side = "sell" if side == "long" else "buy"
    order = client.create_order(symbol, type="market", side=close_side, amount=quantity,
                                 params={"reduceOnly": True})
    cleanup_stale_orders(client, symbol)

    return {"status": "closed", "symbol": symbol, "side": side, "quantity": quantity, "order": order}
