"""어떤 계좌(env)들이 있고, 각 계좌의 파일이 어디에 있는지 — **이 규칙의 유일한 정의**.

2026-09-26 이전에는 11개 파일이 `LIVE_경로 if env == "live" else 데모경로`로 이걸 각자 적고
있었다. 그 모양은 env가 두 개일 때만 맞다 — 세 번째 계좌(demo2)를 넣으면 "live가 아니니까
데모"로 떨어져 **데모 저널과 상태 파일에 조용히 섞여 기록된다.** 그래서 경로는 여기서만
만들고, 모르는 env는 폴백하지 않고 예외를 던진다.

파일명 규칙은 원래 실계좌가 쓰던 그대로다: 데모는 이름 그대로(기존 이력이 안 끊기게), 나머지는
확장자 앞에 `.{env}` 인픽스(`futures_rule_trades.jsonl` → `futures_rule_trades.live.jsonl`).
"""

from pathlib import PurePosixPath

# 순서가 곧 화면/알림에 나오는 순서다.
ENVS = ("demo", "live", "demo2")

# 실제 자금이 오가는 계좌. 주문 가드(_guard_live)와 화면 경고색이 이걸 본다.
REAL_MONEY_ENVS = ("live",)


class UnknownEnvError(ValueError):
    """ENVS에 없는 env. 데모로 폴백하지 않는다 — 그게 이 모듈이 생긴 이유다."""


def check_env(env: str) -> str:
    if env not in ENVS:
        raise UnknownEnvError(f"unknown env {env!r}, expected one of {ENVS}")
    return env


def env_path(demo_path: str, env: str) -> str:
    """데모용 경로를 받아 그 env의 경로를 돌려준다.

    데모 경로를 기준으로 삼는 이유: 기존 상수(`JOURNAL_PATH` 등)가 전부 데모 경로이고, 실계좌
    경로는 늘 거기에 인픽스를 붙인 모양이었다 — 규칙을 새로 만든 게 아니라 있던 규칙을 한 곳에
    적은 것이다."""
    check_env(env)
    if env == "demo":
        return demo_path
    path = PurePosixPath(demo_path)
    return str(path.with_name(f"{path.stem}.{env}{path.suffix}"))


def label(env: str) -> str:
    """알림·화면에 찍는 계좌 이름(DEMO / LIVE / DEMO2)."""
    return check_env(env).upper()
