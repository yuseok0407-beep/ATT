"""과거 데이터로 진입/청산 규칙의 여러 변형을 비교하는 백테스트 CLI.

세 가지 변형을 각 종목(FUTURES_SYMBOLS)에 대해 돌리고 결과를 비교한다:
  - baseline : 지금 실거래 봇 그대로 (고정% 손절/익절, 손익분기 이동도 최대 보유시간도 없음)
  - fixed+exit관리 : 고정% 손절/익절 + 손익분기 이동 + 최대 보유시간
  - ATR+exit관리   : ATR 기반 손절/익절 + 손익분기 이동 + 최대 보유시간

전부 R배수(위험 대비 손익) 기준 — 포지션 사이징은 실거래 봇의 리스크 로직이 별도로 담당하므로
여기서는 다루지 않는다. 실주문 없이 공개 시세만 조회한다.
"""
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
from src.core.config import FUTURES_SYMBOLS, RULE_TIMEFRAME, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.data.futures_exchange import get_futures_market_data_client

BACKTEST_DAYS = 365
BREAKEVEN_AT_R = 1.0
MAX_HOLD_BARS = 72  # 1시간봉 기준 3일
ATR_MULTIPLIER = 2.0
FEE_PCT_PER_SIDE = 0.0004  # 바이낸스 선물 테이커 수수료(편도) 기본값

VARIANTS = {
    "baseline(현재)": dict(stop_mode="fixed", use_breakeven=False, use_max_hold=False),
    "fixed+손익분기만": dict(stop_mode="fixed", use_breakeven=True, use_max_hold=False),
    "fixed+최대보유만": dict(stop_mode="fixed", use_breakeven=False, use_max_hold=True),
    "fixed+exit관리": dict(stop_mode="fixed", use_breakeven=True, use_max_hold=True),
    "ATR단독": dict(stop_mode="atr", atr_multiplier=ATR_MULTIPLIER, use_breakeven=False, use_max_hold=False),
    "ATR+exit관리": dict(stop_mode="atr", atr_multiplier=ATR_MULTIPLIER, use_breakeven=True, use_max_hold=True),
}


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    client = get_futures_market_data_client()
    aggregate = {name: [] for name in VARIANTS}

    for symbol in FUTURES_SYMBOLS:
        print(f"\n=== {symbol} ===")
        df = fetch_historical_ohlcv(client, symbol, timeframe=RULE_TIMEFRAME, days=BACKTEST_DAYS)
        if len(df) < 100:
            print(f"  데이터 부족(봉 {len(df)}개) — 건너뜀")
            continue
        span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
        print(f"  {len(df)}봉, {span_days}일 구간 ({df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]})")

        print(f"  {'변형':<16}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(h)':>12}")
        for name, params in VARIANTS.items():
            trades = run_backtest(
                df, stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                breakeven_at_r=BREAKEVEN_AT_R, max_hold_bars=MAX_HOLD_BARS,
                fee_pct_per_side=FEE_PCT_PER_SIDE, **params,
            )
            stats = summarize(trades)
            aggregate[name].append(stats)
            print(f"  {name:<16}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                  f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                  f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

    print("\n=== 전체 종목 합산 ===")
    print(f"  {'변형':<16}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
    for name, per_symbol_stats in aggregate.items():
        all_trades_count = sum(s["num_trades"] for s in per_symbol_stats)
        total_r = sum(s["total_r"] for s in per_symbol_stats)
        wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
        win_rate = wins / all_trades_count if all_trades_count else None
        avg_r = total_r / all_trades_count if all_trades_count else None
        print(f"  {name:<16}{all_trades_count:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


if __name__ == "__main__":
    main()
