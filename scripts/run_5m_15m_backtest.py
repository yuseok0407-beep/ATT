"""5분봉/15분봉으로 낮췄을 때(+ 고배율-분봉 전략 아이디어의 전제 검증) 어떻게 되는지 확인.

주의: 신호 계산에 쓰는 캔들 개수(SIGNAL_LOOKBACK_BARS=100)는 타임프레임과 무관하게 고정
(실거래 봇의 _evaluate_symbol이 timeframe만 바꾸고 limit=100은 그대로 두는 것과 동일하게 재현) —
그래서 5분봉에서는 지표가 보는 실제 시간 범위가 1시간봉보다 훨씬 짧아진다(약 8시간 분량).
5분봉은 봉 개수가 매우 많아 계산/조회량이 커서 기간을 90일(3개월)로 줄여서 돈다.
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

FEE_PCT_PER_SIDE = 0.0004

# (타임프레임, 조회 기간) — 저시간봉일수록 봉 개수가 급증해 조회/계산 시간이 길어지므로 기간을 줄인다.
TIMEFRAMES = [
    ("5m", 90),
    ("15m", 180),
]

VARIANTS = {
    "현재배포(ADX30+SMA10)": dict(adx_threshold=30, sma_period=10),
    "이전(ADX25+SMA20)": dict(adx_threshold=25, sma_period=20),
}


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    client = get_futures_market_data_client()

    for timeframe, days in TIMEFRAMES:
        aggregate = {name: [] for name in VARIANTS}
        print(f"\n########## {timeframe} ({days}일) ##########")

        for symbol in FUTURES_SYMBOLS:
            df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
            if len(df) < 200:
                print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
                continue
            print(f"\n=== {symbol} ({len(df)}봉) ===")
            print(f"  {'변형':<22}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(봉)':>12}")
            for name, params in VARIANTS.items():
                trades = run_backtest(
                    df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                    require_rsi_confirm=True, fee_pct_per_side=FEE_PCT_PER_SIDE, **params,
                )
                stats = summarize(trades)
                aggregate[name].append(stats)
                print(f"  {name:<22}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                      f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                      f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

        print(f"\n--- {timeframe} 전체 종목 합산 ---")
        print(f"  {'변형':<22}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
        for name, per_symbol_stats in aggregate.items():
            n = sum(s["num_trades"] for s in per_symbol_stats)
            total_r = sum(s["total_r"] for s in per_symbol_stats)
            wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
            win_rate = wins / n if n else None
            avg_r = total_r / n if n else None
            print(f"  {name:<22}{n:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


if __name__ == "__main__":
    main()
