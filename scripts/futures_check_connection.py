import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import LEVERAGE, MARGIN_MODE, USE_TESTNET
from src.data.futures_exchange import get_futures_balance, get_futures_client, get_position

if __name__ == "__main__":
    print(f"Testnet mode: {USE_TESTNET}")
    print(f"Leverage: {LEVERAGE}x, Margin mode: {MARGIN_MODE}")

    client = get_futures_client()

    balance = get_futures_balance(client)
    usdt = balance.get("USDT", {})
    print(f"USDT margin balance: {usdt}")

    position = get_position(client, "BTC/USDT:USDT")
    print(f"Current BTC/USDT position: {position}")
