"""C. 과거 성과로 종목을 고르면 다음 달에도 나은가 — per_symbol.json(탐색 구간 단일 백테스트) 사용."""
import json
import sys
from pathlib import Path

import pandas as pd

for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8")

per = json.load(open(Path(__file__).resolve().parents[1] / "exports" / "per_symbol.json", encoding="utf-8"))
START = pd.Timestamp("2025-09-22 04:00")
CUTOFF = pd.Timestamp("2026-06-24 03:00")

for train_days in (60, 120):
    picked, rest, pairs = [], [], []
    t = START + pd.Timedelta(days=train_days + 17)  # 레짐 SMA400(≈17일) 워밍업 뒤부터 학습창이 찬다
    while t + pd.Timedelta(days=30) <= CUTOFF:
        for sym, v in per.items():
            tr = [(pd.Timestamp(x["exit"]), x["r"]) for x in v["trades"]]
            train = sum(r for ts, r in tr if t - pd.Timedelta(days=train_days) <= ts < t)
            n_train = sum(1 for ts, r in tr if t - pd.Timedelta(days=train_days) <= ts < t)
            test = [r for ts, r in tr if t <= ts < t + pd.Timedelta(days=30)]
            if n_train == 0:
                continue
            pairs.append((train, sum(test)))
            (picked if train > 0 else rest).extend(test)
        t += pd.Timedelta(days=30)
    df = pd.DataFrame(pairs, columns=["train", "test"])
    rho = df["train"].rank().corr(df["test"].rank())
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    print(f"학습 {train_days}일 → 다음 30일 | 종목-월 {len(df)}개 | 순위상관 {rho:+.3f}")
    print(f"  학습 R>0으로 고른 종목의 다음 달: 거래 {len(picked)} 건당 {avg(picked):+.3f}R 총 {sum(picked):+.1f}R")
    print(f"  학습 R<=0이라 뺀 종목의 다음 달: 거래 {len(rest)} 건당 {avg(rest):+.3f}R 총 {sum(rest):+.1f}R")
