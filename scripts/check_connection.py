import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import USE_TESTNET
from src.data.exchange import get_balance, get_client, get_ticker

if __name__ == "__main__":
    print(f"Testnet mode: {USE_TESTNET}")
    client = get_client()

    ticker = get_ticker(client, "BTC/USDT")
    print(f"BTC/USDT last price: {ticker['last']}")

    balance = get_balance(client)
    usdt = balance.get("USDT", {})
    print(f"USDT balance: {usdt}")
