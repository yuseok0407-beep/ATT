"""실거래 거래를 백테스트와 **거래 단위로** 대조한다 — "백테스트 건당 +0.3R, 실거래 ≈0"이 어디서
갈리는지(2026-10-03, 외부 검토 3.8 권고: "같은 함수"가 아니라 "같은 시점에 같은 결정을 했는가").

    python scripts/fetch_recent_ohlcv.py          # 먼저 최근 캔들(.ohlcv_cache_recent)
    python scripts/reconcile_live_vs_backtest.py  # 데모·실계좌 둘 다

A. 같은 거래의 결과 — 실거래 거래 하나하나를 **같은 진입가·손절가·익절가**로 1시간봉에 다시 굴린다.
   손절/익절 판정과 R이 실거래와 다르면 원인은 체결·청산 쪽이다(신호 선택과 무관).
B. 어떤 거래를 했나 — 같은 기간에 **그때의 규칙**(아래 RULE_PERIODS)으로 백테스트가 진입했을 거래와
   실거래 진입을 (종목, 신호봉)으로 맞춘다. 겹친 거래 / 백테스트만 한 거래 / 실거래만 한 거래의 R로
   격차를 나눈다. 종목별 독립 시뮬레이션이라 동시보유 한도·서킷브레이커 정지는 없다 — 실거래가
   브레이커로 멈춘 기간의 신호는 "백테스트만 한 거래"로 나온다.

결과 CSV: exports/reconcile_{env}_same_trade.csv, exports/reconcile_{env}_trade_set.csv
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from src.backtest.data import cache_path  # noqa: E402
from src.backtest.engine import _check_exit, _pnl_r, run_backtest  # noqa: E402
from src.execution import performance  # noqa: E402
from src.execution.journal import read_entries  # noqa: E402

CACHE_DIR = PROJECT_ROOT / ".ohlcv_cache_recent"
EXPORT_DIR = PROJECT_ROOT / "exports"
JOURNALS = {"demo": "journal/futures_rule_trades.jsonl", "live": "journal/futures_rule_trades.live.jsonl"}

# 그때그때 실거래 규칙. 09-12 이후는 저널의 config_changed 스냅샷, 그 전은 strategy_versions의
# 복원 버전. 08-25 이전은 진행 중인 봉으로 신호를 냈으므로(마감봉 아님) B에서 뺀다.
_BASE = dict(adx_threshold=30.0, sma_period=10, take_profit_rr=2.0)
RULE_PERIODS = [
    ("2026-08-25 00:00", "2026-09-08 18:00", dict(_BASE, direction_filter="rsi", regime_sma_period=0,
                                                  min_atr_to_stop_ratio=0.0, htf_hours=0, stop_loss_pct=0.0125)),
    ("2026-09-08 18:00", "2026-09-10 07:00", dict(_BASE, direction_filter="rsi", regime_sma_period=400,
                                                  min_atr_to_stop_ratio=0.0, htf_hours=0, stop_loss_pct=0.0125)),
    ("2026-09-10 07:00", "2026-09-22 08:05", dict(_BASE, direction_filter="rsi", regime_sma_period=400,
                                                  min_atr_to_stop_ratio=0.64, htf_hours=0, stop_loss_pct=0.0125)),
    ("2026-09-22 08:05", "2026-09-24 05:56", dict(_BASE, direction_filter="rsi", regime_sma_period=400,
                                                  min_atr_to_stop_ratio=0.64, htf_hours=0, stop_loss_pct=0.0175)),
    ("2026-09-24 05:56", "2026-09-29 01:01", dict(_BASE, direction_filter="di", regime_sma_period=400,
                                                  min_atr_to_stop_ratio=0.64, htf_hours=0, stop_loss_pct=0.0175)),
    ("2026-09-29 01:01", "2026-10-04 00:00", dict(_BASE, direction_filter="di", regime_sma_period=400,
                                                  min_atr_to_stop_ratio=0.64, htf_hours=12, stop_loss_pct=0.0175)),
]


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts


def load_candles() -> dict[str, pd.DataFrame]:
    out = {}
    for path in CACHE_DIR.glob("*.json"):
        df = pd.read_json(path)
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.iloc[:-1].reset_index(drop=True)   # 마지막 봉은 진행 중일 수 있다
        name = path.stem
        base, rest = name.split("_", 1)
        out[f"{base}/{rest.replace('-', ':')}"] = df
    return out


def replay(df: pd.DataFrame, trade: dict) -> dict:
    """실거래 한 건을 같은 가격들로 1시간봉에 굴린다. 진입 봉 = 진입 시각이 속한 봉."""
    entry_at = _utc(trade["entry_timestamp"])
    start = int(df["timestamp"].searchsorted(entry_at.floor("h")))
    position = {"side": trade["side"], "entry_price": trade["entry_price"],
                "stop_price": trade["stop_loss_price"], "target_price": trade["take_profit_price"],
                "original_stop_price": trade["stop_loss_price"], "breakeven_moved": False}
    for i in range(start, len(df)):
        reason, exit_price = _check_exit(df.iloc[i], position, False, 1.0)
        if reason:
            return {"bt_reason": reason, "bt_exit_price": exit_price, "bt_exit_at": df["timestamp"].iloc[i],
                    "bt_r": _pnl_r(trade["entry_price"], trade["stop_loss_price"], trade["side"], exit_price)}
    return {"bt_reason": "open", "bt_exit_price": None, "bt_exit_at": None, "bt_r": None}


def same_trade(env: str, candles: dict) -> pd.DataFrame:
    rows = []
    for t in performance.resolve_closed_trades(read_entries(path=JOURNALS[env])):
        if t.get("realized_r") is None or t.get("stop_loss_price") is None or t.get("take_profit_price") is None \
                or t.get("symbol") not in candles or not t.get("entry_timestamp"):
            continue
        entry_at = _utc(t["entry_timestamp"])
        net_r, _ = performance._net_r(t)
        rows.append({
            "symbol": t["symbol"], "side": t["side"], "entry_at": entry_at,
            # 마감봉 신호(08-25~)는 봉 마감 직후 진입한다. 그 전 기록은 봉 중간 진입이라 진입 봉의
            # 앞부분(진입 전 가격)까지 판정에 들어간다 — 근사로 표시한다.
            "mid_bar_entry": entry_at < pd.Timestamp("2026-08-25") or (entry_at - entry_at.floor("h")).seconds > 300,
            "live_reason": t.get("reason"), "live_r": t["realized_r"], "live_net_r": net_r,
            **replay(candles[t["symbol"]], t),
        })
    return pd.DataFrame(rows)


def live_entries(env: str) -> pd.DataFrame:
    """실거래 진입 (종목, 신호봉). 신호봉 기록이 없는 08-25~09-08 진입은 '진입 시각의 봉 - 1시간'."""
    closed = {(_utc(t["entry_timestamp"]), t["symbol"]): t
              for t in performance.resolve_closed_trades(read_entries(path=JOURNALS[env]))
              if t.get("entry_timestamp")}
    rows = []
    for e in read_entries(path=JOURNALS[env]):
        if not e.get("entered"):
            continue
        at = _utc(e["timestamp"])
        if at < pd.Timestamp("2026-08-25"):
            continue
        bar = _utc(e["signal_bar_timestamp"]) if e.get("signal_bar_timestamp") else at.floor("h") - pd.Timedelta(hours=1)
        t = closed.get((at, e["symbol"])) or {}
        net_r, _ = performance._net_r(t) if t else (None, False)
        rows.append({"symbol": e["symbol"], "signal_bar": bar, "side": "long" if e.get("signal") == "LONG" else "short",
                     "live_r": t.get("realized_r"), "live_net_r": net_r})
    return pd.DataFrame(rows)


def backtest_entries(candles: dict) -> pd.DataFrame:
    rows = []
    for start, end, params in RULE_PERIODS:
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        for symbol, df in candles.items():
            trades = run_backtest(df, **params)
            for tr in trades:
                bar = df["timestamp"].iloc[tr["entry_index"]]
                entry_at = bar + pd.Timedelta(hours=1)   # 신호봉 마감 = 진입
                if start <= entry_at < end:
                    rows.append({"symbol": symbol, "signal_bar": bar, "side": tr["side"],
                                 "bt_r_net": tr["pnl_r"], "bt_reason": tr["reason"], "rules_from": start})
    return pd.DataFrame(rows)


def _r(series) -> str:
    s = pd.to_numeric(series, errors="coerce").dropna()
    return f"{len(s):>4}건 합 {s.sum():+7.2f}R 건당 {s.mean() if len(s) else 0:+.3f}R"


def main() -> int:
    candles = load_candles()
    if not candles:
        print("캔들이 없다 — 먼저 python scripts/fetch_recent_ohlcv.py")
        return 2
    EXPORT_DIR.mkdir(exist_ok=True)
    bt = backtest_entries(candles)
    for env in JOURNALS:
        print(f"\n==================== {env} ====================")
        a = same_trade(env, candles)
        a.to_csv(EXPORT_DIR / f"reconcile_{env}_same_trade.csv", index=False, encoding="utf-8-sig")
        clean = a[~a["mid_bar_entry"] & a["bt_r"].notna()]
        print(f"[A] 같은 거래를 같은 가격으로 다시 굴림 (봉 마감 직후 진입만 {len(clean)}건, 전체 {len(a)}건)")
        print(f"    실거래 가격R  {_r(clean['live_r'])}")
        print(f"    백테스트 R    {_r(clean['bt_r'])}   ← 같은 거래, 수수료 전")
        print(f"    실거래 순R    {_r(clean['live_net_r'])}   ← 수수료·진입 체결 포함")
        live_kind = clean["live_reason"].where(clean["live_reason"].isin(["stop_loss", "take_profit"]), "기타")
        print("    판정 일치표(행=실거래, 열=백테스트):")
        print(pd.crosstab(live_kind, clean["bt_reason"]).to_string().replace("\n", "\n      "))
        flips = clean[(live_kind == "stop_loss") & (clean["bt_reason"] == "take_profit")]
        if len(flips):
            print(f"    실거래는 손절, 백테스트는 익절: {len(flips)}건 — 실거래 {flips['live_r'].sum():+.2f}R vs "
                  f"백테스트 {flips['bt_r'].sum():+.2f}R")

        live = live_entries(env)
        merged = live.merge(bt, on=["symbol", "signal_bar"], how="outer", indicator=True, suffixes=("_live", "_bt"))
        merged.to_csv(EXPORT_DIR / f"reconcile_{env}_trade_set.csv", index=False, encoding="utf-8-sig")
        both = merged[merged["_merge"] == "both"]
        print("\n[B] 같은 기간(08-25~), 그때의 규칙으로 백테스트가 했을 거래 vs 실거래 진입")
        print(f"    둘 다 진입         실거래 순R {_r(both['live_net_r'])} | 백테스트 {_r(both['bt_r_net'])}")
        print(f"    백테스트만 진입    백테스트 {_r(merged.loc[merged['_merge'] == 'right_only', 'bt_r_net'])}")
        print(f"    실거래만 진입      실거래 순R {_r(merged.loc[merged['_merge'] == 'left_only', 'live_net_r'])}")
        print(f"    (백테스트 전체 {_r(bt['bt_r_net'])} / 실거래 전체 순R {_r(live['live_net_r'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
