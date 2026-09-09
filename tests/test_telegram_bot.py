from unittest.mock import patch

import pytest

from src import telegram_bot as tb


# ---------- check_new_journal_entries ----------

def test_check_new_journal_entries_bootstraps_watermark_without_notifying(monkeypatch):
    entries = [
        {"timestamp": "2026-08-22T00:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT"},
        {"timestamp": "2026-08-22T01:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT"},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    state = {}
    messages = tb.check_new_journal_entries("demo", state)

    assert messages == []
    assert state["demo"]["last_entry_ts"] == "2026-08-22T01:00:00+00:00"


def test_check_new_journal_entries_returns_messages_for_notify_worthy_events_only(monkeypatch):
    entries = [
        {"timestamp": "2026-08-22T01:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT"},  # 이미 처리됨
        {"timestamp": "2026-08-22T02:00:00+00:00", "event": "entered", "symbol": "ETH/USDT:USDT",
         "signal": "LONG", "entry_price": 3000, "stop_loss_price": 2950, "take_profit_price": 3100},
        {"timestamp": "2026-08-22T02:30:00+00:00", "event": "no_signal", "symbol": "SOL/USDT:USDT"},
        {"timestamp": "2026-08-22T03:00:00+00:00", "event": "closed", "symbol": "ETH/USDT:USDT",
         "reason": "take_profit", "exit_price": 3100, "realized_pnl": 42.5},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    state = {"demo": {"last_entry_ts": "2026-08-22T01:00:00+00:00"}}
    messages = tb.check_new_journal_entries("demo", state)

    assert len(messages) == 2
    assert "ETH" in messages[0] and "진입" in messages[0]
    assert "ETH" in messages[1] and "익절" in messages[1] and "42.50" in messages[1]
    assert state["demo"]["last_entry_ts"] == "2026-08-22T03:00:00+00:00"


def test_check_new_journal_entries_returns_empty_when_nothing_new(monkeypatch):
    entries = [{"timestamp": "2026-08-22T01:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT"}]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    state = {"demo": {"last_entry_ts": "2026-08-22T01:00:00+00:00"}}
    assert tb.check_new_journal_entries("demo", state) == []


def test_check_new_journal_entries_uses_live_journal_path_for_live_env(monkeypatch):
    captured = {}

    def _read_entries(path=None):
        captured["path"] = path
        return []

    monkeypatch.setattr(tb, "read_entries", _read_entries)

    tb.check_new_journal_entries("live", {})
    assert captured["path"] == tb.LIVE_JOURNAL_PATH


# ---------- check_bot_status_change ----------

def test_check_bot_status_change_bootstraps_without_notifying():
    with patch("src.telegram_bot.bot_process.get_status", return_value={"running": True}):
        state = {}
        assert tb.check_bot_status_change("demo", state) is None
        assert state["demo"]["was_running"] is True


def test_check_bot_status_change_notifies_on_transition_to_stopped():
    with patch("src.telegram_bot.bot_process.get_status", return_value={"running": False}):
        state = {"demo": {"was_running": True}}
        message = tb.check_bot_status_change("demo", state)
        assert message is not None and "꺼졌습니다" in message
        assert state["demo"]["was_running"] is False


def test_check_bot_status_change_notifies_on_transition_to_running():
    with patch("src.telegram_bot.bot_process.get_status", return_value={"running": True}):
        state = {"live": {"was_running": False}}
        message = tb.check_bot_status_change("live", state)
        assert message is not None and "시작됐습니다" in message and "LIVE" in message


def test_check_bot_status_change_no_message_when_unchanged():
    with patch("src.telegram_bot.bot_process.get_status", return_value={"running": True}):
        state = {"demo": {"was_running": True}}
        assert tb.check_bot_status_change("demo", state) is None


# ---------- check_ip_change ----------

def test_check_ip_change_bootstraps_without_notifying(monkeypatch):
    monkeypatch.setattr(tb, "get_public_ip", lambda: "1.1.1.1")
    state = {}
    assert tb.check_ip_change(state) is None
    assert state["last_ip"] == "1.1.1.1"


def test_check_ip_change_notifies_on_change(monkeypatch):
    monkeypatch.setattr(tb, "get_public_ip", lambda: "2.2.2.2")
    state = {"last_ip": "1.1.1.1"}
    message = tb.check_ip_change(state)
    assert message is not None and "1.1.1.1" in message and "2.2.2.2" in message
    assert state["last_ip"] == "2.2.2.2"


def test_check_ip_change_no_message_when_unchanged(monkeypatch):
    monkeypatch.setattr(tb, "get_public_ip", lambda: "1.1.1.1")
    state = {"last_ip": "1.1.1.1"}
    assert tb.check_ip_change(state) is None


def test_check_ip_change_ignores_fetch_failure(monkeypatch):
    monkeypatch.setattr(tb, "get_public_ip", lambda: None)
    state = {"last_ip": "1.1.1.1"}
    assert tb.check_ip_change(state) is None
    assert state["last_ip"] == "1.1.1.1"  # 실패했으니 덮어쓰지 않음


# ---------- is_authorized ----------

def test_is_authorized_accepts_matching_chat_id():
    update = {"message": {"chat": {"id": 555}, "text": "/status"}}
    assert tb.is_authorized(update, "555") is True


def test_is_authorized_rejects_mismatched_chat_id():
    update = {"message": {"chat": {"id": 999}, "text": "/start_live"}}
    assert tb.is_authorized(update, "555") is False


def test_is_authorized_rejects_update_without_message():
    assert tb.is_authorized({}, "555") is False


# ---------- dispatch_command ----------

@pytest.mark.parametrize("text,expected", [
    ("/start_demo", ("demo", "start")),
    ("/stop_demo", ("demo", "stop")),
    ("/start_live", ("live", "start")),
    ("/stop_live", ("live", "stop")),
    ("/reset_streak_demo", ("demo", "reset_streak")),
    ("/reset_streak_live", ("live", "reset_streak")),
    ("/status", ("", "status")),
    ("/help", ("", "help")),
    ("/status@mybot extra text", ("", "status")),
])
def test_dispatch_command_recognizes_known_commands(text, expected):
    assert tb.dispatch_command(text) == expected


@pytest.mark.parametrize("text", ["", "hello", "/unknown_command"])
def test_dispatch_command_returns_none_for_unknown_input(text):
    assert tb.dispatch_command(text) is None


# ---------- format_status ----------

class _FakeStatusClient:
    def __init__(self, markets, positions):
        self.markets = markets
        self._positions = positions

    def load_markets(self):
        pass


def _patch_status_common(monkeypatch, running=True, heartbeat=None):
    monkeypatch.setattr(tb.bot_process, "get_status", lambda env: {"running": running})
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: heartbeat)


def test_format_status_shows_equity_and_no_positions(monkeypatch):
    _patch_status_common(monkeypatch, running=True, heartbeat={"age_seconds": 5.0, "cycle_count": 42})
    client = _FakeStatusClient(markets={"BTC/USDT:USDT": {}}, positions={})
    monkeypatch.setattr(tb, "get_futures_client", lambda env: client)
    monkeypatch.setattr(tb, "get_futures_balance", lambda c: {"USDT": {"total": 1234.5}})
    monkeypatch.setattr(tb, "get_position", lambda c, symbol: None)
    monkeypatch.setattr(tb, "FUTURES_SYMBOLS", ["BTC/USDT:USDT"])

    text = tb.format_status("demo")

    assert "[DEMO] 실행 중" in text
    assert "마지막 사이클: 5초 전 (#42)" in text
    assert "마진 자산: $1,234.50" in text
    assert "BTC" not in text  # 포지션 없으면 종목별 상세는 안 나옴


def test_format_status_shows_position_price_and_brackets(monkeypatch):
    _patch_status_common(monkeypatch, running=True, heartbeat=None)
    client = _FakeStatusClient(markets={"XRP/USDT:USDT": {}}, positions={})
    monkeypatch.setattr(tb, "get_futures_client", lambda env: client)
    monkeypatch.setattr(tb, "get_futures_balance", lambda c: {"USDT": {"total": 500.0}})
    monkeypatch.setattr(tb, "FUTURES_SYMBOLS", ["XRP/USDT:USDT"])

    position = {"side": "long", "entryPrice": 1.4059, "markPrice": 1.4252, "unrealizedPnl": 152.6}
    monkeypatch.setattr(tb, "get_position", lambda c, symbol: position if symbol == "XRP/USDT:USDT" else None)
    monkeypatch.setattr(tb, "get_bracket_prices", lambda c, symbol: (1.3884, 1.4411))

    text = tb.format_status("live")

    assert "XRP 롱" in text
    assert "진입 1.4059" in text and "현재가 1.4252" in text
    assert "손익 +152.60 USDT" in text
    assert "손절 1.3884" in text and "익절 1.4411" in text


def test_format_status_reports_live_keys_not_configured(monkeypatch):
    _patch_status_common(monkeypatch, running=False, heartbeat=None)

    def _raise(env):
        raise tb.LiveKeysNotConfiguredError("no keys")

    monkeypatch.setattr(tb, "get_futures_client", _raise)

    text = tb.format_status("live")
    assert "라이브 키 미설정" in text


def test_format_status_reports_generic_connection_failure(monkeypatch):
    _patch_status_common(monkeypatch, running=False, heartbeat=None)

    def _raise(env):
        raise Exception("network error")

    monkeypatch.setattr(tb, "get_futures_client", _raise)

    text = tb.format_status("demo")
    assert "계좌 연결 실패" in text


def test_format_status_skips_symbol_when_position_lookup_raises(monkeypatch):
    """한 종목 조회가 실패해도 나머지 정보(자산 등)는 그대로 보여야 한다."""
    _patch_status_common(monkeypatch, running=True, heartbeat=None)
    client = _FakeStatusClient(markets={"BTC/USDT:USDT": {}}, positions={})
    monkeypatch.setattr(tb, "get_futures_client", lambda env: client)
    monkeypatch.setattr(tb, "get_futures_balance", lambda c: {"USDT": {"total": 100.0}})
    monkeypatch.setattr(tb, "FUTURES_SYMBOLS", ["BTC/USDT:USDT"])

    def _raise(c, symbol):
        raise Exception("timeout")

    monkeypatch.setattr(tb, "get_position", _raise)

    text = tb.format_status("demo")
    assert "마진 자산: $100.00" in text


# ---------- run_once ----------

def _make_update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def test_run_once_authorized_start_live_calls_bot_process_and_replies():
    update = _make_update(10, 555, "/start_live")
    with patch("src.telegram_bot.get_updates", return_value=[update]), \
         patch("src.telegram_bot.bot_process.start", return_value={"pid": 4242}) as mock_start, \
         patch("src.telegram_bot.send_message") as mock_send, \
         patch("src.telegram_bot.check_new_journal_entries", return_value=[]), \
         patch("src.telegram_bot.check_bot_status_change", return_value=None), \
         patch("src.telegram_bot.check_ip_change", return_value=None):
        state = tb.run_once({}, chat_id="555", token="tok")

    mock_start.assert_called_once_with("live")
    assert any("LIVE" in c.args[0] for c in mock_send.call_args_list)
    assert state["update_offset"] == 11


def test_run_once_authorized_reset_streak_live_uses_live_journal_and_replies():
    update = _make_update(20, 555, "/reset_streak_live")
    with patch("src.telegram_bot.get_updates", return_value=[update]), \
         patch("src.telegram_bot.reset_consecutive_losses") as mock_reset, \
         patch("src.telegram_bot.send_message") as mock_send, \
         patch("src.telegram_bot.check_new_journal_entries", return_value=[]), \
         patch("src.telegram_bot.check_bot_status_change", return_value=None), \
         patch("src.telegram_bot.check_ip_change", return_value=None):
        tb.run_once({}, chat_id="555", token="tok")

    mock_reset.assert_called_once_with(journal_path=tb.LIVE_JOURNAL_PATH)
    assert any("LIVE" in c.args[0] and "리셋" in c.args[0] for c in mock_send.call_args_list)


def test_run_once_ignores_unauthorized_command_without_calling_bot_process():
    update = _make_update(11, 999, "/start_live")
    with patch("src.telegram_bot.get_updates", return_value=[update]), \
         patch("src.telegram_bot.bot_process.start") as mock_start, \
         patch("src.telegram_bot.send_message") as mock_send, \
         patch("src.telegram_bot.check_new_journal_entries", return_value=[]), \
         patch("src.telegram_bot.check_bot_status_change", return_value=None), \
         patch("src.telegram_bot.check_ip_change", return_value=None):
        state = tb.run_once({}, chat_id="555", token="tok")

    mock_start.assert_not_called()
    mock_send.assert_not_called()
    assert state["update_offset"] == 12  # 그래도 offset은 진행돼야 같은 메시지를 계속 재조회 안 함


def test_run_once_sends_new_journal_and_status_and_ip_messages():
    with patch("src.telegram_bot.get_updates", return_value=[]), \
         patch("src.telegram_bot.send_message") as mock_send, \
         patch("src.telegram_bot.check_new_journal_entries", side_effect=lambda env, s: [f"entry-{env}"]), \
         patch("src.telegram_bot.check_bot_status_change", side_effect=lambda env, s: f"status-{env}"), \
         patch("src.telegram_bot.check_ip_change", return_value="ip-changed"):
        tb.run_once({}, chat_id="555", token="tok")

    sent_texts = [c.args[0] for c in mock_send.call_args_list]
    assert "entry-demo" in sent_texts and "entry-live" in sent_texts
    assert "status-demo" in sent_texts and "status-live" in sent_texts
    assert "ip-changed" in sent_texts


def test_run_once_replies_help_and_does_not_touch_bot_process():
    update = _make_update(20, 555, "/help")
    with patch("src.telegram_bot.get_updates", return_value=[update]), \
         patch("src.telegram_bot.bot_process.start") as mock_start, \
         patch("src.telegram_bot.bot_process.stop") as mock_stop, \
         patch("src.telegram_bot.send_message") as mock_send, \
         patch("src.telegram_bot.check_new_journal_entries", return_value=[]), \
         patch("src.telegram_bot.check_bot_status_change", return_value=None), \
         patch("src.telegram_bot.check_ip_change", return_value=None):
        tb.run_once({}, chat_id="555", token="tok")

    mock_start.assert_not_called()
    mock_stop.assert_not_called()
    assert mock_send.call_args_list[0].args[0] == tb._HELP_TEXT


def test_conditions_command_is_dispatched():
    assert tb.dispatch_command("/conditions") == ("", "conditions")


def test_format_conditions_renders_waiting_symbols(monkeypatch):
    """신호가 없을 때 '무엇을 기다리는 중인지'가 보여야 한다."""
    monkeypatch.setattr(tb, "get_futures_client", lambda env: _raise_(RuntimeError("no keys")))
    monkeypatch.setattr(tb, "collect_conditions", lambda symbols: {
        "timeframe": "1h", "adx_threshold": 30.0, "sma_period": 10, "regime_sma_period": 400,
        "symbols": [{
            "symbol": "BTC/USDT:USDT", "ready": False, "signal": None, "candidate_side": "LONG",
            "proximity": 0.62, "distance_pct": -0.14, "sma_period": 10,
            "adx": 23.1, "adx_threshold": 30.0, "rsi": 42.3,
            "blockers": ["ADX 23.1 < 30", "SMA10 돌파 대기 (-0.14%)"],
        }],
        "errors": [],
    })

    text = tb.format_conditions()
    assert "BTC 롱 대기" in text
    assert "62%" in text
    assert "ADX 23.1/30" in text
    assert "SMA10 돌파 대기" in text


def test_format_conditions_highlights_a_live_signal(monkeypatch):
    monkeypatch.setattr(tb, "get_futures_client", lambda env: _raise_(RuntimeError("no keys")))
    monkeypatch.setattr(tb, "collect_conditions", lambda symbols: {
        "timeframe": "1h", "adx_threshold": 30.0, "sma_period": 10, "regime_sma_period": 400,
        "symbols": [{"symbol": "ZEC/USDT:USDT", "ready": True, "signal": "SHORT",
                     "candidate_side": "SHORT", "proximity": 1.0, "blockers": []}],
        "errors": [],
    })
    assert "ZEC — 지금 숏 신호" in tb.format_conditions()


def test_format_conditions_survives_a_collect_failure(monkeypatch):
    """조회가 통째로 실패해도 예외를 올리지 않고 사유를 돌려줘야 한다 — 알림 루프를 죽이면 안 된다."""
    monkeypatch.setattr(tb, "get_futures_client", lambda env: _raise_(RuntimeError("no keys")))
    monkeypatch.setattr(tb, "collect_conditions",
                        lambda symbols: _raise_(TimeoutError("backend timeout")))
    assert "조건 조회 실패" in tb.format_conditions()


def _raise_(exc):
    raise exc
