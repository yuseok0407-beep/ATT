"""골든크로스/데드크로스(이동평균선 두 개의 교차) 전략이 실제로 통하는지 확인하는 백테스트.

지금 실거래 봇(가격이 이동평균선 "하나"를 돌파)과 달리 여기서는 빠른선/느린선 "둘"의 교차를 본다.
청산 규칙은 다른 백테스트들과 동일하게 고정 손절1.25%/손익비2.0을 그대로 써서 진입 로직만
바꿔치기했을 때의 순수한 효과를 본다.

주의: 느린선 기간(slow_period)은 신호 계산에 쓰는 캔들 개수(100개, SIGNAL_LOOKBACK_BARS)보다
충분히 작아야 한다 — 그래서 흔히 쓰이는 "골든크로스 50/200" 조합은 이 인프라(실거래 봇이 매
사이클 100개 캔들만 보는 구조)로는 애초에 테스트할 수 없다. 대신 타임프레임별로 실전에서 흔히
권장되는 조합(1h: 20/50, 15m: 15/40)과 더 빠른 EMA9/21을 비교한다. 실주문 없이 공개 시세만 조회."""
import sys
from functools import partial
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
from src.core.ma_cross_strategy import detect_signal as detect_ma_cross_signal
from src.data.futures_exchange import get_futures_market_data_client

FEE_PCT_PER_SIDE = 0.0004

# (타임프레임, 조회 기간, {변형이름: 파라미터})
TIMEFRAMES = [
    ("1h", 365, {
        "SMA20/50": dict(fast_period=20, slow_period=50, use_ema=False),
        "EMA20/50": dict(fast_period=20, slow_period=50, use_ema=True),
        "EMA9/21(빠른형)": dict(fast_period=9, slow_period=21, use_ema=True),
        "SMA20/50+ADX25": dict(fast_period=20, slow_period=50, use_ema=False, adx_threshold=25.0),
    }),
    ("15m", 180, {
        "SMA15/40": dict(fast_period=15, slow_period=40, use_ema=False),
        "EMA15/40": dict(fast_period=15, slow_period=40, use_ema=True),
        "EMA9/21(빠른형)": dict(fast_period=9, slow_period=21, use_ema=True),
        "SMA15/40+ADX25": dict(fast_period=15, slow_period=40, use_ema=False, adx_threshold=25.0),
    }),
]


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    client = get_futures_market_data_client()

    for timeframe, days, variant_params in TIMEFRAMES:
        variants = {name: partial(detect_ma_cross_signal, **params) for name, params in variant_params.items()}
        aggregate = {name: [] for name in variants}
        print(f"\n########## {timeframe} ({days}일) — 골든/데드크로스 ##########")

        for symbol in FUTURES_SYMBOLS:
            df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
            if len(df) < 200:
                print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
                continue
            print(f"\n=== {symbol} ({len(df)}봉) ===")
            print(f"  {'변형':<18}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(봉)':>12}")
            for name, signal_fn in variants.items():
                trades = run_backtest(
                    df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                    fee_pct_per_side=FEE_PCT_PER_SIDE, signal_fn=signal_fn,
                )
                stats = summarize(trades)
                aggregate[name].append(stats)
                print(f"  {name:<18}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                      f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                      f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

        print(f"\n--- {timeframe} 전체 종목 합산 ---")
        print(f"  {'변형':<18}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
        for name, per_symbol_stats in aggregate.items():
            n = sum(s["num_trades"] for s in per_symbol_stats)
            total_r = sum(s["total_r"] for s in per_symbol_stats)
            wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
            win_rate = wins / n if n else None
            avg_r = total_r / n if n else None
            print(f"  {name:<18}{n:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


if __name__ == "__main__":
    main()
