from types import SimpleNamespace

import pytest

from dashboard import app as dashboard_app
from src.data.futures_exchange import LiveKeysNotConfiguredError


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
    """BTC 포지션 조회가 타임아웃나도 ETH 상태는 정상적으로 돌아와야 한다 —
    futures_rule_bot.run_once()의 심볼별 예외 격리와 동일한 이유."""
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})

    def _get_position(client, symbol):
        if symbol == "BTC/USDT:USDT":
            raise TimeoutError("backend timeout")
        return None

    monkeypatch.setattr(dashboard_app, "get_position", _get_position)

    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "error" in body["symbols"]["BTC/USDT:USDT"]
    assert body["symbols"]["BTC/USDT:USDT"]["has_position"] is False
    assert "error" not in body["symbols"]["ETH/USDT:USDT"]
    assert body["margin_equity"] == 1000.0


def test_api_status_defaults_to_demo_env(client, monkeypatch):
    monkeypatch.setattr(dashboard_app, "get_futures_balance", lambda client: {"USDT": {"total": 1000.0}})
    monkeypatch.setattr(dashboard_app, "get_position", lambda client, symbol: None)

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
    monkeypatch.setattr(dashboard_app, "get_position", lambda client, symbol: None)
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
