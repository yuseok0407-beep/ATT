from unittest.mock import MagicMock

import ccxt
import pytest

from src.data.futures_exchange import set_margin_mode


def test_set_margin_mode_calls_through_on_success():
    mock_client = MagicMock()
    set_margin_mode(mock_client, "BTC/USDT:USDT", "isolated")
    mock_client.set_margin_mode.assert_called_once_with("isolated", "BTC/USDT:USDT")


def test_set_margin_mode_ignores_already_set_error():
    mock_client = MagicMock()
    mock_client.set_margin_mode.side_effect = ccxt.ExchangeError("No need to change margin type.")
    set_margin_mode(mock_client, "BTC/USDT:USDT", "isolated")  # 예외 없이 통과해야 함


def test_set_margin_mode_ignores_open_orders_error():
    # 실전에서 실제로 발생한 케이스: 이미 포지션/주문이 있는 종목에 재시작마다 마진모드를
    # 다시 설정하려 하면 바이낸스가 이 에러로 거부한다 — 무시해도 안전해야 한다.
    mock_client = MagicMock()
    mock_client.set_margin_mode.side_effect = ccxt.OperationRejected(
        'binance {"code":-4067,"msg":"Position side cannot be changed if there exists open orders."}'
    )
    set_margin_mode(mock_client, "ETH/USDT:USDT", "isolated")  # 예외 없이 통과해야 함


def test_set_margin_mode_reraises_unrelated_errors():
    mock_client = MagicMock()
    mock_client.set_margin_mode.side_effect = ccxt.ExchangeError("some unrelated failure")
    with pytest.raises(ccxt.ExchangeError):
        set_margin_mode(mock_client, "BTC/USDT:USDT", "isolated")
