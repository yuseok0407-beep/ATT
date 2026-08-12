from src.core import futures_decision
from src.core.config import FUTURES_RISK_PER_TRADE, FUTURES_SYMBOL, LEVERAGE, MARGIN_MODE, STOP_LOSS_PCT
from src.core.futures_risk import check_stop_before_liquidation, estimate_liquidation_price, leveraged_position_size
from src.core.risk import check_circuit_breaker
from src.core.state import get_daily_pnl_pct
from src.data.futures_exchange import get_futures_balance, get_futures_client, get_position, set_leverage, set_margin_mode
from src.data.research import build_snapshot
from src.execution.futures_orders import close_position, open_position
from src.execution.journal import append_entry

JOURNAL_PATH = "journal/futures_trades.jsonl"
STATE_PATH = "state/futures_daily_equity.json"
INDICATOR_SYMBOL = "BTC/USDT"  # build_snapshot()이 기대하는 현물 표기 심볼 (지표용 공개 시세)


def _stop_loss_price(entry_price: float, side: str) -> float:
    return entry_price * (1 - STOP_LOSS_PCT) if side == "long" else entry_price * (1 + STOP_LOSS_PCT)


def run_futures_cycle(consecutive_losses: int = 0, daily_pnl_pct: float = None) -> dict:
    """선물 사이클: 계좌/포지션 조회 -> 서킷 브레이커 -> Claude 방향 판단 -> (필요시) 청산가 안전검증 후 진입/청산.

    손절폭은 정책상 고정값(STOP_LOSS_PCT)이며 Claude가 바꿀 수 없다 — futures_decision 참고.
    """
    trading_client = get_futures_client()
    claude_client = futures_decision.get_client()

    set_margin_mode(trading_client, FUTURES_SYMBOL, MARGIN_MODE)
    set_leverage(trading_client, FUTURES_SYMBOL, LEVERAGE)

    balance = get_futures_balance(trading_client)
    margin_equity = (balance.get("USDT") or {}).get("total") or 0.0

    if daily_pnl_pct is None:
        daily_pnl_pct = get_daily_pnl_pct(margin_equity, path=STATE_PATH)

    position = get_position(trading_client, FUTURES_SYMBOL)
    has_position = position is not None

    circuit_breaker = check_circuit_breaker(daily_pnl_pct, consecutive_losses)
    cycle_summary = {"margin_equity": margin_equity, "daily_pnl_pct": daily_pnl_pct, "has_position": has_position}

    if not circuit_breaker.allowed:
        entry = {"event": "circuit_breaker_blocked", "reason": circuit_breaker.reason}
        append_entry(entry, path=JOURNAL_PATH)
        cycle_summary["results"] = [entry]
        return cycle_summary

    snapshot = build_snapshot(INDICATOR_SYMBOL)

    account_ctx = {
        "margin_equity": round(margin_equity, 2),
        "has_position": has_position,
        "position": position,
        "leverage": LEVERAGE,
    }
    risk_ctx = {"circuit_breaker_status": circuit_breaker.reason}

    raw = futures_decision.decide(claude_client, snapshot, account_ctx, risk_ctx)
    validated = futures_decision.validate_decision(raw, has_position)
    gated = futures_decision.apply_confidence_gate(validated)

    entry = {"decision": gated, "snapshot": snapshot}

    if gated["decision"] in ("LONG", "SHORT"):
        side = "long" if gated["decision"] == "LONG" else "short"
        entry_price = snapshot["price"]
        stop_loss_price = _stop_loss_price(entry_price, side)
        liquidation_estimate = estimate_liquidation_price(entry_price, LEVERAGE, side)
        safety = check_stop_before_liquidation(entry_price, stop_loss_price, side, liquidation_estimate)

        if not safety.allowed:
            entry["event"] = "rejected_unsafe_stop"
            entry["reason"] = safety.reason
        else:
            quantity = leveraged_position_size(margin_equity, entry_price, stop_loss_price, LEVERAGE, FUTURES_RISK_PER_TRADE)
            if quantity <= 0:
                entry["event"] = "rejected_zero_quantity"
            else:
                entry["execution"] = open_position(trading_client, FUTURES_SYMBOL, side, quantity, stop_loss_price)

    elif gated["decision"] == "CLOSE":
        side = position["side"]
        quantity = abs(position["contracts"])
        entry["execution"] = close_position(trading_client, FUTURES_SYMBOL, side, quantity)

    append_entry(entry, path=JOURNAL_PATH)
    cycle_summary["results"] = [entry]
    return cycle_summary
