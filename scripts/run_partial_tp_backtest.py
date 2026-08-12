"""부분 익절(스케일 아웃)이 지금 실거래 중인 진입조건(ADX30+SMA10)에서 실제로 도움이 되는지 확인.

예전에 기각했던 "전체 포지션 손익분기 이동"과의 핵심 차이: 그건 +1R에서 포지션 전체의 손절을
진입가로 옮겨서 최악의 경우 0R로 끝났지만(그리고 실제로 곧 +2R까지 갈 트레이드들을 조기
종료시켜 대폭 악화됐었음), 부분 익절은 일부(partial_fraction)를 먼저 실현해두기 때문에 나머지가
손익분기로 되돌아가도 전체 트레이드는 최악의 경우에도 플러스로 남는다는 게 이론적 장점.
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
from src.core.config import FUTURES_SYMBOLS, RULE_SMA_PERIOD, STOP_LOSS_PCT
from src.data.futures_exchange import get_futures_market_data_client

FEE_PCT_PER_SIDE = 0.0004

TIMEFRAMES = [
    ("1h", 365),
    ("15m", 180),
]

# 주의: run_backtest의 sma_period 기본값(20)은 RULE_SMA_PERIOD(config, 현재 10)를 자동으로 따라가지
# 않는다 — 실제 배포된 진입조건을 재현하려면 매 변형마다 명시로 넘겨야 한다.
VARIANTS = {
    "부분익절없음(현재,RR2.0)": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=2.0),
    "부분1R+50%": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=2.0,
                        use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.5),
    "부분1.5R+50%": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=2.0,
                          use_partial_tp=True, partial_at_r=1.5, partial_fraction=0.5),
    "부분1R+30%(적게떼기)": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=2.0,
                                  use_partial_tp=True, partial_at_r=1.0, partial_fraction=0.3),
}
for _params in VARIANTS.values():
    _params["sma_period"] = RULE_SMA_PERIOD


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
        print(f"\n########## {timeframe} ({days}일) — 부분 익절(스케일 아웃) ##########")

        for symbol in FUTURES_SYMBOLS:
            df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
            if len(df) < 200:
                print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
                continue
            print(f"\n=== {symbol} ({len(df)}봉) ===")
            print(f"  {'변형':<24}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(봉)':>12}")
            for name, params in VARIANTS.items():
                trades = run_backtest(df, fee_pct_per_side=FEE_PCT_PER_SIDE, **params)
                stats = summarize(trades)
                aggregate[name].append(stats)
                print(f"  {name:<24}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                      f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                      f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

        print(f"\n--- {timeframe} 전체 종목 합산 ---")
        print(f"  {'변형':<24}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
        for name, per_symbol_stats in aggregate.items():
            n = sum(s["num_trades"] for s in per_symbol_stats)
            total_r = sum(s["total_r"] for s in per_symbol_stats)
            wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
            win_rate = wins / n if n else None
            avg_r = total_r / n if n else None
            print(f"  {name:<24}{n:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


if __name__ == "__main__":
    main()
