from src.core import decision
from src.core.allocation import compute_rebalance, target_allocation
from src.core.risk import check_circuit_breaker, validate_order
from src.core.state import get_daily_pnl_pct
from src.data.exchange import get_balance, get_client, get_market_data_client, get_ticker
from src.data.research import build_snapshot
from src.execution.journal import append_entry
from src.execution.orders import execute_order

UNIVERSE = ["BTC/USDT", "ETH/USDT"]


def get_asset_values(trading_client) -> dict:
    """테스트넷 잔고를 조회하고, 시세는 공개 데이터로 평가해 자산별 USDT 환산 가치를 계산한다."""
    balance = get_balance(trading_client)
    market_client = get_market_data_client()

    values = {}
    for symbol in UNIVERSE:
        base_asset = symbol.split("/")[0]
        amount = (balance.get(base_asset) or {}).get("total") or 0.0
        if amount > 0:
            price = get_ticker(market_client, symbol)["last"]
            values[symbol] = amount * price
        else:
            values[symbol] = 0.0

    values["USDT"] = (balance.get("USDT") or {}).get("total") or 0.0
    return values


def run_cycle(consecutive_losses: int = 0, daily_pnl_pct: float = None) -> dict:
    """리서치 -> 레짐 -> 배분 -> 리스크 게이트 -> Claude 의사결정 -> 실행 -> 저널링, 한 사이클.

    daily_pnl_pct를 생략하면 당일 시작 자산 대비 실현 손익률을 자동 계산한다.
    consecutive_losses는 아직 자동 계산되지 않는다 (state.py의 한계 참고) — 필요 시 호출자가 전달.

    반환값: {"portfolio_value", "daily_pnl_pct", "regime", "results"} — results는 종목별/이벤트별 항목 리스트.
    """
    trading_client = get_client()
    claude_client = decision.get_client()

    asset_values = get_asset_values(trading_client)
    portfolio_value = sum(asset_values.values())

    if daily_pnl_pct is None:
        daily_pnl_pct = get_daily_pnl_pct(portfolio_value)

    btc_snapshot = build_snapshot("BTC/USDT")
    regime_label = btc_snapshot["regime"]["label"]

    cycle_summary = {
        "portfolio_value": portfolio_value,
        "daily_pnl_pct": daily_pnl_pct,
        "regime": regime_label,
    }

    circuit_breaker = check_circuit_breaker(daily_pnl_pct, consecutive_losses)
    if not circuit_breaker.allowed:
        entry = {"event": "circuit_breaker_blocked", "reason": circuit_breaker.reason}
        append_entry(entry)
        cycle_summary["results"] = [entry]
        return cycle_summary

    target = target_allocation(regime_label)
    rebalance = compute_rebalance(asset_values, target)

    results = []
    for symbol, order_value in rebalance.items():
        order_check = validate_order(order_value, portfolio_value)
        if not order_check.allowed:
            entry = {"symbol": symbol, "event": "order_rejected", "reason": order_check.reason}
            append_entry(entry)
            results.append(entry)
            continue

        snapshot = btc_snapshot if symbol == "BTC/USDT" else build_snapshot(symbol)
        suggested_quantity = round(abs(order_value) / snapshot["price"], 6)

        portfolio_ctx = {
            "asset_values": asset_values,
            "portfolio_value": round(portfolio_value, 2),
            "suggested_order_value_usdt": round(order_value, 2),
            "suggested_quantity": suggested_quantity,
        }
        risk_ctx = {"regime": regime_label, "circuit_breaker_status": circuit_breaker.reason}

        raw = decision.decide(claude_client, snapshot, portfolio_ctx, risk_ctx)
        gated = decision.apply_confidence_gate(raw)

        entry = {"symbol": symbol, "snapshot": snapshot, "decision": gated}
        if gated["decision"] in ("BUY", "SELL"):
            side = "buy" if gated["decision"] == "BUY" else "sell"
            exec_result = execute_order(trading_client, symbol, side, gated["quantity"])
            entry["execution"] = exec_result

        append_entry(entry)
        results.append(entry)

    cycle_summary["results"] = results
    return cycle_summary
