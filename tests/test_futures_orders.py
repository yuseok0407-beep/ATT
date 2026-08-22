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


def test_open_position_long_places_entry_and_reduceonly_stop():
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}]

    result = open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000)

    assert result["status"] == "opened"
    mock_client.create_order.assert_has_calls([
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02),
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02,
             params={"stopLossPrice": 64000, "reduceOnly": True}),
    ])


def test_open_position_short_places_entry_sell_and_stop_buy():
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}]

    open_position(mock_client, "BTC/USDT:USDT", "short", 0.02, 66000)

    mock_client.create_order.assert_has_calls([
        call("BTC/USDT:USDT", type="market", side="sell", amount=0.02),
        call("BTC/USDT:USDT", type="market", side="buy", amount=0.02,
             params={"stopLossPrice": 66000, "reduceOnly": True}),
    ])


def test_open_position_blocks_live_mode_without_confirmation():
    mock_client = MagicMock()

    with pytest.raises(RuntimeError):
        open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000, env="live")
    mock_client.create_order.assert_not_called()


def test_close_position_places_reduceonly_market_order_and_cancels_stops():
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


def test_close_position_blocks_live_mode_without_confirmation():
    mock_client = MagicMock()

    with pytest.raises(RuntimeError):
        close_position(mock_client, "BTC/USDT:USDT", "long", 0.02, env="live")
    mock_client.create_order.assert_not_called()


def test_open_position_allows_live_mode_with_explicit_confirmation():
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [{"id": "entry"}, {"id": "stop"}]

    result = open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000,
                            env="live", confirm_live=True)
    assert result["status"] == "opened"


def test_open_position_closes_naked_position_and_reraises_when_stop_order_fails():
    """실전 버그 재현(2026-08-22): 체결 사이 급격한 가격 변동으로 손절 주문이 -2021 "Order would
    immediately trigger"로 거부되면, 진입은 이미 체결됐는데 손절 없는 포지션이 그대로 남았다
    (ETH/SOL 숏 진입에서 실제 확인). 이제는 즉시 청산하고 원래 예외를 그대로 올려야 한다."""
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [
        {"id": "entry"},
        Exception('binance {"code":-2021,"msg":"Order would immediately trigger."}'),
        {"id": "close"},
    ]

    with pytest.raises(Exception, match="-2021"):
        open_position(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000)

    # 세 번째 create_order 호출이 reduceOnly 청산이어야 한다(진입=buy이므로 청산은 손절과 같은 sell)
    assert mock_client.create_order.call_args_list[2] == call(
        "BTC/USDT:USDT", type="market", side="sell", amount=0.02, params={"reduceOnly": True}
    )
    assert mock_client.cancel_all_orders.call_count == 2  # cleanup_stale_orders(일반 + trigger)


def test_open_position_with_bracket_closes_naked_position_when_stop_order_fails():
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [
        {"id": "entry"},
        Exception('binance {"code":-2021,"msg":"Order would immediately trigger."}'),
        {"id": "close"},
    ]

    with pytest.raises(Exception, match="-2021"):
        open_position_with_bracket(mock_client, "BTC/USDT:USDT", "short", 0.02, 66000, 63500)

    # short 진입(sell)이므로 청산은 반대(buy)
    assert mock_client.create_order.call_args_list[2] == call(
        "BTC/USDT:USDT", type="market", side="buy", amount=0.02, params={"reduceOnly": True}
    )
    assert mock_client.cancel_all_orders.call_count == 2


def test_open_position_with_bracket_closes_naked_position_when_take_profit_order_fails():
    """손절은 성공했는데 익절만 실패해도(둘 다 실패한 게 아니라 부분 실패) 마찬가지로 즉시
    청산해야 한다 — 방치하면 손절만 걸린 반쪽짜리 보호 상태로 남는다."""
    mock_client = MagicMock()
    mock_client.create_order.side_effect = [
        {"id": "entry"},
        {"id": "stop"},
        Exception('binance {"code":-2021,"msg":"Order would immediately trigger."}'),
        {"id": "close"},
    ]

    with pytest.raises(Exception, match="-2021"):
        open_position_with_bracket(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000, 65500)

    assert mock_client.create_order.call_args_list[3] == call(
        "BTC/USDT:USDT", type="market", side="sell", amount=0.02, params={"reduceOnly": True}
    )
    assert mock_client.cancel_all_orders.call_count == 2


def test_cleanup_stale_orders_cancels_both_regular_and_conditional():
    mock_client = MagicMock()
    cleanup_stale_orders(mock_client, "BTC/USDT:USDT")
    mock_client.cancel_all_orders.assert_any_call("BTC/USDT:USDT")
    mock_client.cancel_all_orders.assert_any_call("BTC/USDT:USDT", params={"trigger": True})
    assert mock_client.cancel_all_orders.call_count == 2


def test_open_position_with_bracket_places_entry_stop_and_take_profit():
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


def test_open_position_with_bracket_short_uses_buy_side_for_exits():
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


def test_open_position_with_bracket_blocks_live_mode_without_confirmation():
    mock_client = MagicMock()
    with pytest.raises(RuntimeError):
        open_position_with_bracket(mock_client, "BTC/USDT:USDT", "long", 0.02, 64000, 65500, env="live")
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
