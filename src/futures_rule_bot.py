import json
import logging
from datetime import datetime
from pathlib import Path

from src.core.config import (
    FUTURES_RISK_PER_TRADE,
    FUTURES_SYMBOLS,
    LEVERAGE,
    MARGIN_MODE,
    MAX_CONCURRENT_POSITIONS,
    RULE_TIMEFRAME,
)
from src.core.futures_risk import check_stop_before_liquidation, estimate_liquidation_price, leveraged_position_size
from src.core.futures_strategy import compute_bracket_prices, detect_signal
from src.core.risk import check_circuit_breaker
from src.core.state import compute_consecutive_losses, get_daily_pnl_pct
from src.data.exchange import fetch_ohlcv_df
from src.data.futures_exchange import (
    get_futures_balance,
    get_futures_client,
    get_futures_market_data_client,
    get_position,
    set_leverage,
    set_margin_mode,
)
from src.execution.futures_orders import cleanup_stale_orders, open_position_with_bracket
from src.execution.journal import append_entry, read_entries

JOURNAL_PATH = "journal/futures_rule_trades.jsonl"
STATE_PATH = "state/futures_rule_daily_equity.json"
LAST_TRADE_STATE_PATH = "state/futures_rule_last_trade.json"

logger = logging.getLogger(__name__)

# 저널에 남기지 않는(스팸 방지) 이벤트 — 매 사이클 반복돼도 상태 변화가 없는 것들
_SILENT_EVENTS = ("no_signal", "holding_position", "skipped_max_positions")


def _load_last_trade_ids(path: str = LAST_TRADE_STATE_PATH) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    return json.loads(file_path.read_text(encoding="utf-8"))


def _save_last_trade_id(symbol: str, trade_id: str, path: str = LAST_TRADE_STATE_PATH) -> None:
    file_path = Path(path)
    data = _load_last_trade_ids(path)
    data[symbol] = trade_id
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data), encoding="utf-8")


def _find_last_entry_journal(symbol: str, journal_path: str = JOURNAL_PATH) -> dict | None:
    for entry in reversed(read_entries(path=journal_path)):
        if entry.get("symbol") == symbol and entry.get("event") == "entered":
            return entry
    return None


def check_and_log_closed_trade(client, symbol: str) -> dict | None:
    """지금 포지션이 없는(flat) 종목에 대해, 직전 체결이 아직 저널에 안 남은 청산인지 확인하고
    있으면 실현손익과 함께 기록한다. 손절/익절 중 어느 쪽이었는지는 진입 시 기록해둔 손절가/익절가와
    체결가를 비교해 추정한다(정확한 체결가는 트리거 가격과 보통 일치하거나 매우 가깝다).

    fetch_my_trades를 since 없이 limit만 주고 부르면 "최신 N개"가 아니라 계좌에 쌓인 체결 중
    "가장 오래된 N개"가 돌아온다(실전에서 BTC 포지션으로 확인된 동작). 그 심볼의 누적 체결 수가
    limit을 넘으면 이 함수는 그 뒤로 영원히 옛날 체결(대표적으로 진입 체결 그 자체)에 멈춰서,
    실제 청산(예: 익절)을 다른 걸로 오인해 손실 0으로 잘못 기록하는 사고가 실제로 있었다. 그래서
    진입 시각을 since로 앵커링해 그 이후 체결만 본다. 또한 STOP_MARKET/TAKE_PROFIT_MARKET
    체결이 여러 번의 부분 체결로 쪼개질 수 있어(실전 확인됨) 단일 체결이 아니라 진입 이후의
    모든 체결을 합산한다."""
    entry_info = _find_last_entry_journal(symbol, journal_path=JOURNAL_PATH)
    if entry_info is None:
        # 이 기능이 생기기 전에 체결된 거래 등 대조할 진입 기록이 없으면, 잘못된 추정을 기록하지
        # 않도록 그냥 마지막 거래ID만 기억해두고 넘어간다.
        try:
            trades = client.fetch_my_trades(symbol, limit=5)
        except Exception:
            return None
        if trades:
            _save_last_trade_id(symbol, trades[-1].get("id"), path=LAST_TRADE_STATE_PATH)
        return None

    since_ms = int(datetime.fromisoformat(entry_info["timestamp"]).timestamp() * 1000) - 60_000
    try:
        trades = client.fetch_my_trades(symbol, since=since_ms, limit=50)
    except Exception:
        return None
    if not trades:
        return None

    entry_order_id = ((entry_info.get("execution") or {}).get("entry_order") or {}).get("id")
    if entry_order_id is not None:
        closing_trades = [t for t in trades if str((t.get("info") or {}).get("orderId")) != str(entry_order_id)]
    else:
        # 옛 저널 기록 등 진입 주문ID가 없는 경우의 대비책 — 체결가가 진입가와 정확히 같은 것만
        # 진입 체결로 간주한다(슬리피지가 있으면 완벽하지 않지만 없는 것보다 낫다).
        entry_price = entry_info.get("entry_price")
        closing_trades = [t for t in trades if t.get("price") != entry_price]

    if not closing_trades:
        return None

    last_trade_id = closing_trades[-1].get("id")
    if last_trade_id is None or _load_last_trade_ids(path=LAST_TRADE_STATE_PATH).get(symbol) == last_trade_id:
        return None

    total_qty = sum(float(t.get("amount") or 0) for t in closing_trades)
    if total_qty > 0:
        exit_price = sum(float(t.get("price") or 0) * float(t.get("amount") or 0) for t in closing_trades) / total_qty
    else:
        exit_price = closing_trades[-1].get("price")
    realized_pnl = sum(float((t.get("info") or {}).get("realizedPnl", 0) or 0) for t in closing_trades)

    stop_loss_price = entry_info.get("stop_loss_price")
    take_profit_price = entry_info.get("take_profit_price")
    reason = "unknown"
    if stop_loss_price is not None and take_profit_price is not None:
        reason = "stop_loss" if abs(exit_price - stop_loss_price) < abs(exit_price - take_profit_price) else "take_profit"

    closed_entry = {
        "symbol": symbol, "event": "closed", "reason": reason,
        "entry_price": entry_info.get("entry_price"), "exit_price": exit_price,
        "realized_pnl": realized_pnl,
    }
    append_entry(closed_entry, path=JOURNAL_PATH)
    _save_last_trade_id(symbol, last_trade_id, path=LAST_TRADE_STATE_PATH)
    return closed_entry


def record_manual_close(client, symbol: str) -> dict | None:
    """대시보드의 긴급 청산 버튼 등으로 수동 청산한 직후 호출한다. 방금 체결을 조회해서
    reason="manual"로 저널에 남기고, 감시 루프가 같은 체결을 또 손절/익절로 오인해 중복
    기록하지 않도록 마지막 거래ID 상태도 함께 갱신한다."""
    try:
        trades = client.fetch_my_trades(symbol, limit=5)
    except Exception:
        trades = []
    if not trades:
        return None

    last_trade = trades[-1]
    trade_id = last_trade.get("id")
    exit_price = last_trade.get("price")
    realized_pnl = float((last_trade.get("info") or {}).get("realizedPnl", 0) or 0)
    entry_info = _find_last_entry_journal(symbol, journal_path=JOURNAL_PATH)

    closed_entry = {
        "symbol": symbol, "event": "closed", "reason": "manual",
        "entry_price": entry_info.get("entry_price") if entry_info else None,
        "exit_price": exit_price, "realized_pnl": realized_pnl,
    }
    append_entry(closed_entry, path=JOURNAL_PATH)
    if trade_id is not None:
        _save_last_trade_id(symbol, trade_id, path=LAST_TRADE_STATE_PATH)
    return closed_entry


def initialize(client) -> list[str]:
    """루프 시작 시 한 번만 호출 — 감시하는 모든 종목의 레버리지/마진모드를 설정한다.

    FUTURES_SYMBOLS 중 지금 연결된 거래소 환경(예: 데모 트레이딩)에 아예 없는 심볼은 건너뛴다 —
    SOXL처럼 실거래 선물에는 있지만 데모 트레이딩에는 없는 경우가 실제로 있었음(BadSymbol로
    initialize() 자체가 죽어서 봇이 아예 못 뜨는 문제였음). 이번 세션에서 실제로 감시 가능한
    심볼 목록을 반환한다."""
    client.load_markets()
    available = []
    for symbol in FUTURES_SYMBOLS:
        if symbol not in client.markets:
            logger.warning("symbol %s is not available on this exchange environment — skipping", symbol)
            continue
        set_margin_mode(client, symbol, MARGIN_MODE)
        set_leverage(client, symbol, LEVERAGE)
        available.append(symbol)
    return available


def _evaluate_symbol(client, symbol: str, position: dict | None, margin_equity: float,
                      open_position_count: int) -> dict:
    """종목 하나에 대한 판단. 새 포지션을 열면 result["entered"]=True로 호출자에게 알려서
    같은 사이클 안에서 MAX_CONCURRENT_POSITIONS 카운트를 즉시 반영할 수 있게 한다."""
    if position is not None:
        return {"symbol": symbol, "has_position": True, "event": "holding_position",
                "position": position, "entered": False}

    check_and_log_closed_trade(client, symbol)
    cleanup_stale_orders(client, symbol)

    if open_position_count >= MAX_CONCURRENT_POSITIONS:
        return {"symbol": symbol, "has_position": False, "event": "skipped_max_positions", "entered": False}

    market_client = get_futures_market_data_client()
    df = fetch_ohlcv_df(market_client, symbol, timeframe=RULE_TIMEFRAME, limit=100)
    signal = detect_signal(df)

    result = {"symbol": symbol, "has_position": False, "entered": False}
    if signal is None:
        result["event"] = "no_signal"
        return result

    entry_price = float(df["close"].iloc[-1])
    side = "long" if signal == "LONG" else "short"
    stop_loss_price, take_profit_price = compute_bracket_prices(entry_price, side)
    liquidation_estimate = estimate_liquidation_price(entry_price, LEVERAGE, side)
    safety = check_stop_before_liquidation(entry_price, stop_loss_price, side, liquidation_estimate)

    result.update({
        "signal": signal, "entry_price": entry_price,
        "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price,
    })

    if not safety.allowed:
        result["event"] = "rejected_unsafe_stop"
        result["reason"] = safety.reason
        return result

    quantity = leveraged_position_size(margin_equity, entry_price, stop_loss_price, LEVERAGE, FUTURES_RISK_PER_TRADE)
    if quantity <= 0:
        result["event"] = "rejected_zero_quantity"
        return result

    result["execution"] = open_position_with_bracket(
        client, symbol, side, quantity, stop_loss_price, take_profit_price,
    )
    result["event"] = "entered"
    result["entered"] = True
    return result


def run_once(client, consecutive_losses: int = None, daily_pnl_pct: float = None, symbols: list[str] = None) -> dict:
    """감시 루프 한 사이클. symbols(기본값 FUTURES_SYMBOLS 전체)를 순회하며 판단하되, 동시 보유
    포지션이 MAX_CONCURRENT_POSITIONS에 도달하면 나머지 종목은 신규 진입을 건너뛴다
    (skipped_max_positions). symbols는 보통 initialize()가 돌려준, 이 거래소 환경에 실제로
    존재하는 심볼 목록을 그대로 넘겨받는다.

    consecutive_losses를 호출자가 안 넘기면(None) 저널에서 직접 계산한다 — 예전엔 호출자
    (run_futures_bot.py)가 이 값을 아예 안 넘겨서 항상 0으로 고정되고, 서킷브레이커의 "연속
    3연패 시 중단" 조건이 실전에서 계속 죽어있던 사고가 있었다(2026-08-12, UPDATE_LOG.md 참고).
    기본값을 0이 아니라 None으로 두고 여기서 자동 계산하게 해서, 앞으로 호출자가 깜빡 잊고
    안 넘겨도 같은 사고가 재발하지 않게 했다."""
    symbols = FUTURES_SYMBOLS if symbols is None else symbols
    balance = get_futures_balance(client)
    margin_equity = (balance.get("USDT") or {}).get("total") or 0.0

    if daily_pnl_pct is None:
        daily_pnl_pct = get_daily_pnl_pct(margin_equity, path=STATE_PATH)
    if consecutive_losses is None:
        consecutive_losses = compute_consecutive_losses(read_entries(path=JOURNAL_PATH))

    cycle = {"margin_equity": margin_equity, "daily_pnl_pct": daily_pnl_pct, "symbols": {}}

    circuit_breaker = check_circuit_breaker(daily_pnl_pct, consecutive_losses)
    if not circuit_breaker.allowed:
        entry = {"event": "circuit_breaker_blocked", "reason": circuit_breaker.reason}
        append_entry(entry, path=JOURNAL_PATH)
        cycle["event"] = entry["event"]
        return cycle

    positions = {symbol: get_position(client, symbol) for symbol in symbols}
    open_count = sum(1 for p in positions.values() if p is not None)

    for symbol in symbols:
        result = _evaluate_symbol(client, symbol, positions[symbol], margin_equity, open_count)
        if result["entered"]:
            open_count += 1
        if result["event"] not in _SILENT_EVENTS:
            append_entry({k: v for k, v in result.items() if k != "position"}, path=JOURNAL_PATH)
        cycle["symbols"][symbol] = result

    cycle["open_position_count"] = open_count
    return cycle
