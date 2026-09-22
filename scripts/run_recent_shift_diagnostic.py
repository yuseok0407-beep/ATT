"""지금 실거래 파라미터(ADX30+SMA10, 1시간봉, 고정손절+RR2.0)의 최근 365일 백테스트가 왜
-24.98R(2026-08-22 확인)로 뒤집혔는지 진단한다. 2026-08-11 아웃오브샘플 검증(전반 +43.24R/
후반 +11.55R, 양쪽 다 플러스) 통과 직후였다는 점에서, "일시적 드로다운(전략은 여전히 유효)"과
"구조적 붕괴(전략 자체가 무너짐)"를 구분하는 게 목적 — 트렌드추종은 손익이 소수의 큰 승리
트레이드에 몰리는 경우가 흔해서, 최근 구간에 그 승리가 없었을 뿐인 정상적 분산일 수도 있다.

출력: (1) 종목별 전체 요약, (2) 시간순 누적 R 곡선(체크포인트), (3) 최근 30/60/90일 구간별 요약과
전체 대비 비중, (4) 승리 트레이드 집중도(상위 N개가 총이익에서 차지하는 비율, fat-tail 여부).
실주문 없이 공개 시세만 조회한다."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import pandas as pd

from src.backtest.data import fetch_historical_ohlcv
from src.backtest.engine import run_backtest
from src.backtest.report import summarize
from src.core.config import (FEE_PCT_PER_SIDE, FUTURES_SYMBOLS, RULE_ADX_THRESHOLD, RULE_SMA_PERIOD,
                             STOP_LOSS_PCT, TAKE_PROFIT_RR)
from src.data.futures_exchange import get_futures_market_data_client

BACKTEST_DAYS = 365
RECENT_WINDOWS_DAYS = [30, 60, 90]
CHECKPOINT_EVERY = 20


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    client = get_futures_market_data_client()

    all_trades = []  # {"timestamp", "pnl_r", "symbol"}
    per_symbol_summary = {}

    for symbol in FUTURES_SYMBOLS:
        df = fetch_historical_ohlcv(client, symbol, timeframe="1h", days=BACKTEST_DAYS)
        if len(df) < 100:
            print(f"{symbol}: 데이터 부족(봉 {len(df)}개) — 건너뜀")
            continue
        trades = run_backtest(
            df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
            adx_threshold=RULE_ADX_THRESHOLD, sma_period=RULE_SMA_PERIOD,
            require_rsi_confirm=True, fee_pct_per_side=FEE_PCT_PER_SIDE,
        )
        per_symbol_summary[symbol] = summarize(trades)
        for t in trades:
            all_trades.append({
                "timestamp": df["timestamp"].iloc[t["exit_index"]],
                "pnl_r": t["pnl_r"],
                "symbol": symbol,
            })

    all_trades.sort(key=lambda t: t["timestamp"])

    print("\n=== 종목별 전체(365일) 요약 ===")
    total_r = 0.0
    total_trades = 0
    for symbol, stats in per_symbol_summary.items():
        print(f"  {symbol:<16} 거래{stats['num_trades']:>4} 승률{_fmt(stats['win_rate'], pct=True):>7} "
              f"총R{_fmt(stats['total_r']):>9}")
        total_r += stats["total_r"]
        total_trades += stats["num_trades"]
    print(f"  합산: 거래 {total_trades}건, 총R {_fmt(total_r)}")

    if not all_trades:
        print("\n거래가 하나도 없어 이후 분석을 진행할 수 없습니다.")
        return

    print(f"\n=== 누적 R 곡선 (시간순, {CHECKPOINT_EVERY}거래 단위 체크포인트) ===")
    cum = 0.0
    for i, t in enumerate(all_trades, 1):
        cum += t["pnl_r"]
        if i % CHECKPOINT_EVERY == 0 or i == len(all_trades):
            print(f"  거래#{i:>4} ({t['timestamp'].date()}) 누적R={_fmt(cum):>9}")

    print("\n=== 최근 N일 구간별 요약 (전체 대비 비중) ===")
    latest_ts = all_trades[-1]["timestamp"]
    for days in RECENT_WINDOWS_DAYS:
        cutoff = latest_ts - pd.Timedelta(days=days)
        recent = [t["pnl_r"] for t in all_trades if t["timestamp"] >= cutoff]
        if not recent:
            print(f"  최근{days}일: 거래 없음")
            continue
        wins = sum(1 for p in recent if p > 0)
        recent_r = sum(recent)
        share = (recent_r / total_r * 100) if total_r else float("nan")
        print(f"  최근{days:>3}일: 거래{len(recent):>4} 승률{_fmt(wins / len(recent), pct=True):>7} "
              f"총R{_fmt(recent_r):>9}  (전체 총R의 {_fmt(share)}%)")

    print("\n=== 승리 트레이드 집중도 (fat-tail 여부) ===")
    wins_sorted = sorted((t["pnl_r"] for t in all_trades if t["pnl_r"] > 0), reverse=True)
    total_win_r = sum(wins_sorted)
    if not wins_sorted:
        print("  승리 트레이드 없음")
    else:
        for top_n in (5, 10, 20):
            n = min(top_n, len(wins_sorted))
            top_sum = sum(wins_sorted[:n])
            share = (top_sum / total_win_r * 100) if total_win_r else float("nan")
            print(f"  상위 {n:>2}개 승리 트레이드가 총이익의 {_fmt(share)}% 차지 "
                  f"(전체 승리 {len(wins_sorted)}건, 총이익 {_fmt(total_win_r)}R)")


if __name__ == "__main__":
    main()
