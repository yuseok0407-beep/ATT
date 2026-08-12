"""ADX30+SMA10 조합이 "지난 1년 전체로 고른 파라미터"라 과최적화일 위험이 있는지 검증한다.
1년치 데이터를 전반/후반으로 나눠서 각각 독립적으로 백테스트 — 양쪽 반 모두에서 baseline보다
나으면 특정 구간에만 우연히 맞은 게 아니라는 근거가 된다. 실주문 없이 공개 시세만 조회한다."""
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
from src.core.config import FUTURES_SYMBOLS, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.data.futures_exchange import get_futures_market_data_client

BACKTEST_DAYS = 365
FEE_PCT_PER_SIDE = 0.0004

VARIANTS = {
    "현재(ADX25+SMA20)": dict(adx_threshold=25, sma_period=20),
    "ADX30+SMA10": dict(adx_threshold=30, sma_period=10),
}


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def _aggregate_print(label, per_symbol_by_variant):
    print(f"  --- {label} 합산 ---")
    print(f"  {'변형':<20}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
    for name, per_symbol_stats in per_symbol_by_variant.items():
        n = sum(s["num_trades"] for s in per_symbol_stats)
        total_r = sum(s["total_r"] for s in per_symbol_stats)
        wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
        win_rate = wins / n if n else None
        avg_r = total_r / n if n else None
        print(f"  {name:<20}{n:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


def run_timeframe(timeframe: str, label: str):
    client = get_futures_market_data_client()
    first_half = {name: [] for name in VARIANTS}
    second_half = {name: [] for name in VARIANTS}

    print(f"\n########## {label} ({timeframe}) — 전반/후반 아웃오브샘플 ##########")
    for symbol in FUTURES_SYMBOLS:
        df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=BACKTEST_DAYS)
        if len(df) < 200:
            print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
            continue
        mid = len(df) // 2
        half1_df = df.iloc[:mid].reset_index(drop=True)
        half2_df = df.iloc[mid:].reset_index(drop=True)
        print(f"\n=== {symbol} === 전반 {half1_df['timestamp'].iloc[0]}~{half1_df['timestamp'].iloc[-1]} "
              f"({len(half1_df)}봉) / 후반 {half2_df['timestamp'].iloc[0]}~{half2_df['timestamp'].iloc[-1]} "
              f"({len(half2_df)}봉)")

        for name, params in VARIANTS.items():
            s1 = summarize(run_backtest(
                half1_df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                require_rsi_confirm=True, fee_pct_per_side=FEE_PCT_PER_SIDE, **params,
            ))
            s2 = summarize(run_backtest(
                half2_df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                require_rsi_confirm=True, fee_pct_per_side=FEE_PCT_PER_SIDE, **params,
            ))
            first_half[name].append(s1)
            second_half[name].append(s2)
            print(f"  {name:<20} 전반: 거래{s1['num_trades']:>4} 승률{_fmt(s1['win_rate'], pct=True):>6} "
                  f"총R{_fmt(s1['total_r']):>8}   |   후반: 거래{s2['num_trades']:>4} "
                  f"승률{_fmt(s2['win_rate'], pct=True):>6} 총R{_fmt(s2['total_r']):>8}")

    print()
    _aggregate_print(f"{label} 전반(6개월)", first_half)
    _aggregate_print(f"{label} 후반(6개월)", second_half)


def main():
    run_timeframe("1h", "1시간봉")
    run_timeframe("4h", "4시간봉")


if __name__ == "__main__":
    main()
