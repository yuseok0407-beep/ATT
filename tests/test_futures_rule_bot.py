from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src import futures_rule_bot as bot

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"]


def _flat_df(n=60):
    closes = np.full(n, 100.0)
    return pd.DataFrame({"high": closes + 0.1, "low": closes - 0.1, "close": closes})


@pytest.fixture(autouse=True)
def _patch_symbols(monkeypatch):
    monkeypatch.setattr(bot, "FUTURES_SYMBOLS", SYMBOLS)
    monkeypatch.setattr(bot, "MAX_CONCURRENT_POSITIONS", 2)


def _base_patches(balance_total=10_000, positions=None):
    positions = positions or {}
    return [
        patch("src.futures_rule_bot.get_futures_balance", return_value={"USDT": {"total": balance_total}}),
        patch("src.futures_rule_bot.get_position", side_effect=lambda client, symbol: positions.get(symbol)),
        patch("src.futures_rule_bot.cleanup_stale_orders"),
        patch("src.futures_rule_bot.check_and_log_closed_trade"),
        patch("src.futures_rule_bot.append_entry"),
        patch("src.futures_rule_bot.get_futures_market_data_client"),
        patch("src.futures_rule_bot.fetch_ohlcv_df", return_value=_flat_df()),
    ]


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in patches:
        p.stop()


def test_run_once_blocked_by_circuit_breaker():
    patches = _base_patches()
    _start(patches)
    try:
        cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=-0.10)
        assert cycle["event"] == "circuit_breaker_blocked"
        assert "symbols" not in cycle or cycle["symbols"] == {}
    finally:
        _stop(patches)


def test_run_once_computes_consecutive_losses_from_journal_when_not_passed():
    """실전 버그 재현: run_futures_bot.py가 consecutive_losses를 안 넘겨서 서킷브레이커가
    죽어있던 걸 고침 — 이제 안 넘기면(None) 저널에서 직접 계산해야 한다."""
    patches = _base_patches()
    _start(patches)
    try:
        losing_streak = [
            {"event": "closed", "realized_pnl": -1.0},
            {"event": "closed", "realized_pnl": -2.0},
            {"event": "closed", "realized_pnl": -3.0},
        ]
        with patch("src.futures_rule_bot.read_entries", return_value=losing_streak):
            cycle = bot.run_once(MagicMock(), daily_pnl_pct=0.0)
        assert cycle["event"] == "circuit_breaker_blocked"
    finally:
        _stop(patches)


def test_run_once_does_not_block_when_journal_losing_streak_is_below_threshold():
    patches = _base_patches()
    _start(patches)
    try:
        losing_streak = [
            {"event": "closed", "realized_pnl": -1.0},
            {"event": "closed", "realized_pnl": -2.0},
        ]
        with patch("src.futures_rule_bot.read_entries", return_value=losing_streak):
            cycle = bot.run_once(MagicMock(), daily_pnl_pct=0.0)
        assert cycle.get("event") != "circuit_breaker_blocked"
    finally:
        _stop(patches)


def test_run_once_checks_every_symbol_independently():
    patches = _base_patches()
    _start(patches)
    try:
        cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert set(cycle["symbols"].keys()) == set(SYMBOLS)
        for symbol in SYMBOLS:
            assert cycle["symbols"][symbol]["event"] == "no_signal"
        assert cycle["open_position_count"] == 0
    finally:
        _stop(patches)


def test_run_once_reports_holding_position_without_touching_it():
    positions = {"BTC/USDT:USDT": {"side": "long", "contracts": 0.01}}
    patches = _base_patches(positions=positions)
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value=None):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)
        assert cycle["symbols"]["BTC/USDT:USDT"]["event"] == "holding_position"
        assert cycle["symbols"]["ETH/USDT:USDT"]["event"] == "no_signal"
        assert cycle["open_position_count"] == 1
    finally:
        _stop(patches)


def test_run_once_enters_on_signal_and_counts_toward_cap():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket", return_value={"status": "opened"}) as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        entered = [s for s, r in cycle["symbols"].items() if r["event"] == "entered"]
        skipped = [s for s, r in cycle["symbols"].items() if r["event"] == "skipped_max_positions"]

        # MAX_CONCURRENT_POSITIONS=2 이므로 4종목 다 신호가 나도 2개만 진입하고 나머지는 스킵되어야 한다
        assert len(entered) == 2
        assert len(skipped) == 2
        assert mock_open.call_count == 2
        assert cycle["open_position_count"] == 2
    finally:
        _stop(patches)


def test_run_once_respects_existing_positions_when_capping():
    # 이미 2개(cap) 보유 중이면 나머지 종목은 신호가 나도 전부 스킵돼야 한다
    positions = {
        "BTC/USDT:USDT": {"side": "long", "contracts": 0.01},
        "ETH/USDT:USDT": {"side": "short", "contracts": 0.1},
    }
    patches = _base_patches(positions=positions)
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        assert cycle["symbols"]["SOL/USDT:USDT"]["event"] == "skipped_max_positions"
        assert cycle["symbols"]["XRP/USDT:USDT"]["event"] == "skipped_max_positions"
        mock_open.assert_not_called()
        assert cycle["open_position_count"] == 2
    finally:
        _stop(patches)


def test_run_once_rejects_unsafe_stop_for_one_symbol():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value="LONG"), \
             patch("src.futures_rule_bot.compute_bracket_prices", return_value=(1.0, 200.0)), \
             patch("src.futures_rule_bot.open_position_with_bracket") as mock_open:
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0)

        for symbol in SYMBOLS:
            assert cycle["symbols"][symbol]["event"] == "rejected_unsafe_stop"
        mock_open.assert_not_called()
    finally:
        _stop(patches)


def test_run_once_uses_explicit_symbols_list_over_config_default():
    patches = _base_patches()
    _start(patches)
    try:
        with patch("src.futures_rule_bot.detect_signal", return_value=None):
            cycle = bot.run_once(MagicMock(), consecutive_losses=0, daily_pnl_pct=0.0,
                                  symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"])
        assert set(cycle["symbols"].keys()) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}
    finally:
        _stop(patches)


class TestInitialize:
    """실전에서 발견된 문제: SOXL이 실거래 선물엔 있지만 바이낸스 데모 트레이딩 환경엔 없어서
    initialize()가 BadSymbol로 죽어 봇 자체가 못 뜨던 것을 고친 부분."""

    def test_skips_symbol_not_in_client_markets_and_returns_available_list(self):
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS if s != "SOL/USDT:USDT"}  # SOL만 이 거래소에 없다고 가정

        with patch("src.futures_rule_bot.set_margin_mode") as mock_margin, \
             patch("src.futures_rule_bot.set_leverage") as mock_leverage:
            available = bot.initialize(client)

        assert available == ["BTC/USDT:USDT", "ETH/USDT:USDT", "XRP/USDT:USDT"]
        client.load_markets.assert_called_once()
        assert mock_margin.call_count == 3
        assert mock_leverage.call_count == 3

    def test_all_symbols_available_returns_full_list(self):
        client = MagicMock()
        client.markets = {s: {} for s in SYMBOLS}

        with patch("src.futures_rule_bot.set_margin_mode"), patch("src.futures_rule_bot.set_leverage"):
            available = bot.initialize(client)

        assert available == SYMBOLS


class TestCheckAndLogClosedTrade:
    """실전에서 발견된 문제(청산 이벤트가 저널에 전혀 안 남던 것)를 고친 부분."""

    SYMBOL = "ETH/USDT:USDT"

    def _paths(self, tmp_path, monkeypatch):
        journal_path = str(tmp_path / "journal.jsonl")
        state_path = str(tmp_path / "last_trade.json")
        monkeypatch.setattr(bot, "JOURNAL_PATH", journal_path)
        monkeypatch.setattr(bot, "LAST_TRADE_STATE_PATH", state_path)
        return journal_path, state_path

    def test_no_trades_does_nothing(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_my_trades.return_value = []
        assert bot.check_and_log_closed_trade(client, self.SYMBOL) is None

    def test_already_seen_trade_id_does_nothing(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot._save_last_trade_id(self.SYMBOL, "trade-1", path=state_path)
        client = MagicMock()
        client.fetch_my_trades.return_value = [{"id": "trade-1", "price": 100.0, "info": {}}]
        assert bot.check_and_log_closed_trade(client, self.SYMBOL) is None

    def test_no_matching_entry_journal_just_remembers_trade_id(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        client = MagicMock()
        client.fetch_my_trades.return_value = [{"id": "trade-2", "price": 100.0, "info": {}}]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result is None
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "trade-2"
        assert bot.read_entries(path=journal_path) == []

    def test_detects_stop_loss_exit_and_logs_realized_pnl(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1917.99,
            "stop_loss_price": 1893.72, "take_profit_price": 1965.63,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-entry", "price": 1917.99, "info": {"realizedPnl": "0"}},
            {"id": "trade-exit", "price": 1893.72, "info": {"realizedPnl": "-101.06028000"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["event"] == "closed"
        assert result["reason"] == "stop_loss"
        assert result["realized_pnl"] == pytest.approx(-101.06028)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "trade-exit"

    def test_detects_take_profit_exit(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-exit-tp", "price": 1947.5, "info": {"realizedPnl": "197.60"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["reason"] == "take_profit"
        assert result["realized_pnl"] == pytest.approx(197.60)

    def test_seeing_entry_fill_itself_does_not_log_a_close(self, tmp_path, monkeypatch):
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-entry-only", "price": 1900.0, "info": {"realizedPnl": "0"}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)
        assert result is None
        entries = bot.read_entries(path=journal_path)
        assert all(e.get("event") != "closed" for e in entries)

    def test_fetches_trades_since_entry_timestamp_not_a_bare_limit(self, tmp_path, monkeypatch):
        """실전 버그: since 없이 limit만 주면 거래소가 "최신 N개"가 아니라 "가장 오래된 N개"를
        돌려줘서, 체결이 limit개를 넘어가면 이 함수가 옛날 체결(진입 체결)에 영원히 멈춰버렸다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 1900.0,
            "stop_loss_price": 1876.25, "take_profit_price": 1947.5,
        }, path=journal_path)
        entry_timestamp = bot.read_entries(path=journal_path)[0]["timestamp"]
        expected_since_ms = int(datetime.fromisoformat(entry_timestamp).timestamp() * 1000) - 60_000

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "trade-exit-tp", "price": 1947.5, "amount": 1.0, "info": {"realizedPnl": "47.5"}},
        ]

        bot.check_and_log_closed_trade(client, self.SYMBOL)

        client.fetch_my_trades.assert_called_once_with(self.SYMBOL, since=expected_since_ms, limit=50)

    def test_aggregates_multiple_partial_fill_trades_into_one_closed_entry(self, tmp_path, monkeypatch):
        """실전 버그 재현: TAKE_PROFIT_MARKET 주문 하나가 부분 체결 3번으로 쪼개졌는데,
        마지막 한 건의 realizedPnl만 보면 나머지 두 건의 손익이 통째로 누락됐다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 65023.89,
            "stop_loss_price": 65836.688625, "take_profit_price": 63398.29275,
            "execution": {"entry_order": {"id": "28535168719"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-entry", "price": 65061.0, "amount": 0.1228, "info": {"realizedPnl": "0", "orderId": 28535168719}},
            {"id": "t-tp-1", "price": 63467.7, "amount": 0.005, "info": {"realizedPnl": "7.9665", "orderId": 28538022498}},
            {"id": "t-tp-2", "price": 63489.1, "amount": 0.0008, "info": {"realizedPnl": "1.25752", "orderId": 28538022498}},
            {"id": "t-tp-3", "price": 63501.0, "amount": 0.117, "info": {"realizedPnl": "182.52", "orderId": 28538022498}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)

        assert result["reason"] == "take_profit"
        assert result["realized_pnl"] == pytest.approx(7.9665 + 1.25752 + 182.52)
        assert result["exit_price"] == pytest.approx(63499.567, abs=0.01)
        assert bot._load_last_trade_ids(path=state_path)[self.SYMBOL] == "t-tp-3"

    def test_excludes_entry_fill_by_order_id_even_when_price_differs_from_recorded_entry_price(self, tmp_path, monkeypatch):
        """실전 버그: 진입가는 신호가 뜬 캔들 종가로 기록되는데 실제 체결가는 슬리피지로 조금
        다를 수 있다(65023.89 기록 vs 65061.0 실체결). 가격 비교만으로는 진입 체결을 걸러내지
        못했던 걸 주문ID 비교로 고쳤다."""
        journal_path, state_path = self._paths(tmp_path, monkeypatch)
        bot.append_entry({
            "symbol": self.SYMBOL, "event": "entered", "entry_price": 65023.89,
            "stop_loss_price": 65836.688625, "take_profit_price": 63398.29275,
            "execution": {"entry_order": {"id": "28535168719"}},
        }, path=journal_path)

        client = MagicMock()
        client.fetch_my_trades.return_value = [
            {"id": "t-entry", "price": 65061.0, "amount": 0.1228, "info": {"realizedPnl": "0", "orderId": 28535168719}},
        ]

        result = bot.check_and_log_closed_trade(client, self.SYMBOL)
        assert result is None
