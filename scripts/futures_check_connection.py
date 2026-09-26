import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import LEVERAGE, MARGIN_MODE
from src.data.futures_exchange import get_futures_balance, get_futures_client, get_position

if __name__ == "__main__":
    # 데모/실계좌는 전역 설정이 아니라 env로 정해진다(2026-08-22) — 점검 스크립트도 어느 계좌를
    # 보고 있는지 명시적으로 고르게 한다. 예전엔 USE_TESTNET(현물 시절 설정)을 찍고 있었는데
    # 선물에는 아무 영향이 없는 값이라 오히려 "테스트넷이구나"로 오해할 수 있었다.
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["demo", "live", "demo2"], default="demo")
    env = parser.parse_args().env

    print(f"Account: {env} ({'실제 자금' if env == 'live' else '데모 트레이딩'})")
    # demo2는 같은 계정의 데모와 다른 계좌여야 한다 — 두 키가 같은 계좌를 가리키면 잔고가 같게 나온다.
    print(f"Leverage: {LEVERAGE}x, Margin mode: {MARGIN_MODE}")

    client = get_futures_client(env)

    balance = get_futures_balance(client)
    usdt = balance.get("USDT", {})
    print(f"USDT margin balance: {usdt}")

    position = get_position(client, "BTC/USDT:USDT")
    print(f"Current BTC/USDT position: {position}")
