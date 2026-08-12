"""15분봉처럼 1시간보다 낮은 시간단위로 바꾸면 어떻게 되는지 확인.

주의: 신호 계산에 쓰는 캔들 개수(SIGNAL_LOOKBACK_BARS=100)는 타임프레임과 무관하게 고정
(실거래 봇의 _evaluate_symbol이 timeframe만 바꾸고 limit=100은 그대로 두는 것과 동일하게 재현) —
그래서 15분봉에서는 지표가 보는 실제 시간 범위가 1시간봉보다 훨씬 짧아진다(약 25시간 분량).
15분봉은 봉 개수가 많아 계산량이 커서 기간을 180일(6개월)로 줄여서 돈다.
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
from src.core.config import FUTURES_SYMBOLS, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.data.futures_exchange import get_futures_market_data_client

BACKTEST_DAYS = 180
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


def main():
    client = get_futures_market_data_client()
    timeframe = "15m"
    aggregate = {name: [] for name in VARIANTS}

    print(f"\n########## 15분봉 ({BACKTEST_DAYS}일) ##########")
    for symbol in FUTURES_SYMBOLS:
        df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=BACKTEST_DAYS)
        if len(df) < 200:
            print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
            continue
        print(f"\n=== {symbol} ({len(df)}봉) ===")
        print(f"  {'변형':<20}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}")
        for name, params in VARIANTS.items():
            trades = run_backtest(
                df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                require_rsi_confirm=True, fee_pct_per_side=FEE_PCT_PER_SIDE, **params,
            )
            stats = summarize(trades)
            aggregate[name].append(stats)
            print(f"  {name:<20}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                  f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}")

    print("\n--- 15분봉 전체 종목 합산 ---")
    print(f"  {'변형':<20}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
    for name, per_symbol_stats in aggregate.items():
        n = sum(s["num_trades"] for s in per_symbol_stats)
        total_r = sum(s["total_r"] for s in per_symbol_stats)
        wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
        win_rate = wins / n if n else None
        avg_r = total_r / n if n else None
        print(f"  {name:<20}{n:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


if __name__ == "__main__":
    main()
