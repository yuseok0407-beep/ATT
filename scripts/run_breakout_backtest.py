"""Donchian 채널 브레이크아웃(터틀 트레이딩 스타일)이 실제로 통하는지 확인하는 백테스트.

청산 규칙은 실거래 봇/다른 백테스트들과 동일하게 고정 손절1.25%/손익비2.0을 그대로 써서, 진입
로직만 바꿔치기했을 때의 순수한 효과를 본다. ADX 상위추세 필터 유무도 함께 비교(브레이크아웃
초입엔 ADX가 아직 안 따라온 경우가 많아, 필터가 오히려 좋은 초입 신호를 걸러낼 수도 있다는
가설 검증). 실주문 없이 공개 시세만 조회한다."""
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
from src.core.breakout_strategy import detect_signal as detect_breakout_signal
from src.core.config import FUTURES_SYMBOLS, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.data.futures_exchange import get_futures_market_data_client

FEE_PCT_PER_SIDE = 0.0004

# (타임프레임, 조회 기간) — 15분봉은 봉 개수가 많아 기간을 줄인다.
TIMEFRAMES = [
    ("1h", 365),
    ("15m", 180),
]

VARIANTS = {
    "채널20 필터없음": partial(detect_breakout_signal, channel_period=20, adx_threshold=None),
    "채널20 ADX>=25": partial(detect_breakout_signal, channel_period=20, adx_threshold=25.0),
    "채널55 필터없음": partial(detect_breakout_signal, channel_period=55, adx_threshold=None),
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
        print(f"\n########## {timeframe} ({days}일) — Donchian 브레이크아웃 ##########")

        for symbol in FUTURES_SYMBOLS:
            df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
            if len(df) < 200:
                print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
                continue
            print(f"\n=== {symbol} ({len(df)}봉) ===")
            print(f"  {'변형':<18}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(봉)':>12}")
            for name, signal_fn in VARIANTS.items():
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
