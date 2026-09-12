import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, jsonify, render_template, request

logger = logging.getLogger(__name__)

from src.core.config import (
    FUTURES_SYMBOLS,
    LEVERAGE,
    MAX_CONCURRENT_POSITIONS,
    RULE_TIMEFRAME,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from src.core.risk import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS_PCT
from src.core.signal_status import collect_conditions
from src.core.state import compute_consecutive_losses, get_daily_pnl_pct
from src.data.futures_exchange import (LiveKeysNotConfiguredError, fetch_ohlcv_df, get_futures_balance,
                                        get_futures_client, get_position)
from src.data.public_ip import get_public_ip
from src.execution import bot_process, excursion, filter_stats
from src.execution.futures_orders import close_position, get_bracket_prices
from src.execution.heartbeat import DEFAULT_PATH as HEARTBEAT_DEMO_PATH
from src.execution.heartbeat import LIVE_DEFAULT_PATH as HEARTBEAT_LIVE_PATH
from src.execution.heartbeat import read_heartbeat
from src.execution.journal import read_entries
from src.execution.performance import (
    summarize_performance,
    summarize_r_performance,
    summarize_recent_issues,
)
from src.futures_rule_bot import (
    EXCURSION_PATH,
    FILTER_STATS_PATH,
    JOURNAL_PATH,
    LAST_TRADE_STATE_PATH,
    STATE_PATH,
    current_strategy_config,
    record_manual_close,
    reset_consecutive_losses,
)
from src.futures_rule_bot import (
    LIVE_EXCURSION_PATH,
    LIVE_FILTER_STATS_PATH,
    LIVE_JOURNAL_PATH,
    LIVE_LAST_TRADE_STATE_PATH,
    LIVE_STATE_PATH,
)

app = Flask(__name__)
_clients: dict[str, object] = {}  # env("demo"/"live") -> ccxt client, 지연 생성 후 캐시


def _get_client(env: str = "demo"):
    if env not in _clients:
        client = get_futures_client(env)
        client.load_markets()
        _clients[env] = client
    return _clients[env]


def _get_client_or_error(env: str):
    """(client, None) 또는 (client=None, error_response)를 돌려준다 — 라우트에서 곧장
    `client, err = ...; if err: return err`로 쓸 수 있게. 라이브 키가 아예 없는 경우
    (LiveKeysNotConfiguredError)와 키는 있지만 연결 자체가 실패하는 경우(잘못된 키, IP 제한,
    선물 계좌 미활성화 등 ccxt 예외)를 구분해서 둘 다 명확한 JSON으로 응답한다 — 안 그러면
    Flask 기본 500 HTML 페이지가 나가서 프론트가 JSON 파싱에 실패하고 "연결 실패"로만
    뭉뚱그려지며 화면이 이전 상태(예: 데모 데이터)에 멈춰있게 된다(2026-08-22 실전 확인)."""
    try:
        return _get_client(env), None
    except LiveKeysNotConfiguredError as exc:
        return None, (jsonify({"status": "error", "message": str(exc)}), 400)
    except Exception as exc:
        logger.exception("failed to connect futures client (env=%s)", env)
        return None, (jsonify({"status": "error", "message": f"거래소 연결 실패({env}): {exc}"}), 503)


def _resolve_env():
    """쿼리스트링 env를 읽는다. "demo"/"live"가 아니면 None을 돌려주고 호출자가 400으로 처리한다."""
    env = request.args.get("env", "demo")
    return env if env in ("demo", "live") else None


def _paths_for(env: str) -> dict:
    if env == "live":
        return {"journal": LIVE_JOURNAL_PATH, "state": LIVE_STATE_PATH,
                "last_trade": LIVE_LAST_TRADE_STATE_PATH, "heartbeat": HEARTBEAT_LIVE_PATH,
                "filter_stats": LIVE_FILTER_STATS_PATH, "excursion": LIVE_EXCURSION_PATH}
    return {"journal": JOURNAL_PATH, "state": STATE_PATH,
            "last_trade": LAST_TRADE_STATE_PATH, "heartbeat": HEARTBEAT_DEMO_PATH,
            "filter_stats": FILTER_STATS_PATH, "excursion": EXCURSION_PATH}


def _available_symbols(client):
    """FUTURES_SYMBOLS 중 지금 연결된 거래소 환경(데모 트레이딩 등)에 실제로 있는 것만.
    SOXL처럼 실거래 선물엔 있어도 데모 트레이딩엔 없는 심볼이 있어 — 그대로 조회하면 BadSymbol로
    /api/status 전체가 죽는다(futures_rule_bot.initialize()와 동일한 이유로 필터링)."""
    return [s for s in FUTURES_SYMBOLS if s in client.markets]


@app.route("/")
def index():
    # 페이지 로드는 항상 데모 클라이언트로만 렌더링한다 — 실계좌 클라이언트를 미리 만들지 않아서
    # 라이브 키가 아직 없어도 첫 로드가 절대 실패하지 않고, "새로고침하면 항상 데모로 시작"이라는
    # 요구사항을 서버 쪽에서도 보장한다(2026-08-22).
    client = _get_client("demo")
    return render_template("index.html", symbols=_available_symbols(client), leverage=LEVERAGE,
                            max_concurrent=MAX_CONCURRENT_POSITIONS)


@app.route("/api/status")
def api_status():
    """거래소 호출(잔고/포지션/브래킷 가격 조회)은 데모 서버 쪽 일시적 타임아웃(예: ccxt
    RequestTimeout -1007)으로 언제든 실패할 수 있다 — futures_rule_bot.run_once()가 심볼별로
    예외를 격리하는 것과 같은 이유로, 여기서도 한 번의 거래소 hiccup이 전체 500으로 번지지
    않게 격리한다. 잔고 조회가 실패하면 페이지 자체가 의미 없으므로 503으로 명확히 알리고,
    개별 심볼 조회 실패는 그 심볼만 상태 불명으로 표시하고 나머지는 정상 반환한다.

    ?env=demo|live로 어느 계좌를 볼지 고른다(기본 demo). env=live인데 실계좌 키가 없으면
    LiveKeysNotConfiguredError를 400으로 변환해서 프론트가 명확한 안내를 보여줄 수 있게 한다."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400

    client, err = _get_client_or_error(env)
    if err:
        return err

    try:
        balance = get_futures_balance(client)
        margin_equity = (balance.get("USDT") or {}).get("total") or 0.0
    except Exception as exc:
        logger.exception("failed to fetch futures balance (env=%s)", env)
        return jsonify({"status": "error", "message": f"거래소 응답 실패: {exc}"}), 503

    paths = _paths_for(env)
    available_symbols = _available_symbols(client)
    try:
        leverages = client.fetch_leverages(available_symbols)
    except Exception:
        logger.exception("failed to fetch per-symbol leverage — falling back to configured default for all symbols")
        leverages = {}

    symbols = {}
    open_count = 0
    unprotected = []
    excursions = excursion.read_all(path=paths["excursion"])
    for symbol in available_symbols:
        try:
            position = get_position(client, symbol)
            stop_loss_price, take_profit_price = (None, None)
            if position is not None:
                open_count += 1
                stop_loss_price, take_profit_price = get_bracket_prices(client, symbol)
                if stop_loss_price is None:
                    # 손절 주문이 없는 레버리지 포지션 — 지금까지는 카드에 "-" 한 글자로만 표시돼서
                    # 정상 상태와 구분이 안 됐다(2026-09-09). 화면 맨 위에 경고를 띄우려고 따로 모은다.
                    unprotected.append(symbol)
            symbols[symbol] = {
                "has_position": position is not None,
                "position": position,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "unprotected": position is not None and stop_loss_price is None,
                "excursion": excursions.get(symbol),
                "leverage": (leverages.get(symbol) or {}).get("longLeverage") or LEVERAGE,
            }
        except Exception as exc:
            logger.exception("symbol %s raised an error while building status — marking unavailable", symbol)
            symbols[symbol] = {
                "has_position": False,
                "position": None,
                "stop_loss_price": None,
                "take_profit_price": None,
                "error": str(exc),
            }

    all_entries = read_entries(path=paths["journal"])
    # execution(주문 원본 응답)은 진입 기록 하나가 6KB에 달하는데 화면은 이 중 아무것도 안 쓴다 —
    # 30줄이면 36KB이고 5초마다 나가므로, 빼면 같은 화면이 6KB로 줄어든다(2026-09-09).
    # 저널 파일에는 그대로 남으므로 사후 조사에는 영향이 없다.
    entries = [{k: v for k, v in e.items() if k != "execution"}
                for e in list(reversed(all_entries))[:30]]
    daily_pnl_pct = get_daily_pnl_pct(margin_equity, path=paths["state"])
    consecutive_losses = compute_consecutive_losses(all_entries)

    return jsonify({
        "env": env,
        "symbols": symbols,
        "leverage": LEVERAGE,
        "max_concurrent": MAX_CONCURRENT_POSITIONS,
        "margin_equity": margin_equity,
        "open_position_count": open_count,
        "unprotected_symbols": unprotected,
        "recent_entries": entries,
        "recent_issues": summarize_recent_issues(all_entries),
        "filter_stats": filter_stats.read_counts(path=paths["filter_stats"]),
        # 딕셔너리로 보내면 Flask가 키를 알파벳순으로 정렬해버려서 화면에 뜨는 순서가
        # 규칙이 적용되는 순서(레짐 -> 저변동 -> 같은봉 -> 드리프트)와 어긋난다. 리스트는 보존된다.
        "filter_events": [{"key": e, "label": filter_stats.EVENT_LABELS[e]}
                           for e in filter_stats.TRACKED_EVENTS],
        "daily_pnl_pct": daily_pnl_pct,
        "risk_limits": {
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "consecutive_losses": consecutive_losses,
        },
        # 전략 설정은 futures_rule_bot.current_strategy_config() 한 곳에서만 정의한다 —
        # 저널에 기록되는 설정 스냅샷과 화면에 뜨는 설정이 다르면 둘 중 뭘 믿을지 알 수 없다.
        "config": current_strategy_config(),
    })


@app.route("/api/performance")
def api_performance():
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    entries = read_entries(path=_paths_for(env)["journal"])
    # 달러 요약과 R 요약을 같이 내려보낸다 — 묻는 질문이 다르다. 달러는 "실제로 얼마 벌었나",
    # R은 "계획 대비 잘하고 있나"이고 백테스트 기대치와 비교 가능한 건 후자뿐이다.
    return jsonify({**summarize_performance(entries), "r": summarize_r_performance(entries)})


@app.route("/api/chart/<path:symbol>")
def api_chart(symbol):
    """대시보드의 종목 클릭 차트용 — 전략이 실제로 보는 것과 같은 타임프레임(RULE_TIMEFRAME)의
    최근 캔들을 그대로 돌려준다. env별로 마켓 목록이 달라서(예: SOXL은 실계좌에만 있음) 같은
    심볼이라도 env에 따라 404가 될 수 있다."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    client, err = _get_client_or_error(env)
    if err:
        return err
    if symbol not in client.markets:
        return jsonify({"status": "error", "message": "알 수 없는 심볼입니다."}), 404

    df = fetch_ohlcv_df(client, symbol, timeframe=RULE_TIMEFRAME, limit=100)
    candles = [
        {"time": row.timestamp.isoformat(), "open": row.open, "high": row.high,
         "low": row.low, "close": row.close}
        for row in df.itertuples()
    ]
    return jsonify({"symbol": symbol, "timeframe": RULE_TIMEFRAME, "candles": candles})


@app.route("/api/conditions")
def api_conditions():
    """종목별로 "지금 진입 조건에 얼마나 가까운지"를 돌려준다 — 신호가 안 뜨는 대부분의 시간에
    봇이 무엇을 기다리는 중인지 보여주기 위한 것.

    시세 조회는 공개 데이터라 계좌 클라이언트가 필요 없지만, **어떤 심볼을 볼지**는 env에 따라
    다르다(SOXL은 실계좌에만 있음) — 그래서 env로 심볼 목록만 고르고 캔들은 공개 클라이언트로
    받는다. 같은 응답을 텔레그램 봇도 쓰므로 계산 자체는 src/core/signal_status.py에 있다."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    client, err = _get_client_or_error(env)
    if err:
        return err

    payload = collect_conditions(_available_symbols(client))
    payload["env"] = env
    return jsonify(payload)


@app.route("/api/close/<path:symbol>", methods=["POST"])
def api_close(symbol):
    """대시보드에서 사용자가 직접 누르는 긴급 청산. 버튼 클릭 자체가 명시적 의사표시이므로
    confirm_live=True를 항상 넘긴다 — 자동매매 로직에 걸려있는 안전장치(confirm_live 요구)는
    "자동으로 실거래가 나가는 것"을 막기 위한 것이지, 사용자가 직접 누른 청산까지 막을 이유는 없다."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    client, err = _get_client_or_error(env)
    if err:
        return err

    position = get_position(client, symbol)
    if position is None:
        return jsonify({"status": "error", "message": "해당 종목에 보유 중인 포지션이 없습니다."}), 400

    try:
        result = close_position(client, symbol, position["side"], abs(position["contracts"]),
                                 env=env, confirm_live=True)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    paths = _paths_for(env)
    record_manual_close(client, symbol, journal_path=paths["journal"], last_trade_path=paths["last_trade"],
                         excursion_path=paths["excursion"])
    return jsonify(result)


@app.route("/api/risk/reset-streak", methods=["POST"])
def api_risk_reset_streak():
    """연속손실 카운트를 0으로 되돌린다 — 사용자가 명시적으로 버튼을 눌렀을 때만(자동으로는
    절대 안 됨). 서킷브레이커(연속손실 임계치)는 그대로 유지한 채, 이미 쌓인 손실 스트릭만
    지금 시점부터 다시 세게 한다(`reset_consecutive_losses` 참고 — 저널에 경계선 이벤트만
    남기고 실현손익 통계는 안 건드림)."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    paths = _paths_for(env)
    entry = reset_consecutive_losses(journal_path=paths["journal"])
    return jsonify({"status": "ok", "entry": entry})


@app.route("/api/bot/status")
def api_bot_status():
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    return jsonify({**bot_process.get_status(env), "heartbeat": read_heartbeat(path=_paths_for(env)["heartbeat"])})


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    if env == "live":
        # 라이브 키가 없거나 연결 자체가 안 되는 채로 봇 프로세스를 띄우면 매 사이클
        # 크래시-재시도만 반복하게 된다 — 여기서 미리 확인해서 깔끔한 오류로 막는 게 낫다.
        _, err = _get_client_or_error("live")
        if err:
            return err
    return jsonify(bot_process.start(env))


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    return jsonify(bot_process.stop(env))


@app.route("/api/public-ip")
def api_public_ip():
    """LIVE 탭에서 바이낸스 API 키의 IP 화이트리스트와 비교할 수 있도록 이 PC의 현재 공인 IP를
    보여준다. 조회 자체가 실패해도(외부 서비스 hiccup 등) null만 내려주고 200으로 응답 — 이
    부가 기능 하나 때문에 나머지 상태 조회까지 막을 이유는 없다."""
    return jsonify({"ip": get_public_ip()})


@app.route("/api/telegram/status")
def api_telegram_status():
    """텔레그램 알림/원격제어 프로세스(scripts/run_telegram_bot.py)의 실행 상태. env 개념이
    없는 단일 프로세스라 쿼리스트링이 필요 없다."""
    return jsonify(bot_process.get_status("telegram"))


@app.route("/api/telegram/start", methods=["POST"])
def api_telegram_start():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # 토큰/채팅ID 없이 프로세스를 띄우면 run_telegram_bot.py가 그 즉시 로그만 남기고
        # 종료해버려서(스스로 fail-fast) 대시보드엔 "시작은 됐는데 곧바로 꺼진" 것처럼 보인다 —
        # 여기서 미리 확인해서 원인을 바로 알려주는 게 낫다.
        return jsonify({"status": "error",
                         "message": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 .env에 설정되지 않았습니다."}), 400
    return jsonify(bot_process.start("telegram"))


@app.route("/api/telegram/stop", methods=["POST"])
def api_telegram_stop():
    return jsonify(bot_process.stop("telegram"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)
