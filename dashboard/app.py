import hashlib
import hmac
import logging
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, Response, jsonify, redirect, render_template, request

logger = logging.getLogger(__name__)

from src.core.config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DASHBOARD_TOKEN,
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
                                        get_futures_client, get_position, get_positions)
from src.data.public_ip import get_public_ip
from src.execution import bot_process, equity_log, excursion, filter_stats
from src.execution.strategy_versions import summarize_by_version
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

# 월 +1% 자산 수익률(복리) — docs/OBJECTIVE.md의 목표. 화면의 30일 수익률 옆에 기준선으로 쓴다.
MONTHLY_TARGET_RETURN = 0.01

# 차트에서 고를 수 있는 봉 주기. 아무 문자열이나 거래소로 넘기지 않도록 목록으로 막는다.
CHART_TIMEFRAMES = ("15m", "1h", "4h", "1d")


# ---------- 거래소 요청 캐시 (2026-09-28) ----------
# 바이낸스 요청 한도는 IP 단위(USDT-M 분당 가중치 2400)라 대시보드와 두 봇이 한 한도를 나눠 쓴다.
# 예전엔 화면 하나가 5초마다 잔고·포지션(종목마다)·시세를 전부 새로 물어서 LIVE 화면 하나가 분당
# 약 1300을 썼고, PC와 휴대폰에서 같이 열자 한도를 넘겨 IP가 차단됐다(418 -1003) — 차단 동안에는
# 봇도 캔들을 못 받아 "실행 중"인데 아무것도 못 했다. 그래서 거래소에 묻는 값은 여기서 TTL 동안
# 공유한다. 화면이 몇 개든 거래소 요청 수는 같다.
STATUS_TTL = 5
LEVERAGE_TTL = 600
TICKER_TTL = 30
CONDITIONS_TTL = 60   # 신호는 마감 봉으로만 판정하므로 1분 안에 달라질 일이 거의 없다
CHART_TTL = 20

_cache: dict = {}
_cache_guard = threading.Lock()
_cache_locks: dict = {}


def _cached(key, ttl: float, compute):
    """key의 값을 ttl초 동안 재사용한다. 계산 중에 같은 key로 들어온 요청은 기다렸다가 같은 결과를
    받는다(동시에 두 번 거래소에 묻지 않음). compute가 예외를 내면 캐시하지 않고 그대로 올린다."""
    with _cache_guard:
        lock = _cache_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = compute()
        _cache[key] = (time.monotonic(), value)
        return value


def _invalidate(prefix: str, env: str) -> None:
    """청산·리셋처럼 사용자가 상태를 바꾼 직후엔 캐시된 옛 화면을 보여주지 않는다."""
    _cache.pop((prefix, env), None)


# ---------- 접속 제어 (2026-09-28) ----------
# 휴대폰에서 보려고 대시보드를 PC 밖(0.0.0.0)에 열면, 같은 와이파이의 누구나 긴급청산이나
# 실계좌 봇 시작을 누를 수 있게 된다. 그래서 PC 밖에서 오는 요청은 토큰으로 로그인해야 한다.
# 이 PC 자신에서 여는 요청(127.0.0.1)은 지금처럼 바로 통과한다 — 기존 사용 방식을 안 바꾸려고.
_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
_AUTH_COOKIE = "auto2_dash"
_AUTH_COOKIE_DAYS = 90
_PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/icon.svg"}
# 같은 PC의 프록시(예: tailscale serve)를 거쳐 들어온 요청은 주소가 127.0.0.1로 보이지만 실제로는
# 밖에서 온 것이다 — 이런 헤더가 붙어 있으면 로컬로 믿지 않는다.
_PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "Tailscale-User-Login")


def _auth_digest(token: str) -> str:
    """쿠키에는 토큰 원문 대신 이 값을 넣는다 — 쿠키가 새어도 토큰 자체는 드러나지 않게."""
    return hmac.new(token.encode("utf-8"), b"auto2-dashboard", hashlib.sha256).hexdigest()


def _is_local_request() -> bool:
    if request.remote_addr not in _LOOPBACK_ADDRS:
        return False
    return not any(request.headers.get(h) for h in _PROXY_HEADERS)


def _is_authorized() -> bool:
    if _is_local_request():
        return True
    if not DASHBOARD_TOKEN:
        return False
    return hmac.compare_digest(request.cookies.get(_AUTH_COOKIE, ""), _auth_digest(DASHBOARD_TOKEN))


def check_bind_is_safe(host: str, token: str) -> None:
    """PC 밖에서 접속 가능한 주소로 열면서 토큰이 없으면 시작을 거부한다(조용히 열어두지 않음)."""
    if host not in _LOCAL_BIND_HOSTS and not token:
        raise SystemExit(
            f"DASHBOARD_HOST={host}로 열려면 .env에 DASHBOARD_TOKEN(접속 비밀번호)을 설정해야 합니다 — "
            "대시보드에 긴급청산·실계좌 봇 시작 버튼이 있어서 비밀번호 없이 네트워크에 열 수 없습니다.")


@app.before_request
def _require_auth():
    if request.path in _PUBLIC_PATHS or _is_authorized():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        submitted = (request.form.get("token") or "").strip()
        if DASHBOARD_TOKEN and hmac.compare_digest(submitted.encode("utf-8"), DASHBOARD_TOKEN.encode("utf-8")):
            resp = redirect("/")
            resp.set_cookie(_AUTH_COOKIE, _auth_digest(DASHBOARD_TOKEN), max_age=_AUTH_COOKIE_DAYS * 86400,
                            httponly=True, samesite="Strict")
            return resp
        time.sleep(1)  # 무차별 대입을 느리게 — 사람이 한 번 틀리는 데는 지장 없다
        error = "비밀번호가 맞지 않습니다." if DASHBOARD_TOKEN else "서버에 DASHBOARD_TOKEN이 설정되지 않았습니다."
    return render_template("login.html", error=error)


@app.route("/manifest.webmanifest")
def manifest():
    """휴대폰 '홈 화면에 추가'용 — 앱처럼 주소창 없이 열린다."""
    return jsonify({
        "name": "auto2 대시보드", "short_name": "auto2", "start_url": "/", "display": "standalone",
        "background_color": "#0c0e12", "theme_color": "#0c0e12",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    })


@app.route("/icon.svg")
def icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<rect width="64" height="64" rx="14" fill="#0c0e12"/>'
           '<path d="M12 44 L26 30 L34 38 L52 18" fill="none" stroke="#2ebd85" stroke-width="6" '
           'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return Response(svg, mimetype="image/svg+xml")


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
        # 화면이 여러 개(PC·휴대폰) 열려 있어도 거래소에는 STATUS_TTL마다 한 번만 묻는다
        return jsonify(_cached(("status", env), STATUS_TTL, lambda: _build_status(env, client)))
    except _ExchangeUnavailable as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503


class _ExchangeUnavailable(Exception):
    """잔고 조회처럼 화면 전체가 의미 없어지는 실패 — 캐시하지 않고 503으로 알린다."""


def _leverages(env: str, client, symbols: list[str]) -> dict:
    """종목별 레버리지는 봇이 시작할 때 한 번 정하고 안 바뀐다 — 10분에 한 번만 묻는다."""
    def fetch():
        try:
            return client.fetch_leverages(symbols)
        except Exception:
            logger.exception("failed to fetch per-symbol leverage — falling back to configured default")
            return {}
    return _cached(("leverages", env), LEVERAGE_TTL, fetch)


def _build_status(env: str, client) -> dict:
    try:
        balance = get_futures_balance(client)
        margin_equity = (balance.get("USDT") or {}).get("total") or 0.0
    except Exception as exc:
        logger.exception("failed to fetch futures balance (env=%s)", env)
        raise _ExchangeUnavailable(f"거래소 응답 실패: {exc}") from exc

    paths = _paths_for(env)
    available_symbols = _available_symbols(client)
    leverages = _leverages(env, client, available_symbols)
    # 전 종목 포지션을 한 번의 요청으로(가중치 5). 예전처럼 종목마다 부르면 12배였고, 5초 주기와
    # 여러 화면이 겹쳐 바이낸스가 IP를 차단했다(2026-09-28, 418 -1003).
    try:
        positions, positions_error = get_positions(client, available_symbols), None
    except Exception as exc:
        logger.exception("failed to fetch positions (env=%s) — marking all symbols unavailable", env)
        positions, positions_error = {}, exc

    symbols = {}
    open_count = 0
    unprotected = []
    excursions = excursion.read_all(path=paths["excursion"])
    for symbol in available_symbols:
        try:
            if positions_error is not None:
                raise positions_error
            position = positions.get(symbol)
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

    return {
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
    }


@app.route("/api/performance")
def api_performance():
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    entries = read_entries(path=_paths_for(env)["journal"])
    # 달러 요약과 R 요약을 같이 내려보낸다 — 묻는 질문이 다르다. 달러는 "실제로 얼마 벌었나",
    # R은 "계획 대비 잘하고 있나"이고 백테스트 기대치와 비교 가능한 건 후자뿐이다.
    # 전략 버전별 성과도 같이 — 규칙이 바뀐 전후의 거래가 한 숫자로 섞이면 지금 규칙이 통하는지
    # 판단할 수 없다(execution.strategy_versions).
    return jsonify({**summarize_performance(entries), "r": summarize_r_performance(entries),
                    "versions": summarize_by_version(entries)})


@app.route("/api/chart/<path:symbol>")
def api_chart(symbol):
    """대시보드 차트용 캔들. 기본 주기는 전략이 실제로 보는 RULE_TIMEFRAME이고 `?tf=`로
    CHART_TIMEFRAMES 중 하나를 고를 수 있다. env별로 마켓 목록이 달라서(예: SOXL은 실계좌에만
    있음) 같은 심볼이라도 env에 따라 404가 될 수 있다.

    마지막 봉은 아직 마감 안 된 봉이다 — 차트는 거래소 화면처럼 그대로 보여주고(`live_last`로
    표시), 신호 판정에는 이 응답을 쓰지 않는다."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    timeframe = request.args.get("tf", RULE_TIMEFRAME)
    if timeframe not in CHART_TIMEFRAMES and timeframe != RULE_TIMEFRAME:
        return jsonify({"status": "error", "message": "지원하지 않는 봉 주기입니다."}), 400
    client, err = _get_client_or_error(env)
    if err:
        return err
    if symbol not in client.markets:
        return jsonify({"status": "error", "message": "알 수 없는 심볼입니다."}), 404

    try:
        df = _cached(("chart", env, symbol, timeframe), CHART_TTL,
                     lambda: fetch_ohlcv_df(client, symbol, timeframe=timeframe, limit=160))
    except Exception as exc:
        logger.exception("failed to fetch chart candles for %s (env=%s)", symbol, env)
        return jsonify({"status": "error", "message": f"캔들 조회 실패: {exc}"}), 503
    # 시각은 epoch ms로 보낸다 — timestamp는 tz 없는 UTC라 isoformat으로 보내면 브라우저가
    # 로컬 시각으로 읽어서 9시간 어긋난다.
    candles = [
        {"t": int(row.timestamp.value // 1_000_000), "o": row.open, "h": row.high,
         "l": row.low, "c": row.close, "v": row.volume}
        for row in df.itertuples()
    ]
    return jsonify({"symbol": symbol, "timeframe": timeframe, "strategy_timeframe": RULE_TIMEFRAME,
                    "timeframes": list(CHART_TIMEFRAMES), "candles": candles, "live_last": True})


@app.route("/api/tickers")
def api_tickers():
    """감시 종목의 현재가·24시간 등락 — 거래소 화면의 종목 목록처럼 보여주기 위한 것.
    한 번의 호출로 전 종목을 받는다. 실패해도 화면의 나머지는 살아 있어야 하므로 빈 값으로 200."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    client, err = _get_client_or_error(env)
    if err:
        return err
    symbols = _available_symbols(client)
    # 전 종목 24시간 시세는 가중치 40짜리 요청이다 — TICKER_TTL 동안 모든 화면이 공유한다.
    # 실패는 캐시하지 않는다(다음 요청이 다시 시도).
    try:
        raw = _cached(("tickers", env), TICKER_TTL, lambda: client.fetch_tickers(symbols))
    except Exception:
        logger.exception("failed to fetch tickers (env=%s)", env)
        raw = {}
    tickers = {
        symbol: {"last": t.get("last"), "change_pct": t.get("percentage"), "high": t.get("high"),
                 "low": t.get("low"), "quote_volume": t.get("quoteVolume")}
        for symbol, t in raw.items() if symbol in symbols
    }
    return jsonify({"env": env, "tickers": tickers})


@app.route("/api/equity")
def api_equity():
    """날짜별 마진 자산과 최근 30일 수익률 — 목표(월 +1%)와 직접 비교할 수 있는 유일한 값
    (R이나 실현손익 합계로는 못 낸다: equity_log 모듈 설명 참고)."""
    env = _resolve_env()
    if env is None:
        return jsonify({"status": "error", "message": "알 수 없는 env입니다."}), 400
    # 경로는 호출 시점에 모듈에서 읽는다 — 테스트(conftest)가 모듈 전역을 갈아끼워 격리한다.
    path = equity_log.LIVE_DEFAULT_PATH if env == "live" else equity_log.DEFAULT_PATH
    days = [{"day": day, "start": rec.get("start"), "end": rec.get("end")}
            for day, rec in equity_log.read_days(path=path).items() if isinstance(rec, dict)]
    return jsonify({"env": env, "days": days, "month": equity_log.period_return(path=path, days=30),
                    "target_monthly_return": MONTHLY_TARGET_RETURN})


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

    symbols = _available_symbols(client)
    # 종목마다 캔들 400여 개를 받는 무거운 계산이다 — 동시에 여러 화면이 불러도 한 번만 돈다.
    payload = dict(_cached(("conditions", env), CONDITIONS_TTL,
                           lambda: collect_conditions(symbols)))
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
    _invalidate("status", env)
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
    _invalidate("status", env)
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
    check_bind_is_safe(DASHBOARD_HOST, DASHBOARD_TOKEN)
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, threaded=True)
