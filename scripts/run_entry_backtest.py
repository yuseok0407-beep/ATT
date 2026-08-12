"""진입 조건(ADX 임계값 / RSI 확인 / SMA 기간 / 캔들 주기) 변형을 비교하는 백테스트 CLI.

청산 규칙은 전부 지금 실거래 봇 그대로(고정% 손절/익절, 손익분기 이동·최대보유시간 없음)로 고정하고
진입 조건 하나씩만 바꿔서, 어떤 진입 조건 변화가 실제로 유의미한 차이를 만드는지 본다.
실주문 없이 공개 시세만 조회한다.
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
from src.core.config import FUTURES_SYMBOLS, RULE_ADX_THRESHOLD, STOP_LOSS_PCT, TAKE_PROFIT_RR
from src.data.futures_exchange import get_futures_market_data_client

BACKTEST_DAYS = 365
FEE_PCT_PER_SIDE = 0.0004

VARIANTS = {
    "현재(ADX25+SMA20+RSI50)": dict(adx_threshold=RULE_ADX_THRESHOLD, sma_period=20, require_rsi_confirm=True),
    "ADX 15": dict(adx_threshold=15, sma_period=20, require_rsi_confirm=True),
    "ADX 20": dict(adx_threshold=20, sma_period=20, require_rsi_confirm=True),
    "ADX 30": dict(adx_threshold=30, sma_period=20, require_rsi_confirm=True),
    "ADX 35": dict(adx_threshold=35, sma_period=20, require_rsi_confirm=True),
    "RSI 필터 제거": dict(adx_threshold=RULE_ADX_THRESHOLD, sma_period=20, require_rsi_confirm=False),
    "SMA10": dict(adx_threshold=RULE_ADX_THRESHOLD, sma_period=10, require_rsi_confirm=True),
    "SMA50": dict(adx_threshold=RULE_ADX_THRESHOLD, sma_period=50, require_rsi_confirm=True),
}


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def run_matrix(timeframe: str, label: str):
    client = get_futures_market_data_client()
    aggregate = {name: [] for name in VARIANTS}

    print(f"\n########## {label} ({timeframe}) ##########")
    for symbol in FUTURES_SYMBOLS:
        df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=BACKTEST_DAYS)
        if len(df) < 100:
            print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
            continue
        span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
        print(f"\n=== {symbol} ({len(df)}봉, {span_days}일) ===")
        print(f"  {'변형':<26}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}")
        for name, params in VARIANTS.items():
            trades = run_backtest(
                df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                fee_pct_per_side=FEE_PCT_PER_SIDE, **params,
            )
            stats = summarize(trades)
            aggregate[name].append(stats)
            print(f"  {name:<26}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                  f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}")

    print(f"\n--- {label} 전체 종목 합산 ---")
    print(f"  {'변형':<26}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
    for name, per_symbol_stats in aggregate.items():
        all_trades_count = sum(s["num_trades"] for s in per_symbol_stats)
        total_r = sum(s["total_r"] for s in per_symbol_stats)
        wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
        win_rate = wins / all_trades_count if all_trades_count else None
        avg_r = total_r / all_trades_count if all_trades_count else None
        print(f"  {name:<26}{all_trades_count:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


def main():
    run_matrix("1h", "1시간봉")
    run_matrix("4h", "4시간봉")


if __name__ == "__main__":
    main()
