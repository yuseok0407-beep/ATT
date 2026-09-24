from datetime import datetime, timedelta, timezone
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
         patch("src.telegram_bot.check_ip_change", return_value=None), \
         patch("src.telegram_bot.check_daily_summary", return_value=None):
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
         patch("src.telegram_bot.check_ip_change", return_value=None), \
         patch("src.telegram_bot.check_daily_summary", return_value=None):
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
         patch("src.telegram_bot.check_ip_change", return_value=None), \
         patch("src.telegram_bot.check_daily_summary", return_value=None):
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
         patch("src.telegram_bot.check_ip_change", return_value=None), \
         patch("src.telegram_bot.check_daily_summary", return_value=None):
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



# ---------- 하트비트 워치독 (2026-09-09) ----------

def _running(monkeypatch, running=True):
    monkeypatch.setattr(tb.bot_process, "get_status", lambda env: {"running": running})


def test_check_heartbeat_stall_warns_once_when_cycles_stop_while_the_process_lives(monkeypatch):
    """프로세스 생사만 보던 기존 알림으로는 "떠 있는데 멈춘" 상태를 절대 못 잡는다."""
    _running(monkeypatch)
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 3600})

    state = {}
    first = tb.check_heartbeat_stall("demo", state)
    second = tb.check_heartbeat_stall("demo", state)

    assert first is not None and "60분 전" in first
    assert second is None  # 매 사이클 반복해서 쏘지 않는다


def test_check_heartbeat_stall_is_quiet_while_cycles_are_fresh(monkeypatch):
    _running(monkeypatch)
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 31})

    assert tb.check_heartbeat_stall("demo", {}) is None


def test_check_heartbeat_stall_reports_recovery(monkeypatch):
    _running(monkeypatch)
    state = {}
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 3600})
    tb.check_heartbeat_stall("demo", state)

    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 20})
    recovered = tb.check_heartbeat_stall("demo", state)

    assert recovered is not None and "다시" in recovered
    assert tb.check_heartbeat_stall("demo", state) is None


def test_check_heartbeat_stall_says_nothing_when_the_bot_is_stopped(monkeypatch):
    """봇이 꺼져 있으면 하트비트가 낡은 게 당연하다 — check_bot_status_change가 이미 알렸다."""
    _running(monkeypatch, running=False)
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 99999})

    assert tb.check_heartbeat_stall("demo", {}) is None


def test_check_heartbeat_stall_does_not_report_recovery_after_a_restart(monkeypatch):
    """멈춤 -> 봇 중지 -> 재시작 순서에서 "복구됐다"가 잘못 나가면 안 된다."""
    state = {}
    _running(monkeypatch)
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 3600})
    tb.check_heartbeat_stall("demo", state)

    _running(monkeypatch, running=False)
    tb.check_heartbeat_stall("demo", state)

    _running(monkeypatch)
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 10})
    assert tb.check_heartbeat_stall("demo", state) is None


def test_check_heartbeat_stall_is_quiet_when_there_is_no_heartbeat_yet(monkeypatch):
    _running(monkeypatch)
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: None)

    assert tb.check_heartbeat_stall("demo", {}) is None


# ---------- 무보호 포지션 알림 (2026-09-09) ----------

def test_unprotected_position_is_notified(monkeypatch):
    entries = [
        {"timestamp": "2026-09-09T02:00:00+00:00", "event": "unprotected_position",
         "symbol": "BTC/USDT:USDT"},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    messages = tb.check_new_journal_entries("live", {"live": {"last_entry_ts": "2026-09-09T01:00:00+00:00"}})

    assert len(messages) == 1
    assert "BTC" in messages[0] and "손절 주문이 없습니다" in messages[0]
    assert "LIVE" in messages[0]


def test_position_protected_is_notified_as_a_resolution(monkeypatch):
    entries = [
        {"timestamp": "2026-09-09T02:00:00+00:00", "event": "position_protected",
         "symbol": "BTC/USDT:USDT", "stop_loss_price": 78000.0},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    messages = tb.check_new_journal_entries("demo", {"demo": {"last_entry_ts": "2026-09-09T01:00:00+00:00"}})

    assert len(messages) == 1
    assert "복구" in messages[0] and "78,000" in messages[0]


# ---------- 보유 중 최고점 표시 ----------

def test_format_excursion_shows_the_best_and_worst_points():
    text = tb._format_excursion({"max_favorable_r": 1.62, "max_adverse_r": -0.35})
    assert "최고 +1.62R" in text and "최저 -0.35R" in text


def test_format_excursion_is_empty_without_a_record():
    assert tb._format_excursion(None) == ""
    assert tb._format_excursion({}) == ""



# ---------- 일일 요약 푸시 (2026-09-09) ----------

from datetime import datetime, timedelta, timezone


def _fixed_now(hour, day=8):
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc).astimezone()


def test_check_daily_summary_waits_until_the_configured_hour(monkeypatch):
    monkeypatch.setattr(tb, "TELEGRAM_DAILY_SUMMARY_HOUR", 9)
    monkeypatch.setattr(tb, "build_daily_summary", lambda day: f"요약 {day}")

    state = {}
    assert tb.check_daily_summary(state, now=_fixed_now(8).replace(hour=8)) is None
    assert state == {}


def test_check_daily_summary_sends_yesterday_once_past_the_hour(monkeypatch):
    """"지금까지의 오늘"이 아니라 완결된 전날을 보낸다."""
    monkeypatch.setattr(tb, "TELEGRAM_DAILY_SUMMARY_HOUR", 9)
    monkeypatch.setattr(tb, "build_daily_summary", lambda day: f"요약 {day}")

    now = _fixed_now(9).replace(hour=10)
    state = {}
    message = tb.check_daily_summary(state, now=now)

    yesterday = (now.date() - timedelta(days=1)).isoformat()
    assert message == f"요약 {yesterday}"
    assert state["last_summary_date"] == yesterday


def test_check_daily_summary_sends_only_once_per_day(monkeypatch):
    monkeypatch.setattr(tb, "TELEGRAM_DAILY_SUMMARY_HOUR", 9)
    monkeypatch.setattr(tb, "build_daily_summary", lambda day: f"요약 {day}")

    now = _fixed_now(9).replace(hour=14)
    state = {}
    assert tb.check_daily_summary(state, now=now) is not None
    assert tb.check_daily_summary(state, now=now) is None
    assert tb.check_daily_summary(state, now=now.replace(hour=23)) is None


def test_check_daily_summary_sends_again_the_next_day(monkeypatch):
    monkeypatch.setattr(tb, "TELEGRAM_DAILY_SUMMARY_HOUR", 9)
    monkeypatch.setattr(tb, "build_daily_summary", lambda day: f"요약 {day}")

    state = {}
    tb.check_daily_summary(state, now=_fixed_now(9, day=8).replace(hour=10))
    message = tb.check_daily_summary(state, now=_fixed_now(9, day=9).replace(hour=10))

    assert message is not None


def test_check_daily_summary_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(tb, "TELEGRAM_DAILY_SUMMARY_HOUR", -1)
    assert tb.check_daily_summary({}, now=_fixed_now(9).replace(hour=23)) is None


def test_build_daily_summary_covers_both_accounts(monkeypatch):
    """두 계좌를 한 메시지에 담는다 — 두 통으로 나누면 폰에서 비교가 안 된다."""
    entries = [
        {"timestamp": "2026-09-08T05:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "signal": "LONG", "entry_price": 100.0, "stop_loss_price": 99.0},
        {"timestamp": "2026-09-08T06:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "take_profit", "entry_price": 100.0, "exit_price": 102.0, "realized_pnl": 20.0},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries if "live" not in str(path) else [])
    monkeypatch.setattr(tb.filter_stats, "read_counts", lambda path=None, days=1: {})

    from src.execution.performance import _local_day
    text = tb.build_daily_summary(_local_day("2026-09-08T06:00:00+00:00"))

    # 금액과 R은 **수수료를 뺀 값**이다(2026-09-24). 손절폭 1%에서 왕복 수수료는
    # 2 x 0.0004 / 0.01 = 0.08R이고, R당 10달러이므로 0.80달러가 빠진다.
    assert "[DEMO] 1건 · 승 1 / 패 0 · +1.92R · +19.20 USDT" in text
    assert "수수료 0.80 차감 추정 (차감 전 +20.00)" in text
    assert "[LIVE] 청산된 거래 없음" in text


def test_build_daily_summary_includes_blocked_counts(monkeypatch):
    entries = [
        {"timestamp": "2026-09-08T05:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "signal": "LONG", "entry_price": 100.0, "stop_loss_price": 99.0},
        {"timestamp": "2026-09-08T06:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "stop_loss", "entry_price": 100.0, "exit_price": 99.0, "realized_pnl": -10.0},
    ]
    from src.execution.performance import _local_day
    day = _local_day("2026-09-08T06:00:00+00:00")
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)
    monkeypatch.setattr(tb.filter_stats, "read_counts",
                        lambda path=None, days=1: {day: {"skipped_low_volatility": 7}})

    text = tb.build_daily_summary(day)

    assert "차단: 저변동 7" in text


def test_help_text_lists_every_registered_command():
    """/help와 _COMMANDS가 어긋나면 "동작은 하는데 아무도 모르는 명령"이 생긴다 — 실제로
    /conditions가 추가된 뒤 한동안 그랬고, /summary도 같은 실수를 반복할 뻔했다."""
    missing = [command for command in tb._COMMANDS if command not in tb._HELP_TEXT]
    assert missing == []


def test_help_text_does_not_advertise_commands_that_do_not_exist():
    """반대 방향도 막는다 — 없는 명령을 안내하면 사용자가 오타를 의심하게 된다."""
    import re
    advertised = set(re.findall(r"/[a-z_]+", tb._HELP_TEXT))
    assert advertised - set(tb._COMMANDS) == set()


# ---------- 설정 변경 알림 ----------

def test_config_change_notification_names_what_changed(monkeypatch):
    """설정을 바꾸고 재시작했을 때 "그 값으로 실제로 떴는지"를 폰에서 확인할 수 있어야 한다."""
    entries = [
        {"timestamp": "2026-09-12T00:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT"},
        {"timestamp": "2026-09-12T01:00:00+00:00", "event": "config_changed", "first_record": False,
         "changes": {"regime_sma_period": {"from": 0, "to": 400},
                     "symbols": {"from": ["BTC/USDT:USDT"], "to": ["BTC/USDT:USDT", "SUI/USDT:USDT"]}}},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    messages = tb.check_new_journal_entries("live", {"live": {"last_entry_ts": "2026-09-12T00:30:00+00:00"}})

    assert len(messages) == 1
    assert "레짐SMA 0→400" in messages[0]
    assert "추가 SUI" in messages[0]  # 심볼 목록은 12종목을 다 찍지 않고 차이만


def test_first_config_snapshot_says_tracking_started(monkeypatch):
    entries = [
        {"timestamp": "2026-09-12T00:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT"},
        {"timestamp": "2026-09-12T01:00:00+00:00", "event": "config_changed", "first_record": True,
         "changes": {}},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries)

    messages = tb.check_new_journal_entries("demo", {"demo": {"last_entry_ts": "2026-09-12T00:30:00+00:00"}})

    assert len(messages) == 1
    assert "설정 변경 추적 시작" in messages[0]


# ---------- 서킷브레이커 정지 지속 알림 (2026-09-22) ----------

_T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _breaker_heartbeat(monkeypatch, blocked, age=20, reason="연속 손실 5회로 임계치 도달"):
    record = {"age_seconds": age, "circuit_breaker_blocked": blocked,
              "breaker_reason": reason if blocked else None}
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: record)


def test_breaker_halt_is_quiet_until_the_threshold_then_warns_once(monkeypatch):
    """막힌 걸 처음 본 순간이 아니라 BREAKER_HALT_ALERT_HOURS가 지나서야 한 통 — 시작 알림은
    저널의 circuit_breaker_blocked가 이미 보낸다."""
    _running(monkeypatch)
    monkeypatch.setattr(tb, "BREAKER_HALT_ALERT_HOURS", 6)
    _breaker_heartbeat(monkeypatch, blocked=True)
    state = {}

    assert tb.check_breaker_halt("demo", state, now=_T0) is None
    assert tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=5)) is None
    warned = tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=6))
    assert warned is not None and "6시간째" in warned and "연속 손실 5회" in warned
    assert "/reset_streak_demo" in warned
    assert tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=12)) is None


def test_breaker_halt_reports_recovery_only_after_it_warned(monkeypatch):
    _running(monkeypatch)
    monkeypatch.setattr(tb, "BREAKER_HALT_ALERT_HOURS", 6)
    state = {}

    # 경고 전에 풀리면 조용하다 — 짧은 정지까지 "풀렸다"를 쏘면 노이즈다
    _breaker_heartbeat(monkeypatch, blocked=True)
    tb.check_breaker_halt("live", state, now=_T0)
    _breaker_heartbeat(monkeypatch, blocked=False)
    assert tb.check_breaker_halt("live", state, now=_T0 + timedelta(hours=1)) is None

    _breaker_heartbeat(monkeypatch, blocked=True)
    tb.check_breaker_halt("live", state, now=_T0 + timedelta(hours=2))
    assert tb.check_breaker_halt("live", state, now=_T0 + timedelta(hours=8)) is not None
    _breaker_heartbeat(monkeypatch, blocked=False)
    recovered = tb.check_breaker_halt("live", state, now=_T0 + timedelta(hours=9))
    assert recovered is not None and "LIVE" in recovered and "풀려" in recovered
    assert tb.check_breaker_halt("live", state, now=_T0 + timedelta(hours=10)) is None


def test_breaker_halt_clock_restarts_after_it_clears(monkeypatch):
    """정지 -> 해제 -> 재정지면 새 정지의 시간만 센다(앞 정지 시간을 이어 붙이지 않는다)."""
    _running(monkeypatch)
    monkeypatch.setattr(tb, "BREAKER_HALT_ALERT_HOURS", 6)
    state = {}
    _breaker_heartbeat(monkeypatch, blocked=True)
    tb.check_breaker_halt("demo", state, now=_T0)
    _breaker_heartbeat(monkeypatch, blocked=False)
    tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=5))
    _breaker_heartbeat(monkeypatch, blocked=True)
    tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=5, minutes=1))

    assert tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=7)) is None


def test_breaker_halt_says_nothing_when_the_bot_is_stopped(monkeypatch):
    _running(monkeypatch, running=False)
    monkeypatch.setattr(tb, "BREAKER_HALT_ALERT_HOURS", 6)
    _breaker_heartbeat(monkeypatch, blocked=True)
    state = {"demo": {"breaker_blocked_since": (_T0 - timedelta(hours=48)).isoformat()}}

    assert tb.check_breaker_halt("demo", state, now=_T0) is None
    assert "breaker_blocked_since" not in state["demo"]  # 재시작 후 옛 정지 시간을 이어 세지 않는다


def test_breaker_halt_ignores_a_stale_or_old_format_heartbeat(monkeypatch):
    """낡은 하트비트는 check_heartbeat_stall 담당이고, 필드가 없는 건 재시작 전 옛 봇 코드다 —
    둘 다 '지금 막혀 있는가'를 알 수 없으므로 판단을 바꾸지 않는다."""
    _running(monkeypatch)
    monkeypatch.setattr(tb, "BREAKER_HALT_ALERT_HOURS", 6)
    since = (_T0 - timedelta(hours=1)).isoformat()
    state = {"demo": {"breaker_blocked_since": since}}

    _breaker_heartbeat(monkeypatch, blocked=False, age=99999)
    assert tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=10)) is None
    monkeypatch.setattr(tb, "read_heartbeat", lambda path=None: {"age_seconds": 20})
    assert tb.check_breaker_halt("demo", state, now=_T0 + timedelta(hours=10)) is None
    assert state["demo"]["breaker_blocked_since"] == since


def test_breaker_halt_can_be_disabled(monkeypatch):
    _running(monkeypatch)
    monkeypatch.setattr(tb, "BREAKER_HALT_ALERT_HOURS", 0)
    _breaker_heartbeat(monkeypatch, blocked=True)
    state = {"demo": {"breaker_blocked_since": (_T0 - timedelta(hours=48)).isoformat()}}

    assert tb.check_breaker_halt("demo", state, now=_T0) is None


def test_daily_summary_reports_fee_adjusted_money(monkeypatch):
    """거래소의 realizedPnl에는 수수료가 안 들어있다 — 그대로 알리면 "수익 +N"이라고 해놓고
    계좌 총자산은 줄어 있는 일이 생긴다(2026-09-24 사용자 보고). 이 전략은 건당 기대값과
    수수료가 같은 크기라 부호까지 뒤집힌다."""
    entries = [
        {"timestamp": "2026-09-08T05:00:00+00:00", "event": "entered", "symbol": "BTC/USDT:USDT",
         "signal": "LONG", "entry_price": 100.0, "stop_loss_price": 99.0},
        {"timestamp": "2026-09-08T06:00:00+00:00", "event": "closed", "symbol": "BTC/USDT:USDT",
         "reason": "take_profit", "entry_price": 100.0, "exit_price": 102.0,
         "realized_pnl": 20.0, "net_realized_pnl": 18.5, "realized_r": 2.0,
         "net_realized_r": 1.85, "fee_r": 0.15, "total_fee": 1.5},
    ]
    monkeypatch.setattr(tb, "read_entries", lambda path=None: entries if "live" not in str(path) else [])
    monkeypatch.setattr(tb.filter_stats, "read_counts", lambda path=None, days=1: {})

    from src.execution.performance import _local_day
    text = tb.build_daily_summary(_local_day("2026-09-08T06:00:00+00:00"))

    assert "+1.85R · +18.50 USDT" in text
    # 실측 수수료가 기록에 있으면 "추정"을 붙이지 않는다.
    assert "수수료 1.50 차감 (차감 전 +20.00)" in text


def test_daily_summary_shows_the_equity_move(monkeypatch, tmp_path):
    """자산 줄만이 계좌 화면의 총자산과 직접 맞춰볼 수 있는 값이다."""
    path = tmp_path / "equity.json"
    monkeypatch.setattr(tb.equity_log, "DEFAULT_PATH", str(path))
    from datetime import datetime
    for hour, value in ((1, 1000.0), (23, 1012.5)):
        tb.equity_log.record(value, path=str(path),
                             now=datetime.fromisoformat(f"2026-09-08T{hour:02d}:00:00").astimezone())

    monkeypatch.setattr(tb, "read_entries", lambda path=None: [])
    text = tb.build_daily_summary("2026-09-08")

    assert "자산 1000.00 → 1012.50 (+12.50, +1.25%)" in text


def test_daily_summary_shows_equity_even_when_nothing_closed(monkeypatch, tmp_path):
    """거래가 없어도 자산은 움직인다(보유 포지션 평가손익, 펀딩비). "거래 없음"만 보내면
    변화가 없었던 것으로 읽혀 또 계좌와 어긋난다."""
    path = tmp_path / "equity.json"
    monkeypatch.setattr(tb.equity_log, "DEFAULT_PATH", str(path))
    from datetime import datetime
    for hour, value in ((1, 500.0), (23, 490.0)):
        tb.equity_log.record(value, path=str(path),
                             now=datetime.fromisoformat(f"2026-09-08T{hour:02d}:00:00").astimezone())

    monkeypatch.setattr(tb, "read_entries", lambda path=None: [])
    text = tb.build_daily_summary("2026-09-08")

    assert "청산된 거래 없음" in text
    assert "자산 500.00 → 490.00 (-10.00, -2.00%)" in text


def test_daily_summary_omits_equity_before_any_is_recorded(monkeypatch):
    """새 코드로 봇을 재시작하기 전에는 기록이 없다 — 그 줄 없이 정상 동작해야 한다."""
    monkeypatch.setattr(tb, "read_entries", lambda path=None: [])
    text = tb.build_daily_summary("2026-09-08")
    assert "자산" not in text
    assert "청산된 거래 없음" in text


def test_daily_summary_compares_recent_equity_to_the_monthly_goal(monkeypatch, tmp_path):
    """목표(월 +10%)는 금액 기준이므로 비교도 금액 기준이어야 한다 — R 합계로는 못 낸다."""
    path = tmp_path / "equity.json"
    monkeypatch.setattr(tb.equity_log, "DEFAULT_PATH", str(path))
    from datetime import datetime, timedelta
    base = datetime.fromisoformat("2026-09-01T09:00:00").astimezone()
    for i, value in enumerate([1000.0, 1040.0, 1080.0]):
        tb.equity_log.record(value, path=str(path), now=base + timedelta(days=i))

    monkeypatch.setattr(tb, "read_entries", lambda path=None: [])
    text = tb.build_daily_summary("2026-09-03")

    assert "— 목표 진행 —" in text
    assert "1000.00 → 1080.00 (+8.00%)" in text
    assert "목표 월 +10%" in text
