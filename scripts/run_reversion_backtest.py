"""볼린저밴드+RSI 평균회귀 전략이 실제로 통하는지 확인하는 백테스트.

1차 시도(고정 손절1.25%/손익비2.0을 추세추종과 그대로 재사용)는 두 타임프레임 다 마이너스였음.
평균회귀는 "중간밴드(SMA)로 되돌아오면 익절"이 원래 더 자연스러운 목표라는 가설을 검증하기 위해,
익절 목표를 고정 폭 대신 볼린저밴드 중간선(매 봉 움직이는 값)으로 바꾼 버전을 함께 비교한다.
목표가가 고정이 아니게 되면 반등이 끝까지 안 오는 경우 포지션이 무한정 남을 수 있어, 이 백테스트
한정으로 최대 보유시간(max_hold)도 걸어 강제 청산되게 한다(실거래 봇엔 아직 이 기능이 없음 —
지난 논의에서 다뤘던 "익절/손절 안 닿으면 정리 안 되는 문제"와 같은 맥락).

레짐 필터(ADX) 유무도 함께 비교. 실주문 없이 공개 시세만 조회한다."""
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
from src.core.indicators import bollinger_bands
from src.core.reversion_strategy import detect_signal as detect_reversion_signal
from src.data.futures_exchange import get_futures_market_data_client

FEE_PCT_PER_SIDE = 0.0004
BB_PERIOD = 20

# (타임프레임, 조회 기간, 최대 보유 봉수) — 15분봉은 봉 개수가 많아 기간을 줄인다.
# max_hold_bars는 대략 3일 분량으로 맞춤(1h=72봉, 15m=288봉).
TIMEFRAMES = [
    ("1h", 365, 72),
    ("15m", 180, 288),
]

SIGNAL_VARIANTS = {
    "필터없음": partial(detect_reversion_signal, adx_threshold=None),
    "ADX<25": partial(detect_reversion_signal, adx_threshold=25.0),
}


def _fmt(value, pct=False, digits=2):
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def main():
    client = get_futures_market_data_client()

    for timeframe, days, max_hold_bars in TIMEFRAMES:
        variant_names = [f"고정익절(2.5%)+{s}" for s in SIGNAL_VARIANTS] + \
                         [f"중간밴드익절+{s}" for s in SIGNAL_VARIANTS]
        aggregate = {name: [] for name in variant_names}
        print(f"\n########## {timeframe} ({days}일) — 볼린저밴드+RSI 평균회귀 (익절방식 비교) ##########")

        for symbol in FUTURES_SYMBOLS:
            df = fetch_historical_ohlcv(client, symbol, timeframe=timeframe, days=days)
            if len(df) < 200:
                print(f"\n=== {symbol} === 데이터 부족(봉 {len(df)}개) — 건너뜀")
                continue
            middle_band = bollinger_bands(df["close"], BB_PERIOD)["middle"]

            print(f"\n=== {symbol} ({len(df)}봉) ===")
            print(f"  {'변형':<26}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}{'최대DD(R)':>11}{'평균보유(봉)':>12}")

            for signal_name, signal_fn in SIGNAL_VARIANTS.items():
                fixed_name = f"고정익절(2.5%)+{signal_name}"
                trades = run_backtest(
                    df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                    fee_pct_per_side=FEE_PCT_PER_SIDE, signal_fn=signal_fn,
                    use_max_hold=True, max_hold_bars=max_hold_bars,
                )
                stats = summarize(trades)
                aggregate[fixed_name].append(stats)
                print(f"  {fixed_name:<26}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                      f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                      f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

                mid_name = f"중간밴드익절+{signal_name}"
                trades = run_backtest(
                    df, stop_mode="fixed", stop_loss_pct=STOP_LOSS_PCT, take_profit_rr=TAKE_PROFIT_RR,
                    fee_pct_per_side=FEE_PCT_PER_SIDE, signal_fn=signal_fn,
                    target_series=middle_band, use_max_hold=True, max_hold_bars=max_hold_bars,
                )
                stats = summarize(trades)
                aggregate[mid_name].append(stats)
                print(f"  {mid_name:<26}{stats['num_trades']:>6}{_fmt(stats['win_rate'], pct=True):>8}"
                      f"{_fmt(stats['avg_r']):>8}{_fmt(stats['total_r']):>9}{_fmt(stats['max_drawdown_r']):>11}"
                      f"{_fmt(stats['avg_hold_bars'], digits=1):>12}")

        print(f"\n--- {timeframe} 전체 종목 합산 ---")
        print(f"  {'변형':<26}{'거래수':>6}{'승률':>8}{'평균R':>8}{'총R':>9}")
        for name in variant_names:
            per_symbol_stats = aggregate[name]
            n = sum(s["num_trades"] for s in per_symbol_stats)
            total_r = sum(s["total_r"] for s in per_symbol_stats)
            wins = sum((s["win_rate"] or 0) * s["num_trades"] for s in per_symbol_stats)
            win_rate = wins / n if n else None
            avg_r = total_r / n if n else None
            print(f"  {name:<26}{n:>6}{_fmt(win_rate, pct=True):>8}{_fmt(avg_r):>8}{_fmt(total_r):>9}")


if __name__ == "__main__":
    main()
