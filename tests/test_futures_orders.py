from unittest.mock import MagicMock, call

import pytest

from src.execution.futures_orders import (
    cleanup_stale_orders,
    close_position,
    get_bracket_prices,
    open_position,
    open_position_with_bracket,
)


def test_open_position_invalid_side_raises():
    with pytest.raises(ValueError):
        open_position(MagicMock(), "BTC/USDT:USDT", "up", 0.01, 64000)


def test_open_position_zero_quantity_skips_without_calling_client():
    mock_client = MagicMock()
    result = open_position(mock_client, "BTC/USDT:USDT", "long", 0, 64000)
    assert result["status"] == "skipped"
    mock_client.create_order.assert_not_called()


def test_open_position_long_places_entry_and_reduceonly_stop(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", True)
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}]

    result = open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000)

    assert result["status"] == "opened"
    mock_client.create_order.assert_has_calls([
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02),
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02,
             params={"stopLossPrice": 64000, "reduceOnly": True}),
    ])


def test_open_position_short_places_entry_sell_and_stop_buy(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", True)
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}]

    open_position(mock_client, "BTC/USDT:USDT", "short", 0.02, 66000)

    mock_client.create_order.assert_has_calls([
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02),
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02,
             params={"stopLossPrice": 66000, "reduceOnly": True}),
    ])


def test_open_position_blocks_live_mode_without_confirmation(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", False)
    mock_client = MagicMock()

    with pytest.raises(RuntimeError):
        open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000)
    mock_client.create_order.assert_not_called()


def test_close_position_places_reduceonly_market_order_and_cancels_stops(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", True)
    mock_client = MagicMock()
    mock_client.create_order.return_value = {"id": "close"}

    result = close_position(mock_client, "BTC/USDT:USDT", "long", 0.02)

    assert result["status"] == "closed"
    mock_client.create_order.assert_called_once_with(
        "BTC/USDT:USDT", type="market", side="sell", amount=0.02, params={"reduceOnly": True}
    )
    # 일반 주문과 조건부(algo/stop) 주문은 바이낸스에서 별개 취소 엔드포인트를 쓰므로 둘 다 취소해야 한다.
    mock_client.cancel_all_orders.assert_any_call("BTC/USDT:USDT")
    mock_client.cancel_all_orders.assert_any_call("BTC/USDT:USDT", params={"trigger": True})
    assert mock_client.cancel_all_orders.call_count == 2


def test_close_position_blocks_live_mode_without_confirmation(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", False)
    mock_client = MagicMock()

    with pytest.raises(RuntimeError):
        close_position(mock_client, "BTC/USDT:USDT", "long", 0.02)
    mock_client.create_order.assert_not_called()


def test_open_position_allows_live_mode_with_explicit_confirmation(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", False)
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}]

    result = open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000, confirm_live=True)
    assert result["status"] == "opened"


def test_cleanup_stale_orders_cancels_both_regular_and_conditional():
    mock_client = MagicMock()
    cleanup_stale_orders(mock_client, "BTC/USDT:USDT")
    mock_client.cancel_all_orders.assert_any_call("BTC/USDT:USDT")
    mock_client.cancel_all_orders.assert_any_call("BTC/USDT:USDT", params={"trigger": True})
    assert mock_client.cancel_all_orders.call_count == 2


def test_open_position_with_bracket_places_entry_stop_and_take_profit(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", True)
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}, {"id": "tp"}]

    result = open_position_with_bracket(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000, 65500)

    assert result["status"] == "opened"
    assert result["stop_order"]["id"] == "stop"
    assert result["take_profit_order"]["id"] == "tp"
    mock_client.create_order.assert_has_calls([
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02),
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02,
             params={"stopLossPrice": 64000, "reduceOnly": True}),
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02,
             params={"takeProfitPrice": 65500, "reduceOnly": True}),
    ])


def test_open_position_with_bracket_short_uses_buy_side_for_exits(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", True)
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}, {"id": "tp"}]

    open_position_with_bracket(mock_client, "BTC/USDT:USDT", "short", 0.02, 66000, 63500)

    mock_client.create_order.assert_has_calls([
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02),
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02,
             params={"stopLossPrice": 66000, "reduceOnly": True}),
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02,
             params={"takeProfitPrice": 63500, "reduceOnly": True}),
    ])


def test_open_position_with_bracket_zero_quantity_skips():
    mock_client = MagicMock()
    result = open_position_with_bracket(mock_client, "BTC/USDT:USDT", "long", 0, 64000, 65500)
    assert result["status"] == "skipped"
    mock_client.create_order.assert_not_called()


def test_open_position_with_bracket_blocks_live_mode_without_confirmation(monkeypatch):
    monkeypatch.setattr("src.execution.futures_orders.USE_TESTNET", False)
    mock_client = MagicMock()
    with pytest.raises(RuntimeError):
        open_position_with_bracket(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000, 65500)
    mock_client.create_order.assert_not_called()


def test_get_bracket_prices_finds_stop_and_take_profit():
    mock_client = MagicMock()
    mock_client.fetch_open_orders.return_value = [
        {"info": {"orderType": "STOP_MARKET"}, "triggerPrice": 64000},
        {"info": {"orderType": "TAKE_PROFIT_MARKET"}, "triggerPrice": 66000},
    ]
    stop, take_profit = get_bracket_prices(mock_client, "BTC/USDT:USDT")
    assert stop == 64000
    assert take_profit == 66000
    mock_client.fetch_open_orders.assert_called_once_with("BTC/USDT:USDT", params={"trigger": True})


def test_get_bracket_prices_no_matching_orders_returns_none_none():
    mock_client = MagicMock()
    mock_client.fetch_open_orders.return_value = []
    assert get_bracket_prices(mock_client, "BTC/USDT:USDT") == (None, None)


def test_get_bracket_prices_swallows_fetch_errors():
    mock_client = MagicMock()
    mock_client.fetch_open_orders.side_effect = Exception("network error")
    assert get_bracket_prices(mock_client, "BTC/USDT:USDT") == (None, None)
