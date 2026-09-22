"""지금 실거래 중인 진입/청산 로직(ADX30+SMA10, 1시간봉, 고정손절+RR2.0)을 그대로 두고, 종목만
넓혀서 이 전략이 실제로 먹히는 심볼을 찾는 스크리닝. signal_fn을 넘기지 않아 config의
RULE_ADX_THRESHOLD/STOP_LOSS_PCT/TAKE_PROFIT_RR을 그대로 쓴다(sma_period만 하드코딩 기본값
트랩이 있어 명시로 넘김 — UPDATE_LOG.md 2026-08-11 참고).

후보 종목:
  - 크립토: 데모 트레이딩 계좌에서 실제로 거래 가능한 USDT 무기한선물 중 24h 거래대금 상위
    TOP_N_BY_VOLUME개(레버리지 토큰/스테이블코인 페어/주식형 심볼 제외)
  - "주식형" 심볼: 바이낸스가 무기한선물로 상장한 토큰화 미국주식/ETF(TSLA, NVDA, COIN, MSTR,
    GOOGL, AMZN, META, QQQ, SPY, AAPL, MSFT, SOXL, SOXS, CRCL, HOOD) 중 데모 트레이딩 계좌에
    실제로 있는 것만(2026-08-12 확인 결과 AAPL/MSFT/SOXL/SOXS/IBIT/GLXY는 시세 데이터엔 있어도
    데모 트레이딩 계좌엔 없었음 — 이 경우 자동으로 후보에서 빠진다)

실주문 없이 공개 시세만 조회한다."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from src.backtest.data import fetch_historical_ohlcv
from src.backtest.engine import run_backtest
from src.backtest.report import summarize
from src.core.config import FEE_PCT_PER_SIDE, RULE_SMA_PERIOD
from src.data.futures_exchange import get_futures_client, get_futures_market_data_client

TIMEFRAME = "1h"
DAYS = 365
TOP_N_BY_VOLUME = 25
MIN_BARS = 200

EQUITY_TICKERS = [
    "TSLA", "NVDA", "COIN", "MSTR", "GOOGL", "AMZN", "META", "QQQ", "SPY",
    "AAPL", "MSFT", "SOXL", "SOXS", "CRCL", "HOOD",
]
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI"}
LEVERAGED_TOKEN_MARKERS = ("UP", "DOWN", "BULL", "BEAR")


def _build_candidates(market_client, trading_client):
    market_client.load_markets()
    trading_client.load_markets()
    tradeable = set(trading_client.markets)

    perps = [
        m for m in market_client.markets.values()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
    ]
    tickers = market_client.fetch_tickers()

    def is_leveraged_token(base):
        return any(marker in base for marker in LEVERAGED_TOKEN_MARKERS)

    crypto = [
        m["symbol"] for m in perps
        if m["symbol"] in tradeable
        and m["base"] not in STABLE_BASES
        and m["base"] not in EQUITY_TICKERS
        and not is_leveraged_token(m["base"])
    ]
    crypto_ranked = sorted(
        crypto, key=lambda s: (tickers.get(s) or {}).get("quoteVolume") or 0, reverse=True,
    )
    top_crypto = [(s, "crypto") for s in crypto_ranked[:TOP_N_BY_VOLUME]]

    equity = [
        (f"{t}/USDT:USDT", "equity") for t in EQUITY_TICKERS
        if f"{t}/USDT:USDT" in tradeable
    ]
    skipped_equity = [t for t in EQUITY_TICKERS if f"{t}/USDT:USDT" not in tradeable]
    if skipped_equity:
        print(f"(데모 계좌에 없어 제외된 주식형 심볼: {', '.join(skipped_equity)})\n")

    return top_crypto + equity


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    market_client = get_futures_market_data_client()
    trading_client = get_futures_client()
    candidates = _build_candidates(market_client, trading_client)
    print(f"스크리닝 대상 {len(candidates)}종목 ({TIMEFRAME}, {DAYS}일)\n")

    results = []
    for symbol, category in candidates:
        df = fetch_historical_ohlcv(market_client, symbol, timeframe=TIMEFRAME, days=DAYS)
        if len(df) < MIN_BARS:
            print(f"{symbol:<16}[{category}] 데이터 부족(봉 {len(df)}개) — 건너뜀")
            continue
        trades = run_backtest(df, fee_pct_per_side=FEE_PCT_PER_SIDE, sma_period=RULE_SMA_PERIOD)
        stats = summarize(trades)
        stats["symbol"] = symbol
        stats["category"] = category
        stats["num_bars"] = len(df)
        results.append(stats)
        print(f"{symbol:<16}[{category}] 봉{len(df):>5}  거래{stats['num_trades']:>3}  "
              f"승률{_fmt(stats['win_rate'], pct=True):>7}  평균R{_fmt(stats['avg_r']):>7}  "
              f"총R{_fmt(stats['total_r']):>8}  최대DD(R){_fmt(stats['max_drawdown_r']):>8}")

    results_with_trades = [r for r in results if r["num_trades"] > 0]
    print(f"\n{'='*90}\n총R 기준 정렬 (거래 1건 이상)\n{'='*90}")
    print(f"{'심볼':<16}{'구분':<8}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}")
    for r in sorted(results_with_trades, key=lambda r: r["total_r"], reverse=True):
        print(f"{r['symbol']:<16}{r['category']:<8}{r['num_trades']:>6}"
              f"{_fmt(r['win_rate'], pct=True):>8}{_fmt(r['avg_r']):>8}{_fmt(r['total_r']):>9}"
              f"{_fmt(r['max_drawdown_r']):>11}")

    total_trades = sum(r["num_trades"] for r in results)
    total_r = sum(r["total_r"] for r in results)
    print(f"\n전체 합산: {len(results)}종목 스크리닝, {total_trades}건 거래, 총R={_fmt(total_r)}")


if __name__ == "__main__":
    main()
