import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, jsonify, render_template

from src.core.config import (
    FUTURES_SYMBOLS,
    LEVERAGE,
    MAX_CONCURRENT_POSITIONS,
    RULE_ADX_THRESHOLD,
    RULE_SMA_PERIOD,
    RULE_TIMEFRAME,
    STOP_LOSS_PCT,
    TAKE_PROFIT_RR,
)
from src.core.risk import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS_PCT
from src.core.state import compute_consecutive_losses, get_daily_pnl_pct
from src.data.exchange import fetch_ohlcv_df
from src.data.futures_exchange import get_futures_balance, get_futures_client, get_position
from src.execution import bot_process
from src.execution.futures_orders import close_position, get_bracket_prices
from src.execution.heartbeat import read_heartbeat
from src.execution.journal import read_entries
from src.execution.performance import summarize_performance
from src.futures_rule_bot import JOURNAL_PATH, STATE_PATH, record_manual_close

app = Flask(__name__)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = get_futures_client()
        _client.load_markets()
    return _client


def _available_symbols(client):
    """FUTURES_SYMBOLS 중 지금 연결된 거래소 환경(데모 트레이딩 등)에 실제로 있는 것만.
    SOXL처럼 실거래 선물엔 있어도 데모 트레이딩엔 없는 심볼이 있어 — 그대로 조회하면 BadSymbol로
    /api/status 전체가 죽는다(futures_rule_bot.initialize()와 동일한 이유로 필터링)."""
    return [s for s in FUTURES_SYMBOLS if s in client.markets]


@app.route("/")
def index():
    client = _get_client()
    return render_template("index.html", symbols=_available_symbols(client), leverage=LEVERAGE,
                            max_concurrent=MAX_CONCURRENT_POSITIONS)


@app.route("/api/status")
def api_status():
    client = _get_client()

    balance = get_futures_balance(client)
    margin_equity = (balance.get("USDT") or {}).get("total") or 0.0

    symbols = {}
    open_count = 0
    for symbol in _available_symbols(client):
        position = get_position(client, symbol)
        stop_loss_price, take_profit_price = (None, None)
        if position is not None:
            open_count += 1
            stop_loss_price, take_profit_price = get_bracket_prices(client, symbol)
        symbols[symbol] = {
            "has_position": position is not None,
            "position": position,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
        }

    all_entries = read_entries(path=JOURNAL_PATH)
    entries = list(reversed(all_entries))[:30]
    daily_pnl_pct = get_daily_pnl_pct(margin_equity, path=STATE_PATH)
    consecutive_losses = compute_consecutive_losses(all_entries)

    return jsonify({
        "symbols": symbols,
        "leverage": LEVERAGE,
        "max_concurrent": MAX_CONCURRENT_POSITIONS,
        "margin_equity": margin_equity,
        "open_position_count": open_count,
        "recent_entries": entries,
        "daily_pnl_pct": daily_pnl_pct,
        "risk_limits": {
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "consecutive_losses": consecutive_losses,
        },
        "config": {
            "adx_threshold": RULE_ADX_THRESHOLD,
            "sma_period": RULE_SMA_PERIOD,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_rr": TAKE_PROFIT_RR,
        },
    })


@app.route("/api/performance")
def api_performance():
    entries = read_entries(path=JOURNAL_PATH)
    return jsonify(summarize_performance(entries))


@app.route("/api/chart/<path:symbol>")
def api_chart(symbol):
    """대시보드의 종목 클릭 차트용 — 전략이 실제로 보는 것과 같은 타임프레임(RULE_TIMEFRAME)의
    최근 캔들을 그대로 돌려준다."""
    client = _get_client()
    if symbol not in client.markets:
        return jsonify({"status": "error", "message": "알 수 없는 심볼입니다."}), 404

    df = fetch_ohlcv_df(client, symbol, timeframe=RULE_TIMEFRAME, limit=100)
    candles = [
        {"time": row.timestamp.isoformat(), "open": row.open, "high": row.high,
         "low": row.low, "close": row.close}
        for row in df.itertuples()
    ]
    return jsonify({"symbol": symbol, "timeframe": RULE_TIMEFRAME, "candles": candles})


@app.route("/api/close/<path:symbol>", methods=["POST"])
def api_close(symbol):
    """대시보드에서 사용자가 직접 누르는 긴급 청산. 버튼 클릭 자체가 명시적 의사표시이므로
    confirm_live=True를 항상 넘긴다 — 자동매매 로직에 걸려있는 안전장치(confirm_live 요구)는
    "자동으로 실거래가 나가는 것"을 막기 위한 것이지, 사용자가 직접 누른 청산까지 막을 이유는 없다."""
    client = _get_client()
    position = get_position(client, symbol)
    if position is None:
        return jsonify({"status": "error", "message": "해당 종목에 보유 중인 포지션이 없습니다."}), 400

    try:
        result = close_position(client, symbol, position["side"], abs(position["contracts"]), confirm_live=True)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    record_manual_close(client, symbol)
    return jsonify(result)


@app.route("/api/bot/status")
def api_bot_status():
    return jsonify({**bot_process.get_status(), "heartbeat": read_heartbeat()})


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    return jsonify(bot_process.start())


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    return jsonify(bot_process.stop())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)
