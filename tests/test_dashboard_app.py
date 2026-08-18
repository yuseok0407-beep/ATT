from types import SimpleNamespace

import pytest

from dashboard import app as dashboard_app


class _FakeClient:
    markets = {"BTC/USDT:USDT": {}, "ETH/USDT:USDT": {}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard_app, "_get_client", lambda: _FakeClient())
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
