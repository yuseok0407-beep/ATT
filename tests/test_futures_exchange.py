from unittest.mock import MagicMock

import ccxt
import pytest

from src.data.futures_exchange import get_max_leverage, get_notional_cap, set_margin_mode


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


def test_get_max_leverage_returns_highest_tier():
    mock_client = MagicMock()
    mock_client.fetch_leverage_tiers.return_value = {
        "TSLA/USDT:USDT": [
            {"maxLeverage": 5.0}, {"maxLeverage": 3.0}, {"maxLeverage": 1.0},
        ],
    }
    assert get_max_leverage(mock_client, "TSLA/USDT:USDT") == 5
    mock_client.fetch_leverage_tiers.assert_called_once_with(["TSLA/USDT:USDT"])


def test_get_max_leverage_returns_zero_when_symbol_missing():
    mock_client = MagicMock()
    mock_client.fetch_leverage_tiers.return_value = {}
    assert get_max_leverage(mock_client, "TSLA/USDT:USDT") == 0


_TSLA_TIERS = {
    "TSLA/USDT:USDT": [
        {"maxLeverage": 5.0, "maxNotional": 5_000.0},
        {"maxLeverage": 4.0, "maxNotional": 10_000.0},
        {"maxLeverage": 3.0, "maxNotional": 30_000.0},
        {"maxLeverage": 2.0, "maxNotional": 80_000.0},
        {"maxLeverage": 1.0, "maxNotional": 200_000.0},
    ],
}


def test_get_notional_cap_returns_the_matching_tiers_own_cap():
    """실전 확인된 실제 TSLA 티어: 5배에서 허용되는 구간은 tier1뿐이라 cap도 tier1의 $5000이어야
    한다(2026-08-18, -2027 오류 원인 진단 과정에서 확인)."""
    mock_client = MagicMock()
    mock_client.fetch_leverage_tiers.return_value = _TSLA_TIERS
    assert get_notional_cap(mock_client, "TSLA/USDT:USDT", leverage=5) == 5_000.0
    mock_client.fetch_leverage_tiers.assert_called_once_with(["TSLA/USDT:USDT"])


def test_get_notional_cap_uses_the_largest_notional_still_compatible_with_the_leverage():
    """레버리지 구간은 명목가치가 커질수록 허용 레버리지가 단조 감소한다 — 낮은 레버리지를
    쓴다면 더 높은 구간(더 큰 cap)까지 그 레버리지가 여전히 허용되므로, 해당하는 구간들 중
    가장 큰 cap을 골라야 한다(가장 작은 cap을 고르면 지나치게 보수적으로 과소평가하게 됨)."""
    mock_client = MagicMock()
    mock_client.fetch_leverage_tiers.return_value = _TSLA_TIERS
    assert get_notional_cap(mock_client, "TSLA/USDT:USDT", leverage=2) == 80_000.0


def test_get_notional_cap_returns_none_when_no_tier_supports_the_leverage():
    mock_client = MagicMock()
    mock_client.fetch_leverage_tiers.return_value = _TSLA_TIERS
    assert get_notional_cap(mock_client, "TSLA/USDT:USDT", leverage=10) is None


def test_get_notional_cap_returns_none_when_symbol_missing():
    mock_client = MagicMock()
    mock_client.fetch_leverage_tiers.return_value = {}
    assert get_notional_cap(mock_client, "TSLA/USDT:USDT", leverage=5) is None
