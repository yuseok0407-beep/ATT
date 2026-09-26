"""aoa(BitMEX) 공개 체결 내역 분석 — docs/DEMO2_PLAN.md 1절의 숫자를 다시 낸다.

원자료: demo2/aoa_public_2021-12-31_with_letter/aoa-execution-*.csv (약 149만 체결, 600MB —
커밋하지 않는다), aoa-wallet-*.csv(지갑 입출금·실현손익).

하는 일:
1. XBTUSD 체결을 **왕복 거래**(포지션 0 → 0)로 묶는다. 포지션이 0을 가로지르는 체결은 두
   거래로 쪼갠다 — 안 쪼개면 2018년 손익이 -1,743 XBT로 나와 지갑(+188)과 정반대가 된다.
   인버스 계약이라 손익은 XBT로 Σ(수량/가격)이다.
2. 연도별 스타일(승률·평균 이익/손실·보유시간·물타기)을 낸다.
3. 진입 직전 1시간봉 지표로 그의 롱/숏을 얼마나 맞힐 수 있는지 본다(바이낸스 BTC/USDT
   1시간봉을 공개 API로 받는다 — 2018~2021 BitMEX 가격과 거의 같다).

    python scripts/analyze_aoa_trades.py
"""

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "demo2" / "aoa_public_2021-12-31_with_letter"
CACHE_DIR = PROJECT_ROOT / ".aoa_cache"

_COLUMNS = ["symbol", "side", "lastqty", "lastpx", "lastliquidityind", "exectype", "ordtype",
            "orderid", "text", "transacttime"]


def load_executions(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """체결(Trade)만. 파싱이 느려서(600MB) 한 번 읽으면 .aoa_cache에 저장해 둔다."""
    cache = CACHE_DIR / "executions.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    files = sorted(glob.glob(str(data_dir / "aoa-execution-*.csv")))
    if not files:
        raise FileNotFoundError(f"체결 CSV가 없습니다: {data_dir}")
    df = pd.concat([pd.read_csv(f, usecols=_COLUMNS, low_memory=False) for f in files], ignore_index=True)
    df = df[df["exectype"] == "Trade"].copy()
    df["t"] = pd.to_datetime(df.pop("transacttime"), format="mixed")
    CACHE_DIR.mkdir(exist_ok=True)
    df.to_pickle(cache)
    return df


def round_trips(fills: pd.DataFrame) -> pd.DataFrame:
    """한 종목의 체결(시간순)을 왕복 거래로 묶는다. fills에는 side/lastqty/lastpx/t가 있어야 한다.

    인버스(XBTUSD) 기준 손익 = Σ(부호 있는 수량 / 가격) [XBT]. 포지션이 0을 가로지르는 체결은
    닫는 몫과 새로 여는 몫으로 쪼갠다.

    열: dir(+1 롱/-1 숏), start, end, entry(수량가중 진입가), exit, ret(방향 반영 수익률),
    pnl(XBT), adds(진입 쪽 체결 수), maxpos, first_px(첫 체결가), stop_exit, liq, maker."""
    fills = fills.sort_values("t", kind="stable")
    q = np.where(fills["side"].to_numpy() == "Buy", 1.0, -1.0) * fills["lastqty"].to_numpy(dtype=float)
    px = fills["lastpx"].to_numpy(dtype=float)
    ts = fills["t"].to_numpy()
    ordtype = fills["ordtype"].to_numpy() if "ordtype" in fills else np.full(len(q), "")
    text = fills["text"].to_numpy() if "text" in fills else np.full(len(q), "")
    maker = (fills["lastliquidityind"].to_numpy() == "AddedLiquidity") if "lastliquidityind" in fills \
        else np.zeros(len(q), dtype=bool)

    rows, pos, cur = [], 0.0, None
    for i in range(len(q)):
        remaining = q[i]
        while remaining != 0:
            if pos == 0:
                cur = {"dir": int(np.sign(remaining)), "start": ts[i], "first_px": px[i],
                       "entry_qty": 0.0, "entry_cost": 0.0, "exit_qty": 0.0, "exit_cost": 0.0,
                       "pnl": 0.0, "maxpos": 0.0, "adds": 0, "fills": 0, "maker_fills": 0,
                       "stop_exit": False, "liq": False}
            if np.sign(remaining) == cur["dir"]:
                take = remaining
                cur["entry_qty"] += abs(take)
                cur["entry_cost"] += abs(take) / px[i]
                cur["adds"] += 1
            else:
                take = np.sign(remaining) * min(abs(remaining), abs(pos))
                cur["exit_qty"] += abs(take)
                cur["exit_cost"] += abs(take) / px[i]
                cur["stop_exit"] |= ordtype[i] in ("Stop", "StopLimit")
                cur["liq"] |= text[i] == "Liquidation"
            cur["pnl"] += take / px[i]
            pos += take
            remaining -= take
            cur["maxpos"] = max(cur["maxpos"], abs(pos))
            cur["fills"] += 1
            cur["maker_fills"] += int(maker[i])
            if pos == 0:
                cur["end"] = ts[i]
                rows.append(cur)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # 인버스 계약의 평균가는 조화평균(수량 / Σ수량/가격)이다.
    out["entry"] = out["entry_qty"] / out["entry_cost"]
    out["exit"] = out["exit_qty"] / out["exit_cost"]
    out["ret"] = out["dir"] * (out["exit"] / out["entry"] - 1)
    out["hold_h"] = (pd.to_datetime(out["end"]) - pd.to_datetime(out["start"])).dt.total_seconds() / 3600
    out["maker"] = out["maker_fills"] / out["fills"]
    # 평단이 첫 체결가보다 불리해졌으면(0.05% 넘게) 물타기로 본다.
    out["averaged_down"] = out["dir"] * (out["entry"] / out["first_px"] - 1) < -0.0005
    return out


def yearly_profile(trips: pd.DataFrame) -> pd.DataFrame:
    trips = trips.assign(y=pd.to_datetime(trips["start"]).dt.year, win=trips["ret"] > 0)
    g = trips.groupby("y")
    return pd.DataFrame({
        "trades": g.size(),
        "long_share": g["dir"].apply(lambda s: (s > 0).mean()),
        "win_rate": g["win"].mean(),
        "avg_win_pct": trips[trips["win"]].groupby("y")["ret"].mean() * 100,
        "avg_loss_pct": trips[~trips["win"]].groupby("y")["ret"].mean() * 100,
        "median_hold_h": g["hold_h"].median(),
        "stop_exit_share": g["stop_exit"].mean(),
        "pnl_xbt": g["pnl"].sum(),
        "pnl_averaged_down": trips[trips["averaged_down"]].groupby("y")["pnl"].sum(),
        "pnl_not_averaged": trips[~trips["averaged_down"]].groupby("y")["pnl"].sum(),
    }).round(3)


def wallet_realised_by_year(data_dir: Path = DATA_DIR) -> pd.Series:
    """지갑의 연도별 실현손익(XBT) — 재구성이 맞는지 대조하는 기준."""
    files = glob.glob(str(data_dir / "aoa-wallet-*.csv"))
    w = pd.read_csv(files[0])
    w = w[w["transacttype"] == "RealisedPNL"]
    return (w["amount"] / 1e8).groupby(pd.to_datetime(w["date"]).dt.year).sum().round(1)


def fetch_btc_hourly() -> pd.DataFrame:
    cache = CACHE_DIR / "btc_1h_2018_2021.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    import ccxt
    ex = ccxt.binance()
    since, end, rows = ex.parse8601("2018-01-01T00:00:00Z"), ex.parse8601("2022-01-02T00:00:00Z"), []
    while since < end:
        batch = ex.fetch_ohlcv("BTC/USDT", "1h", since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 3_600_000
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df.index = pd.to_datetime(df.pop("ts"), unit="ms")
    CACHE_DIR.mkdir(exist_ok=True)
    df.to_pickle(cache)
    return df


def entry_features(candles: pd.DataFrame) -> pd.DataFrame:
    """각 시각의 1시간봉 지표. 진입 직전 **마감된** 봉의 값을 쓰도록 호출자가 한 시간 당긴다."""
    close = candles["close"]
    tr = pd.concat([candles["high"] - candles["low"], (candles["high"] - close.shift()).abs(),
                    (candles["low"] - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    delta = close.diff()
    rs = delta.clip(lower=0).ewm(alpha=1 / 14).mean() / (-delta.clip(upper=0)).ewm(alpha=1 / 14).mean()
    hi24, lo24 = candles["high"].rolling(24).max(), candles["low"].rolling(24).min()
    return pd.DataFrame({
        "r1": close.pct_change(1) * 100, "r4": close.pct_change(4) * 100,
        "r24": close.pct_change(24) * 100, "r72": close.pct_change(72) * 100,
        "z20": (close - close.rolling(20).mean()) / atr, "z100": (close - close.rolling(100).mean()) / atr,
        "rsi": 100 - 100 / (1 + rs), "pos24": (close - lo24) / (hi24 - lo24),
        "vol": candles["volume"] / candles["volume"].rolling(24 * 7).mean(), "atrpct": atr / close * 100,
    })


def direction_predictability(trips: pd.DataFrame, features: pd.DataFrame) -> dict:
    """2018-19로 학습한 로지스틱 회귀가 2020-21의 롱/숏을 얼마나 맞히는지. 동전은 50%."""
    at = pd.to_datetime(trips["start"]).dt.floor("h") - pd.Timedelta(hours=1)
    X = features.reindex(at.values).reset_index(drop=True)
    X["dir"] = trips["dir"].to_numpy()
    X["y"] = pd.to_datetime(trips["start"]).dt.year.to_numpy()
    X = X.dropna()
    cols = [c for c in features.columns]
    train, test = X[X["y"] <= 2019], X[X["y"] >= 2020]
    mu, sd = train[cols].mean(), train[cols].std()

    def design(d):
        return np.c_[np.ones(len(d)), ((d[cols] - mu) / sd).to_numpy()]

    w = np.zeros(len(cols) + 1)
    A, target = design(train), (train["dir"] > 0).to_numpy(dtype=float)
    for _ in range(3000):
        p = 1 / (1 + np.exp(-A @ w))
        w -= 0.1 * A.T @ (p - target) / len(target)

    def accuracy(d):
        return float((((1 / (1 + np.exp(-design(d) @ w))) > 0.5) == (d["dir"] > 0)).mean())

    X["r4_decile"] = pd.qcut(X["r4"], 10)
    return {
        "train_acc": accuracy(train), "test_acc": accuracy(test), "test_n": len(test),
        "weights": dict(zip(["bias"] + cols, np.round(w, 2))),
        "long_share_by_r4_decile": X.groupby("r4_decile", observed=True)["dir"].apply(
            lambda s: round(float((s > 0).mean()), 3)).to_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="aoa 공개 체결 내역 분석")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--skip-candles", action="store_true", help="캔들 조회 없이 1~2만")
    args = parser.parse_args()

    pd.set_option("display.width", 200)
    fills = load_executions(args.data_dir)
    trips = round_trips(fills[fills["symbol"] == "XBTUSD"])
    by_year = yearly_profile(trips)
    wallet = wallet_realised_by_year(args.data_dir)

    print(f"XBTUSD 왕복 {len(trips)}건 · 손익 합계 {trips['pnl'].sum():+.1f} XBT")
    print(f"손익 부호와 수익률 부호 일치율 {(np.sign(trips['ret']) == np.sign(trips['pnl'])).mean():.2%}")
    print("\n연도별 스타일:")
    print(by_year.to_string())
    print("\n지갑 실현손익(모든 종목·수수료·펀딩 포함) 연도별 XBT:", wallet.to_dict())

    if args.skip_candles:
        return 0
    features = entry_features(fetch_btc_hourly())
    result = direction_predictability(trips, features)
    print(f"\n방향 예측: 학습 {result['train_acc']:.1%} / 검증(2020-21, {result['test_n']}건) "
          f"{result['test_acc']:.1%} — 동전은 50%")
    print("가중치:", result["weights"])
    print("직전 4시간 수익률 10분위별 롱 비중:")
    for bucket, share in result["long_share_by_r4_decile"].items():
        print(f"  {bucket}: {share:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
