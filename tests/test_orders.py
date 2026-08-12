from unittest.mock import MagicMock

import pytest

from src.execution.orders import execute_order


def test_execute_order_invalid_side_raises():
    with pytest.raises(ValueError):
        execute_order(MagicMock(), "BTC/USDT", "hold", 1.0)


def test_execute_order_zero_quantity_is_skipped_without_calling_client():
    mock_client = MagicMock()
    result = execute_order(mock_client, "BTC/USDT", "buy", 0)
    assert result["status"] == "skipped"
    mock_client.create_order.assert_not_called()


def test_execute_order_calls_client_on_testnet(monkeypatch):
    monkeypatch.setattr("src.execution.orders.USE_TESTNET", True)
    mock_client = MagicMock()
    mock_client.create_order.return_value = {"id": "123"}

    result = execute_order(mock_client, "BTC/USDT", "buy", 0.01)

    assert result["status"] == "filled"
    mock_client.create_order.assert_called_once_with("BTC/USDT", type="market", side="buy", amount=0.01)


def test_execute_order_blocks_live_mode_without_confirmation(monkeypatch):
    monkeypatch.setattr("src.execution.orders.USE_TESTNET", False)
    mock_client = MagicMock()

    with pytest.raises(RuntimeError):
        execute_order(mock_client, "BTC/USDT", "buy", 0.01)
    mock_client.create_order.assert_not_called()


def test_execute_order_allows_live_mode_with_explicit_confirmation(monkeypatch):
    monkeypatch.setattr("src.execution.orders.USE_TESTNET", False)
    mock_client = MagicMock()
    mock_client.create_order.return_value = {"id": "456"}

    result = execute_order(mock_client, "BTC/USDT", "sell", 0.01, confirm_live=True)
    assert result["status"] == "filled"
