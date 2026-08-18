import ccxt

from src.core.config import (
    BINANCE_FUTURES_API_KEY,
    BINANCE_FUTURES_API_SECRET,
    MARGIN_MODE,
    USE_TESTNET,
)


def get_futures_client() -> ccxt.binance:
    """USDT-M 선물 계좌/주문 전용 클라이언트.

    바이낸스가 예전 독립 테스트넷(testnet.binancefuture.com, GitHub 로그인)을 폐지하고
    실제 계정으로 로그인해 쓰는 "Demo Trading"(demo.binance.com)으로 통합했다. 그래서
    현물처럼 set_sandbox_mode()가 아니라 enable_demo_trading()을 써야 한다 — 키는
    실제 바이낸스 계정으로 로그인 후 demo.binance.com/en/my/settings/api-management 에서
    발급받은 데모 트레이딩 전용 키(BINANCE_FUTURES_API_KEY/SECRET)."""
    client = ccxt.binance({
        "apiKey": BINANCE_FUTURES_API_KEY,
        "secret": BINANCE_FUTURES_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    if USE_TESTNET:
        client.enable_demo_trading(True)
    return client


def get_futures_market_data_client() -> ccxt.binance:
    """선물 시세/캔들 조회 전용 클라이언트 (키 없이 공개 데이터만). 지표 계산은 반드시 이걸로
    해야 한다 — SOXL처럼 바이낸스 현물에는 없고 선물에만 있는 심볼도 있어서, 현물 클라이언트로는
    조회 자체가 실패한다."""
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})


def get_futures_balance(client: ccxt.binance) -> dict:
    return client.fetch_balance()


def get_position(client: ccxt.binance, symbol: str) -> dict | None:
    """해당 심볼의 현재 보유 포지션을 반환한다. 포지션이 없으면 None."""
    positions = client.fetch_positions([symbol])
    for position in positions:
        contracts = position.get("contracts") or 0
        if contracts != 0:
            return position
    return None


def set_leverage(client: ccxt.binance, symbol: str, leverage: int) -> None:
    client.set_leverage(leverage, symbol)


def get_max_leverage(client: ccxt.binance, symbol: str) -> int:
    """이 심볼에 거래소가 허용하는 최대 레버리지. 심볼마다 다르다(예: BTC 125x, TSLA 5x —
    2026-08-12 실전 확인, 토큰화 주식형 심볼일수록 낮은 편)."""
    tiers = client.fetch_leverage_tiers([symbol])
    symbol_tiers = tiers.get(symbol) or []
    return int(max((t.get("maxLeverage") or 0) for t in symbol_tiers)) if symbol_tiers else 0


def get_notional_cap(client: ccxt.binance, symbol: str, leverage: int) -> float | None:
    """지금 leverage로 이 심볼을 거래할 때 거래소가 허용하는 최대 포지션 명목가치(USDT).

    바이낸스는 레버리지 구간(bracket)마다 별도 notional cap을 둔다 — 구간이 올라갈수록(=명목
    가치가 커질수록) 허용 레버리지는 단조 감소한다(예: TSLA는 tier1에서만 5배가 허용되고 cap이
    $5000, 그 이상 명목가치를 쓰려면 레버리지를 4배 이하로 낮춰야 함). `leveraged_position_size`의
    리스크 기반 수량 계산은 이 상한을 몰라서, 계좌 자산이 커지면(리스크 비율이 고정이라 계좌가
    크면 명목가치도 커짐) 이 cap을 넘는 주문을 시도해 거래소가 `-2027 Exceeded the maximum
    allowable position at current leverage`로 거부하는 사고가 실제 있었다(2026-08-18, TSLA).

    leverage 이상을 허용하는 구간들 중 가장 큰 notional cap을 돌려준다 — 구간이 단조 감소이므로
    이는 "이 leverage를 유지한 채 낼 수 있는 최대 명목가치"와 같다. 해당 구간을 못 찾으면 None
    (호출자는 이 상한을 무시하고 기존 로직대로 계산해야 한다)."""
    tiers = client.fetch_leverage_tiers([symbol])
    symbol_tiers = tiers.get(symbol) or []
    matching = [t for t in symbol_tiers if (t.get("maxLeverage") or 0) >= leverage]
    if not matching:
        return None
    return max(t["maxNotional"] for t in matching)


_MARGIN_MODE_ALREADY_OK_MESSAGES = (
    "No need to change margin type",  # 이미 같은 마진 모드로 설정된 경우
    "Position side cannot be changed if there exists open orders",  # 이미 포지션/주문이 있어 변경 불가한 경우
)


def set_margin_mode(client: ccxt.binance, symbol: str, mode: str = MARGIN_MODE) -> None:
    """봇 시작 시마다 감시 종목 전체에 대해 호출되므로, 이미 포지션·주문이 있어 변경이
    거부되는 경우까지 안전하게 무시해야 재시작할 때마다 죽지 않는다. 두 경우 다 "이미 원하는
    상태(또는 우리가 건드릴 수 없는 상태)"라는 뜻이라 무시해도 실제 리스크는 없다."""
    try:
        client.set_margin_mode(mode, symbol)
    except ccxt.ExchangeError as exc:
        if not any(msg in str(exc) for msg in _MARGIN_MODE_ALREADY_OK_MESSAGES):
            raise
