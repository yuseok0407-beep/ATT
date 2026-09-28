from types import SimpleNamespace

import pytest

from dashboard import app as dashboard_app
from src.data.futures_exchange import LiveKeysNotConfiguredError
from src.execution import excursion, filter_stats


class _FakeClient:
    markets = {"BTC/USDT:USDT": {}, "ETH/USDT:USDT": {}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard_app, "_get_client", lambda env="demo": _FakeClient())
    monkeypatch.setattr(dashboard_app, "read_entries", lambda path=None: [])
    monkeypatch.setattr(dashboard_app, "get_daily_pnl_pct", lambda equity, path=None: 0.0)
    monkeypatch.setattr(dashboard_app, "compute_consecutive_losses", lambda entries: 0)
    dashboard_app.app.config["TESTING"] = True
    return dashboard_app.app.test_client()


@pytest.fixture(autouse=True)
def _clear_exchange_cache():
    """거래소 응답 캐시는 모듈 전역이라 테스트끼리 섞이지 않게 매번 비운다."""
    dashboard_app._cache.clear()
    yield
    dashboard_app._cache.clear()


def _positions(mapping=None):
    mapping = mapping or {}
    return lambda client, symbols: {s: mapping.get(s) for s in symbols}


def test_api_status_returns_503_when_balance_fetch_fails(client, monkeypatch):
    """실전 재현: 데모 거래소 백엔드가 타임아웃(ccxt RequestTimeout -1007)나면 잔고 조회부터
    실패한다 — 예전엔 처리 안 된 예외가 그대로 터져 Flask 기본 500 HTML 페이지가 나갔다."""
    def _raise(client):
        raise TimeoutError("Timeout waiting for response from backend server")

    monkeypatch.setattr(dashboard_app, "get_futures_balance", _raise)

    resp = client.get("/api/status")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "error"
    assert "거래소 응답 실패" in body["message"]


def test_api_status_isolates_one_symbols_exchange_error_from_the_rest(client, monkeypatch):
    """BTC의 손절/익절 주문 조회가 타임아웃나도 ETH 상태는 정상적으로 돌아와야 한다 —
    futures_rule_bot.run_once()의 심볼별 예외 격리와 동일한 이유."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", _positions({"BTC/USDT:USDT": _position()}))

    def _brackets(client, symbol):
        raise TimeoutError("backend timeout")

    monkeypatch.setattr(dashboard_app, "get_bracket_prices", _brackets)

    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "error" in body["symbols"]["BTC/USDT:USDT"]
    assert body["symbols"]["BTC/USDT:USDT"]["has_position"] is False
    assert "error" not in body["symbols"]["ETH/USDT:USDT"]
    assert body["margin_equity"] == 1000.0


def test_api_status_marks_symbols_unavailable_when_the_positions_call_fails(client, monkeypatch):
    """포지션은 한 번의 요청으로 받는다 — 그게 실패하면 잔고는 보여주되 종목은 전부 상태 불명."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})

    def _raise(client, symbols):
        raise TimeoutError("backend timeout")

    monkeypatch.setattr(dashboard_app, "get_positions", _raise)

    body = client.get("/api/status").get_json()

    assert all("error" in s for s in body["symbols"].values())
    assert body["margin_equity"] == 1000.0


def test_api_status_asks_the_exchange_once_for_many_viewers(client, monkeypatch):
    """PC와 휴대폰이 동시에 열어도 거래소 요청은 캐시 TTL마다 한 번 — 바이낸스 한도는 IP 단위라
    화면 수만큼 요청이 늘면 IP가 차단된다(2026-09-28, 418 -1003)."""
    calls = {"balance": 0, "positions": 0}

    def _balance(client):
        calls["balance"] += 1
        return {"USDT": {"total": 1000.0}}

    def _get_positions(client, symbols):
        calls["positions"] += 1
        return {s: None for s in symbols}

    monkeypatch.setattr(dashboard_app, "get_futures_balance", _balance)
    monkeypatch.setattr(dashboard_app, "get_positions", _get_positions)

    for _ in range(3):
        assert client.get("/api/status").status_code == 200

    assert calls == {"balance": 1, "positions": 1}


def test_a_failed_status_is_not_cached(client, monkeypatch):
    state = {"fail": True}

    def _balance(client):
        if state["fail"]:
            raise TimeoutError("backend timeout")
        return {"USDT": {"total": 1000.0}}

    monkeypatch.setattr(dashboard_app, "get_futures_balance", _balance)
    monkeypatch.setattr(dashboard_app, "get_positions", _positions())

    assert client.get("/api/status").status_code == 503
    state["fail"] = False
    assert client.get("/api/status").status_code == 200


def test_api_status_defaults_to_demo_env(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", lambda client, symbols: {s: None for s in symbols})

    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.get_json()["env"] == "demo"


def test_api_status_rejects_invalid_env_query_param(client):
    resp = client.get("/api/status?env=bogus")
    assert resp.status_code == 400
    assert "env" in resp.get_json()["message"]


def test_api_status_env_live_without_keys_returns_400(monkeypatch):
    """실계좌 키가 .env에 없는데 대시보드에서 LIVE 탭을 열면(또는 라이브 키가 잘못 설정된 채로
    새로고침하면) 서버가 죽지 않고 명확한 400을 돌려줘야 한다(2026-08-22)."""
    def _get_client(env="demo"):
        if env == "live":
            raise LiveKeysNotConfiguredError("BINANCE_FUTURES_LIVE_API_KEY/... 미설정")
        return _FakeClient()

    monkeypatch.setattr(dashboard_app, "_get_client", _get_client)
    dashboard_app.app.config["TESTING"] = True
    test_client = dashboard_app.app.test_client()

    resp = test_client.get("/api/status?env=live")
    assert resp.status_code == 400
    assert "LIVE" in resp.get_json()["message"] or "BINANCE" in resp.get_json()["message"]


def test_api_risk_reset_streak_rejects_invalid_env_query_param(client):
    resp = client.post("/api/risk/reset-streak?env=bogus")
    assert resp.status_code == 400


def test_api_risk_reset_streak_uses_demo_journal_by_default(client, monkeypatch):
    captured = {}

    def _fake_reset(journal_path=None):
        captured["journal_path"] = journal_path
        return {"event": "consecutive_loss_reset"}

    monkeypatch.setattr(dashboard_app, "reset_consecutive_losses", _fake_reset)

    resp = client.post("/api/risk/reset-streak")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert captured["journal_path"] == dashboard_app.JOURNAL_PATH


def test_api_risk_reset_streak_uses_live_journal_when_env_live(client, monkeypatch):
    captured = {}

    def _fake_reset(journal_path=None):
        captured["journal_path"] = journal_path
        return {"event": "consecutive_loss_reset"}

    monkeypatch.setattr(dashboard_app, "reset_consecutive_losses", _fake_reset)

    resp = client.post("/api/risk/reset-streak?env=live")
    assert resp.status_code == 200
    assert captured["journal_path"] == dashboard_app.LIVE_JOURNAL_PATH


def test_api_status_demo_and_live_use_different_journal_and_state_paths(client, monkeypatch):
    """데모/실계좌가 서로 다른 저널/상태 파일을 읽는지 — 이 구조 전체의 핵심 보증이라 직접 확인."""
    captured = {"journal_paths": [], "state_paths": []}

    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", lambda client, symbols: {s: None for s in symbols})
    monkeypatch.setattr(dashboard_app, "read_entries",
                         lambda path=None: captured["journal_paths"].append(path) or [])
    monkeypatch.setattr(dashboard_app, "get_daily_pnl_pct",
                         lambda equity, path=None: captured["state_paths"].append(path) or 0.0)

    client.get("/api/status?env=demo")
    client.get("/api/status?env=live")

    assert captured["journal_paths"][0] != captured["journal_paths"][1]
    assert captured["state_paths"][0] != captured["state_paths"][1]
    assert captured["journal_paths"][0] == dashboard_app.JOURNAL_PATH
    assert captured["journal_paths"][1] == dashboard_app.LIVE_JOURNAL_PATH


def test_api_public_ip_returns_get_public_ip_result(client, monkeypatch):
    """실제 캐싱/조회 로직은 src/data/public_ip.py로 옮겨서 tests/test_public_ip.py가 따로
    검증한다 — 여기서는 라우트가 그 함수를 그대로 호출해서 응답에 담는지만 확인."""
    monkeypatch.setattr(dashboard_app, "get_public_ip", lambda: "1.2.3.4")

    resp = client.get("/api/public-ip")
    assert resp.status_code == 200
    assert resp.get_json()["ip"] == "1.2.3.4"


def test_api_telegram_status_calls_bot_process_with_telegram_key(client, monkeypatch):
    monkeypatch.setattr(dashboard_app.bot_process, "get_status",
                         lambda key: {"running": True, "pid": 123, "started_at": 1.0} if key == "telegram" else {})

    resp = client.get("/api/telegram/status")
    assert resp.status_code == 200
    assert resp.get_json() == {"running": True, "pid": 123, "started_at": 1.0}


def test_api_telegram_start_returns_400_when_token_or_chat_id_missing(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(dashboard_app, "TELEGRAM_CHAT_ID", "")
    mock_start = None

    def _start(key):
        nonlocal mock_start
        mock_start = key

    monkeypatch.setattr(dashboard_app.bot_process, "start", _start)

    resp = client.post("/api/telegram/start")
    assert resp.status_code == 400
    assert "TELEGRAM" in resp.get_json()["message"]
    assert mock_start is None  # 토큰 없으면 프로세스를 아예 안 띄운다


def test_api_telegram_start_calls_bot_process_when_configured(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(dashboard_app, "TELEGRAM_CHAT_ID", "555")
    monkeypatch.setattr(dashboard_app.bot_process, "start",
                         lambda key: {"running": True, "pid": 999, "started_at": 1.0} if key == "telegram" else {})

    resp = client.post("/api/telegram/start")
    assert resp.status_code == 200
    assert resp.get_json()["pid"] == 999


def test_api_telegram_stop_calls_bot_process_with_telegram_key(client, monkeypatch):
    monkeypatch.setattr(dashboard_app.bot_process, "stop",
                         lambda key: {"running": False, "pid": None, "started_at": None} if key == "telegram" else {"BUG": True})

    resp = client.post("/api/telegram/stop")
    assert resp.status_code == 200
    assert resp.get_json() == {"running": False, "pid": None, "started_at": None}


def test_api_conditions_returns_rows_for_the_env_symbols(client, monkeypatch):
    """조건 근접도는 계좌 마켓에 있는 심볼만 대상으로 한다(SOXL처럼 env마다 다름).
    계산 자체는 core.signal_status가 하므로 여기서는 라우트 배선만 확인한다."""
    captured = {}

    def _collect(symbols):
        captured["symbols"] = list(symbols)
        return {"timeframe": "1h", "adx_threshold": 30.0, "sma_period": 10,
                "regime_sma_period": 400, "cross_near_pct": 1.0,
                "symbols": [{"symbol": "BTC/USDT:USDT", "proximity": 0.5}],
                "errors": [], "generated_at": 0.0}

    monkeypatch.setattr(dashboard_app, "collect_conditions", _collect)
    resp = client.get("/api/conditions?env=demo")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["env"] == "demo"
    assert captured["symbols"] == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert body["symbols"][0]["symbol"] == "BTC/USDT:USDT"


def test_api_conditions_rejects_an_unknown_env(client):
    assert client.get("/api/conditions?env=bogus").status_code == 400



# ---------- 무보호 포지션 / 거부 요약 / 차단 통계 (2026-09-09) ----------

def _position(side="long"):
    return {"side": side, "entryPrice": 100.0, "markPrice": 101.0, "contracts": 1.0}


def test_api_status_flags_a_position_without_a_stop_order(client, monkeypatch):
    """손절 없는 레버리지 포지션은 화면 맨 위 경보로 올라가야 한다 — 지금까지는 카드 안
    손절가 칸의 "-" 한 글자로만 표시돼서 정상 상태와 구분이 안 됐다."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", _positions({"BTC/USDT:USDT": _position()}))
    monkeypatch.setattr(dashboard_app, "get_bracket_prices", lambda client, symbol: (None, 102.0))

    body = client.get("/api/status").get_json()

    assert body["unprotected_symbols"] == ["BTC/USDT:USDT"]
    assert body["symbols"]["BTC/USDT:USDT"]["unprotected"] is True
    assert body["symbols"]["ETH/USDT:USDT"]["unprotected"] is False


def test_api_status_does_not_flag_a_protected_position(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", _positions({"BTC/USDT:USDT": _position()}))
    monkeypatch.setattr(dashboard_app, "get_bracket_prices", lambda client, symbol: (99.0, 102.0))

    body = client.get("/api/status").get_json()

    assert body["unprotected_symbols"] == []
    assert body["symbols"]["BTC/USDT:USDT"]["unprotected"] is False


def test_api_status_includes_recent_exchange_rejections(client, monkeypatch):
    """저널엔 남지만 알림에서도 "최근 내역" 30줄에서도 밀려나 안 보이던 것들."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", lambda client, symbols: {s: None for s in symbols})
    monkeypatch.setattr(dashboard_app, "read_entries", lambda path=None: [
        {"timestamp": recent, "event": "rejected_exchange_error", "symbol": "TSLA/USDT:USDT",
         "reason": 'binance {"code":-2027,"msg":"Exceeded the maximum allowable position"}'},
    ])

    body = client.get("/api/status").get_json()

    assert body["recent_issues"]["rejections"][0]["code"] == "-2027"
    assert body["recent_issues"]["rejections"][0]["symbols"] == ["TSLA"]


def test_api_status_includes_todays_filter_blocks(client, monkeypatch, tmp_path):
    """저널에 안 남는 차단 사유(레짐숏/저변동/…)를 볼 수 있는 유일한 창구."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", lambda client, symbols: {s: None for s in symbols})
    stats_path = str(tmp_path / "stats.json")
    filter_stats.record("skipped_low_volatility", "BTC/USDT:USDT", "bar1",
                        path=stats_path, today="2026-09-09")
    monkeypatch.setattr(dashboard_app, "FILTER_STATS_PATH", stats_path)

    body = client.get("/api/status").get_json()

    assert body["filter_stats"]["2026-09-09"] == {"skipped_low_volatility": 1}
    # 규칙 적용 순서 그대로 내려와야 한다(딕셔너리로 보내면 Flask가 알파벳순으로 섞는다)
    assert [e["key"] for e in body["filter_events"]] == list(filter_stats.TRACKED_EVENTS)
    assert body["filter_events"][1]["label"] == "저변동"


def test_api_status_includes_the_excursion_of_an_open_position(client, monkeypatch, tmp_path):
    """"지금 +0.4R인데 아까 +1.6R까지 갔었다"를 카드에 그리기 위한 데이터."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", _positions({"BTC/USDT:USDT": _position()}))
    monkeypatch.setattr(dashboard_app, "get_bracket_prices", lambda client, symbol: (99.0, 102.0))
    excursion_path = str(tmp_path / "exc.json")
    excursion.update("BTC/USDT:USDT", entry_price=100.0, stop_loss_price=99.0,
                     mark_price=101.6, side="long", path=excursion_path)
    monkeypatch.setattr(dashboard_app, "EXCURSION_PATH", excursion_path)

    body = client.get("/api/status").get_json()

    assert body["symbols"]["BTC/USDT:USDT"]["excursion"]["max_favorable_r"] == pytest.approx(1.6)
    assert body["symbols"]["ETH/USDT:USDT"]["excursion"] is None


def test_api_status_strips_the_raw_order_blob_from_recent_entries(client, monkeypatch):
    """진입 기록 하나의 execution(주문 원본 응답)이 6KB인데 화면은 이 중 아무것도 안 쓴다 —
    30줄이면 36KB이고 5초마다 나간다. 저널 파일에는 그대로 남는다."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", lambda client, symbols: {s: None for s in symbols})
    monkeypatch.setattr(dashboard_app, "read_entries", lambda path=None: [
        {"timestamp": "2026-09-09T01:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "entry_price": 100.0, "execution": {"entry_order": {"info": {"x": "y" * 1000}}}},
    ])

    body = client.get("/api/status").get_json()

    entry = body["recent_entries"][0]
    assert "execution" not in entry
    assert entry["entry_price"] == 100.0  # 화면이 쓰는 필드는 그대로


def test_api_performance_includes_r_metrics(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "read_entries", lambda path=None: [
        {"timestamp": "2026-09-08T01:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "signal": "LONG", "entry_price": 100.0, "stop_loss_price": 99.0},
        {"timestamp": "2026-09-08T02:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "take_profit", "entry_price": 100.0, "exit_price": 102.0, "realized_pnl": 20.0},
    ])

    body = client.get("/api/performance").get_json()

    assert body["num_trades"] == 1              # 기존 달러 요약은 그대로
    assert body["r"]["total_r"] == pytest.approx(2.0)
    assert len(body["r"]["equity_curve"]) == 1


def test_api_status_config_comes_from_the_single_strategy_config(client, monkeypatch):
    """화면의 설정과 저널에 기록되는 설정 스냅샷이 갈라지면 둘 중 뭘 믿을지 알 수 없다 —
    둘 다 futures_rule_bot.current_strategy_config() 하나에서 나온다."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_positions", lambda client, symbols: {s: None for s in symbols})

    config = client.get("/api/status").get_json()["config"]

    assert config == dashboard_app.current_strategy_config()
    # 진입 판단에 실제로 쓰이는 필터들이 화면에 빠져 있으면 "왜 진입을 안 하지"를 화면만 보고 알 수 없다
    assert "regime_sma_period" in config and "min_atr_to_stop_ratio" in config


# ---------- 휴대폰 접속용 접근 제어 (2026-09-28) ----------

_REMOTE = {"REMOTE_ADDR": "100.64.0.7"}  # 같은 와이파이/Tailscale에서 온 요청


def test_local_requests_do_not_need_a_token(client, monkeypatch):
    """이 PC에서 여는 기존 사용 방식은 그대로여야 한다(테스트 클라이언트 기본 주소가 127.0.0.1)."""
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "secret")
    monkeypatch.setattr(dashboard_app, "get_public_ip", lambda: "1.2.3.4")
    assert client.get("/api/public-ip").status_code == 200


def test_remote_requests_without_login_are_refused(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "secret")

    api = client.get("/api/status", environ_base=_REMOTE)
    page = client.get("/", environ_base=_REMOTE)
    close = client.post("/api/close/BTC/USDT:USDT", environ_base=_REMOTE)

    assert api.status_code == 401
    assert page.status_code == 302 and page.headers["Location"].endswith("/login")
    assert close.status_code == 401  # 긴급청산 같은 조작이 인증 없이 통과하면 안 된다


def test_remote_requests_are_refused_when_no_token_is_configured(client, monkeypatch):
    """토큰이 비어 있으면 빈 쿠키로도 통과하지 못해야 한다(빈 문자열끼리 비교해 통과하는 사고 방지)."""
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "")
    client.set_cookie(dashboard_app._AUTH_COOKIE, "")
    assert client.get("/api/public-ip", environ_base=_REMOTE).status_code == 401


def test_login_with_the_right_token_lets_remote_requests_through(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "secret")
    monkeypatch.setattr(dashboard_app, "get_public_ip", lambda: "1.2.3.4")

    resp = client.post("/login", data={"token": "secret"}, environ_base=_REMOTE)

    assert resp.status_code == 302
    cookie = resp.headers["Set-Cookie"]
    assert "secret" not in cookie.split(";")[0]  # 쿠키에 토큰 원문을 넣지 않는다
    assert "HttpOnly" in cookie
    assert client.get("/api/public-ip", environ_base=_REMOTE).status_code == 200


def test_login_with_a_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "secret")
    monkeypatch.setattr(dashboard_app.time, "sleep", lambda s: None)

    resp = client.post("/login", data={"token": "guess"}, environ_base=_REMOTE)

    assert resp.status_code == 200
    assert "Set-Cookie" not in resp.headers
    assert client.get("/api/public-ip", environ_base=_REMOTE).status_code == 401


def test_a_proxied_request_from_loopback_is_not_trusted_as_local(client, monkeypatch):
    """같은 PC의 프록시(tailscale serve 등)를 거친 요청은 주소가 127.0.0.1이어도 밖에서 온 것이다."""
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "secret")
    resp = client.get("/api/public-ip", headers={"X-Forwarded-For": "100.64.0.7"})
    assert resp.status_code == 401


def test_login_page_and_manifest_are_reachable_without_login(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DASHBOARD_TOKEN", "secret")
    assert client.get("/login", environ_base=_REMOTE).status_code == 200
    assert client.get("/manifest.webmanifest", environ_base=_REMOTE).status_code == 200


def test_binding_outside_this_pc_requires_a_token():
    with pytest.raises(SystemExit):
        dashboard_app.check_bind_is_safe("0.0.0.0", "")
    dashboard_app.check_bind_is_safe("0.0.0.0", "secret")
    dashboard_app.check_bind_is_safe("127.0.0.1", "")


# ---------- 차트 · 시세 · 자산 (2026-09-28) ----------

def test_api_chart_sends_epoch_ms_and_volume(client, monkeypatch):
    """timestamp는 tz 없는 UTC라 isoformat으로 보내면 브라우저가 로컬 시각으로 읽어 9시간 어긋난다."""
    import pandas as pd
    captured = {}

    def _fetch(client, symbol, timeframe="1h", limit=100):
        captured["timeframe"] = timeframe
        return pd.DataFrame({"timestamp": pd.to_datetime([1_700_000_000_000], unit="ms"),
                             "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]})

    monkeypatch.setattr(dashboard_app, "fetch_ohlcv_df", _fetch)

    body = client.get("/api/chart/BTC/USDT:USDT?tf=4h").get_json()

    assert captured["timeframe"] == "4h"
    assert body["candles"][0] == {"t": 1_700_000_000_000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0}


def test_api_chart_rejects_an_unlisted_timeframe(client):
    assert client.get("/api/chart/BTC/USDT:USDT?tf=3m").status_code == 400


def test_api_tickers_returns_only_the_watched_symbols(client, monkeypatch):
    class _TickerClient(_FakeClient):
        def fetch_tickers(self, symbols):
            return {"BTC/USDT:USDT": {"last": 100.0, "percentage": 1.5, "high": 110.0, "low": 90.0,
                                      "quoteVolume": 5e6},
                    "DOGE/USDT:USDT": {"last": 0.1}}

    monkeypatch.setattr(dashboard_app, "_get_client", lambda env="demo": _TickerClient())

    body = client.get("/api/tickers").get_json()

    assert set(body["tickers"]) == {"BTC/USDT:USDT"}
    assert body["tickers"]["BTC/USDT:USDT"]["change_pct"] == 1.5


def test_api_tickers_survives_an_exchange_error(client, monkeypatch):
    class _BrokenClient(_FakeClient):
        def fetch_tickers(self, symbols):
            raise TimeoutError("backend timeout")

    monkeypatch.setattr(dashboard_app, "_get_client", lambda env="demo": _BrokenClient())

    resp = client.get("/api/tickers")
    assert resp.status_code == 200
    assert resp.get_json()["tickers"] == {}


def test_api_equity_reads_the_env_specific_log(client):
    from datetime import datetime
    from src.execution import equity_log
    equity_log.record(1000.0, path=equity_log.DEFAULT_PATH, now=datetime(2026, 9, 1, 9).astimezone())
    equity_log.record(1010.0, path=equity_log.DEFAULT_PATH, now=datetime(2026, 9, 2, 9).astimezone())
    equity_log.record(500.0, path=equity_log.LIVE_DEFAULT_PATH, now=datetime(2026, 9, 2, 9).astimezone())

    demo = client.get("/api/equity").get_json()
    live = client.get("/api/equity?env=live").get_json()

    assert [d["day"] for d in demo["days"]] == ["2026-09-01", "2026-09-02"]
    assert demo["month"]["return_pct"] == pytest.approx(0.01)
    assert demo["target_monthly_return"] == 0.01
    assert live["month"] is None  # 하루치뿐이라 수익률을 못 낸다
