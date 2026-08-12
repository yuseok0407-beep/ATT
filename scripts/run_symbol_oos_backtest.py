"""2026-08-12 종목 스크리닝(run_symbol_screen_backtest.py)에서 총R이 양수였던 후보들이 특정
구간에서 우연히 잘 맞은 것뿐인지, 아니면 1년 내내 꾸준히 먹히는 엣지인지를 전반/후반
아웃오브샘플로 검증한다. 지금 실거래 중인 진입/청산 로직(ADX30+SMA10, 고정손절+RR2.0)을
그대로 쓴다. 양쪽 반 모두 총R이 양수여야 "PASS" — 한쪽만 양수면 그 구간에만 맞았던 것일 수
있다는 뜻이라 "FAIL"로 표시한다. 실주문 없이 공개 시세만 조회한다."""
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
from src.core.config import RULE_SMA_PERIOD
from src.data.futures_exchange import get_futures_market_data_client

TIMEFRAME = "1h"
DAYS = 365
FEE_PCT_PER_SIDE = 0.0004
MIN_BARS = 400  # 반으로 쪼갰을 때 한쪽이 최소 200봉은 되도록

# 2026-08-12 스크리닝에서 총R 양수 + 거래 30건 이상이었던 후보(SPCX/QQQ/SPY/KORU는 기간이
# 너무 짧아 반으로 쪼개면 통계적으로 무의미해서 제외) + 현재 실거래 중인 BTC/ETH는 비교 기준으로 포함.
CANDIDATES = [
    "BTC/USDT:USDT", "ETH/USDT:USDT",  # 현재 실거래 중 (기준선)
    "XRP/USDT:USDT", "SOL/USDT:USDT", "CRCL/USDT:USDT", "XAU/USDT:USDT",
    "ZEC/USDT:USDT", "TSLA/USDT:USDT", "BNB/USDT:USDT", "META/USDT:USDT",
]


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    client = get_futures_market_data_client()
    rows = []

    for symbol in CANDIDATES:
        df = fetch_historical_ohlcv(client, symbol, timeframe=TIMEFRAME, days=DAYS)
        if len(df) < MIN_BARS:
            print(f"{symbol:<16} 데이터 부족(봉 {len(df)}개, 최소 {MIN_BARS} 필요) — 건너뜀")
            continue

        mid = len(df) // 2
        half1_df = df.iloc[:mid].reset_index(drop=True)
        half2_df = df.iloc[mid:].reset_index(drop=True)

        s1 = summarize(run_backtest(half1_df, fee_pct_per_side=FEE_PCT_PER_SIDE, sma_period=RULE_SMA_PERIOD))
        s2 = summarize(run_backtest(half2_df, fee_pct_per_side=FEE_PCT_PER_SIDE, sma_period=RULE_SMA_PERIOD))
        full = summarize(run_backtest(df, fee_pct_per_side=FEE_PCT_PER_SIDE, sma_period=RULE_SMA_PERIOD))

        both_positive = s1["total_r"] > 0 and s2["total_r"] > 0
        verdict = "PASS" if both_positive else "FAIL"
        rows.append((symbol, s1, s2, full, verdict))

        print(f"\n=== {symbol} === 전반 {half1_df['timestamp'].iloc[0].date()}~{half1_df['timestamp'].iloc[-1].date()} "
              f"({len(half1_df)}봉) / 후반 {half2_df['timestamp'].iloc[0].date()}~{half2_df['timestamp'].iloc[-1].date()} "
              f"({len(half2_df)}봉) — [{verdict}]")
        print(f"  전반: 거래{s1['num_trades']:>4} 승률{_fmt(s1['win_rate'], pct=True):>7} 총R{_fmt(s1['total_r']):>8}   "
              f"|   후반: 거래{s2['num_trades']:>4} 승률{_fmt(s2['win_rate'], pct=True):>7} 총R{_fmt(s2['total_r']):>8}   "
              f"|   전체: 거래{full['num_trades']:>4} 총R{_fmt(full['total_r']):>8}")

    print(f"\n{'='*80}\n요약 (양쪽 반 모두 총R 양수 = PASS)\n{'='*80}")
    print(f"{'심볼':<16}{'전반 총R':>10}{'후반 총R':>10}{'전체 총R':>10}{'판정':>8}")
    for symbol, s1, s2, full, verdict in sorted(rows, key=lambda r: r[3]["total_r"], reverse=True):
        print(f"{symbol:<16}{_fmt(s1['total_r']):>10}{_fmt(s2['total_r']):>10}{_fmt(full['total_r']):>10}{verdict:>8}")


if __name__ == "__main__":
    main()
