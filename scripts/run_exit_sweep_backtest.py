"""ATR 기반 손절폭 / 손익비(RR)를, 지금 실거래 중인 진입조건(ADX30+SMA10)을 그대로 두고
청산 규칙만 바꿔가며 재검증한다.

예전에 ATR 손절을 테스트한 적이 있지만(2026-08-11 "백테스트 하네스 구축" 항목) 그때는 진입조건이
구버전(ADX25+SMA20)이었고, 그 이후 진입조건이 ADX30+SMA10으로 바뀌어 재검증이 안 된 상태였다.
signal_fn을 넘기지 않아 실거래 봇과 동일하게 config의 RULE_ADX_THRESHOLD/RULE_SMA_PERIOD 기본값을
그대로 쓴다. 실주문 없이 공개 시세만 조회한다."""
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

# 주의: run_backtest의 sma_period 기본값은 20으로 하드코딩돼 있고(백테스트 결과를 실거래 config
# 변경과 무관하게 재현 가능하도록 의도적으로 그렇게 함) RULE_SMA_PERIOD(config, 현재 10)를 자동으로
# 따라가지 않는다 — 그래서 실제 배포된 진입조건을 재현하려면 매 변형마다 sma_period를 명시로
# 넘겨야 한다(빠뜨리면 조용히 SMA20으로 도는 사고가 남).
VARIANTS = {
    "고정손절(현재,RR2.0)": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=2.0),
    "ATR×1.5(RR2.0)": dict(stop_mode="atr", atr_multiplier=1.5, take_profit_rr=2.0),
    "ATR×2.0(RR2.0)": dict(stop_mode="atr", atr_multiplier=2.0, take_profit_rr=2.0),
    "ATR×3.0(RR2.0)": dict(stop_mode="atr", atr_multiplier=3.0, take_profit_rr=2.0),
    "고정손절+RR1.5": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=1.5),
    "고정손절+RR2.5": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=2.5),
    "고정손절+RR3.0": dict(stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=3.0),
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
        print(f"\n########## {timeframe} ({days}일) — 청산 규칙(ATR/RR) 재검증 ##########")

        for symbol in FUTURES_SYMBOLS:
            df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
            if len(df) < 200:
                print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
                continue
            print(f"\n=== {symbol} ({len(df)}봉) ===")
            print(f"  {'변형':<20}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(봉)':>12}")
            for name, params in VARIANTS.items():
                trades = run_backtest(df, fee_pct_per_side=FEE_PCT_PER_SIDE, **params)
                stats = summarize(trades)
                aggregate[name].append(stats)
                print(f"  {name:<20}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                      f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                      f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

        print(f"\n--- {timeframe} 전체 종목 합산 ---")
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
