"""계좌(env)와 계좌별 파일 경로 — 세 번째 계좌(demo2)가 데모 파일에 섞이지 않는지(2026-09-26)."""

import pytest

from src.core.envs import ENVS, UnknownEnvError, check_env, env_path, label
from src.execution import env_paths


def test_demo_paths_are_the_historical_file_names():
    """데모 파일명이 바뀌면 기존 이력이 끊긴다. conftest가 tmp로 갈아끼우기 전 값이라 모듈을
    다시 읽지 않고 규칙만 확인한다."""
    assert env_path("journal/futures_rule_trades.jsonl", "demo") == "journal/futures_rule_trades.jsonl"


def test_live_paths_keep_the_existing_infix():
    assert env_path("journal/futures_rule_trades.jsonl", "live") == "journal/futures_rule_trades.live.jsonl"
    assert env_path("state/futures_rule_daily_equity.json", "live") == "state/futures_rule_daily_equity.live.json"
    assert env_path("logs/futures_rule_bot.log", "live") == "logs/futures_rule_bot.live.log"


def test_demo2_gets_its_own_infix():
    assert env_path("journal/futures_rule_trades.jsonl", "demo2") == "journal/futures_rule_trades.demo2.jsonl"


def test_unknown_env_is_an_error_not_a_demo_fallback():
    """이 모듈이 생긴 이유 — "live가 아니면 데모"는 세 번째 계좌를 데모 파일로 보낸다."""
    with pytest.raises(UnknownEnvError):
        env_path("journal/futures_rule_trades.jsonl", "demo3")
    with pytest.raises(UnknownEnvError):
        env_paths.paths_for("demo3")
    with pytest.raises(UnknownEnvError):
        check_env("")


def test_every_env_has_disjoint_files():
    """어느 두 계좌도 같은 파일을 하나라도 공유하면 안 된다."""
    seen = {}
    for env in ENVS:
        for key, path in env_paths.paths_for(env).items():
            assert path not in seen, f"{env}.{key} shares {path} with {seen.get(path)}"
            seen[path] = f"{env}.{key}"


def test_labels():
    assert [label(env) for env in ENVS] == ["DEMO", "LIVE", "DEMO2"]


def test_bot_module_constants_still_match_the_resolver():
    """옛 상수(LIVE_JOURNAL_PATH 등)를 import하는 코드가 남아 있어도 같은 파일을 가리켜야 한다."""
    from src import futures_rule_bot as bot
    assert bot.LIVE_JOURNAL_PATH == "journal/futures_rule_trades.live.jsonl"
    assert bot.LIVE_STATE_PATH == "state/futures_rule_daily_equity.live.json"
    assert bot.JOURNAL_PATH == "journal/futures_rule_trades.jsonl"
