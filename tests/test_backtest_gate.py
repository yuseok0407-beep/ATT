"""사전 등록 게이트(`src/backtest/gate.py`)와 실험 실행기의 규칙을 고정한다.

이 테스트들이 지키는 것은 성능이 아니라 **절차**다. 게이트 기준이 조용히 느슨해지거나, 탐색
공간이 조용히 넓어지거나, 원장이 덮어쓰이거나, 홀드아웃이 실수로 열리는 것을 막는다 — 그 넷이
깨지면 원장에 쌓인 모든 행의 의미가 같이 사라진다.
"""

import importlib.util
import re
from pathlib import Path

import pandas as pd
import pytest

from src.backtest import gate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = PROJECT_ROOT / "docs" / "BACKTEST_PROTOCOL.md"


def _load_runner():
    """scripts/run_experiment.py를 모듈로 불러온다(패키지가 아니라 스크립트라서)."""
    spec = importlib.util.spec_from_file_location(
        "run_experiment", PROJECT_ROOT / "scripts" / "run_experiment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trade(symbol, pnl_r, step):
    return {"symbol": symbol, "pnl_r": pnl_r, "exit_index": step, "exit_step": step}


def _result(trades):
    return {"trades": trades, "blocked": {}}


def _df(n):
    return pd.DataFrame({"close": [1.0] * n})


# --- 지표 계산 -------------------------------------------------------------

def test_split_bounds_divides_each_symbol_evenly():
    bounds = gate.split_bounds({"A": _df(100), "B": _df(40)}, 1, n_splits=4)
    assert bounds["A"] == (25, 50)
    assert bounds["B"] == (10, 20)


def test_run_metrics_flags_a_losing_split():
    # 4분할 중 세 번째 구간(50~75)만 손실 -> all_splits_positive가 False여야 한다.
    trades = [_trade("A", 1.0, 10), _trade("A", 1.0, 30), _trade("A", -2.0, 60),
              _trade("A", 1.0, 90)]
    metrics = gate.run_metrics(_result(trades), {"A": _df(100)})
    assert metrics["total_r"] == pytest.approx(1.0)
    assert metrics["all_splits_positive"] is False
    assert metrics["splits"][2] == pytest.approx(-2.0)


def test_run_metrics_measures_symbol_concentration():
    trades = [_trade("A", 8.0, 10), _trade("B", 1.0, 20), _trade("C", 1.0, 30)]
    metrics = gate.run_metrics(_result(trades), {s: _df(40) for s in "ABC"})
    assert metrics["positive_symbols"] == 3
    assert metrics["top2_share"] == pytest.approx(9.0 / 10.0)


def test_run_metrics_leaves_ratios_undefined_when_total_r_is_negative():
    """총R이 음수면 '상위 2종목의 몫'과 '낙폭 대비 수익'은 의미가 없다.

    비율로 계산하면 부호가 뒤집혀 좋아 보이는 값이 나온다 — None으로 두고 게이트가 FAIL
    처리하게 한다."""
    metrics = gate.run_metrics(_result([_trade("A", -1.0, 10)]), {"A": _df(40)})
    assert metrics["top2_share"] is None
    assert metrics["recovery_factor"] is None


def test_summarize_reports_the_spread_across_seeds():
    results = [_result([_trade("A", r, 10)]) for r in (1.0, 2.0, 3.0)]
    summary = gate.summarize(results, {"A": _df(40)})
    assert summary["seeds"] == 3
    assert summary["total_r_median"] == pytest.approx(2.0)
    assert summary["total_r_p5"] == pytest.approx(1.0)
    assert summary["total_r_p95"] == pytest.approx(3.0)


def test_summarize_refuses_an_empty_run():
    with pytest.raises(ValueError):
        gate.summarize([], {"A": _df(40)})


# --- 판정 -----------------------------------------------------------------

def _passing_summary(**overrides):
    base = {
        "seeds": 20, "symbols": 12,
        "trades_median": 400,
        "total_r_median": 100.0, "total_r_p5": 60.0, "total_r_p95": 140.0,
        "mdd_r_median": -30.0,
        "oos_pass_rate": 1.0,
        "recovery_factor_median": 3.0,
        "top2_share_median": 0.4,
        "positive_symbol_share_median": 0.8,
    }
    return base | overrides


def test_evaluate_passes_when_every_criterion_is_met():
    verdict = gate.evaluate(_passing_summary(), _passing_summary(total_r_median=20.0))
    assert verdict["passed"] is True
    assert verdict["failed"] == []
    assert verdict["gate_version"] == gate.GATE_VERSION


@pytest.mark.parametrize("field, value, expected", [
    ("trades_median", gate.MIN_TRADES - 1, "trades"),
    ("total_r_p5", -1.0, "total_r_p5"),
    ("oos_pass_rate", 0.5, "oos_pass_rate"),
    ("recovery_factor_median", 1.0, "recovery_factor"),
    ("top2_share_median", 0.9, "top2_share"),
    ("positive_symbol_share_median", 0.3, "positive_symbol_share"),
])
def test_evaluate_fails_the_named_criterion(field, value, expected):
    verdict = gate.evaluate(_passing_summary(**{field: value}),
                            _passing_summary(total_r_median=20.0))
    assert verdict["passed"] is False
    assert verdict["failed"] == [expected]


def test_evaluate_fails_when_the_strategy_dies_under_stress_cost():
    """기본 비용에서 아무리 좋아도 슬리피지를 넣어 음수가 되면 통과시키지 않는다 —
    이 전략의 엣지가 체결 비용과 같은 크기라는 것이 이 프로젝트의 핵심 발견이다."""
    verdict = gate.evaluate(_passing_summary(total_r_median=500.0),
                            _passing_summary(total_r_median=-1.0))
    assert verdict["passed"] is False
    assert verdict["failed"] == ["stress_total_r"]


def test_evaluate_fails_when_ratios_are_undefined():
    verdict = gate.evaluate(_passing_summary(top2_share_median=None,
                                             recovery_factor_median=None),
                            _passing_summary(total_r_median=20.0))
    assert set(verdict["failed"]) == {"recovery_factor", "top2_share"}


# --- 문서와 코드의 일치 ----------------------------------------------------

def test_gate_thresholds_match_the_protocol_document():
    """기준값이 코드에만 있고 문서에는 옛날 값이 남는 상황을 막는다.

    이 프로젝트에서 실제로 두 번 사고가 난 유형이다(엔진 기본값이 실거래와 달랐던 건).
    기준을 바꾸려면 두 곳을 같이 고쳐야 하고, 그래야 '사전 등록'이라는 말이 성립한다."""
    text = PROTOCOL_PATH.read_text(encoding="utf-8")
    expected = {
        "trades": str(gate.MIN_TRADES),
        "total_r_p5": f"{gate.MIN_TOTAL_R_P5:.1f}",
        "oos_pass_rate": f"{gate.MIN_OOS_PASS_RATE:.2f}",
        "recovery_factor": f"{gate.MIN_RECOVERY_FACTOR:.1f}",
        "top2_share": f"{gate.MAX_TOP2_SHARE:.2f}",
        "positive_symbol_share": f"{gate.MIN_POSITIVE_SYMBOL_SHARE:.2f}",
        "stress_total_r": f"{gate.MIN_STRESS_TOTAL_R:.1f}",
    }
    for name, value in expected.items():
        row = next((line for line in text.splitlines()
                    if line.startswith(f"| `{name}`")), None)
        assert row is not None, f"프로토콜 표에 {name} 행이 없다"
        assert value in row, f"{name}: 문서({row.strip()})와 코드({value})가 다르다"

    assert f"{gate.STRESS_SLIPPAGE_R_PER_SIDE}" in text


def test_protocol_declares_the_same_holdout_length_as_the_runner():
    runner = _load_runner()
    assert f"최근 {runner.HOLDOUT_DAYS}일" in PROTOCOL_PATH.read_text(encoding="utf-8")


def test_searchable_parameters_are_all_listed_in_the_protocol():
    runner = _load_runner()
    text = PROTOCOL_PATH.read_text(encoding="utf-8")
    for name in runner.SEARCHABLE:
        assert f"| `{name}` |" in text, f"탐색 공간에 {name}이 있는데 문서 표에는 없다"


# --- 실행기의 절차 규칙 ----------------------------------------------------

def test_runner_rejects_a_parameter_outside_the_declared_search_space():
    runner = _load_runner()
    with pytest.raises(SystemExit) as excinfo:
        runner._parse_overrides(["leverage=20"])
    assert "탐색 공간" in str(excinfo.value)


def test_runner_parses_declared_parameters_with_their_types():
    runner = _load_runner()
    parsed = runner._parse_overrides(["stop_loss_pct=0.025", "max_concurrent_positions=4"])
    assert parsed == {"stop_loss_pct": 0.025, "max_concurrent_positions": 4}


def test_ledger_is_append_only(tmp_path, monkeypatch):
    runner = _load_runner()
    monkeypatch.setattr(runner, "LEDGER_PATH", tmp_path / "experiments.tsv")

    runner.append_ledger({"run_id": "E0001", "verdict": "FAIL", "window": "search"})
    runner.append_ledger({"run_id": "E0002", "verdict": "PASS", "window": "search"})

    rows = runner.read_ledger()
    assert [r["run_id"] for r in rows] == ["E0001", "E0002"]
    # 실패한 시도가 남아야 '몇 번 만에 나온 PASS인지'를 알 수 있다.
    assert rows[0]["verdict"] == "FAIL"


def test_ledger_round_trips_every_declared_column(tmp_path, monkeypatch):
    runner = _load_runner()
    monkeypatch.setattr(runner, "LEDGER_PATH", tmp_path / "experiments.tsv")
    row = {c: f"v_{c}" for c in runner.LEDGER_COLUMNS}
    runner.append_ledger(row)
    assert runner.read_ledger() == [row]


def test_ledger_note_cannot_break_the_tsv_columns(tmp_path, monkeypatch):
    """메모에 탭이 들어가면 열이 밀려 원장 전체가 오독된다."""
    runner = _load_runner()
    monkeypatch.setattr(runner, "LEDGER_PATH", tmp_path / "experiments.tsv")
    argv = ["--note", "a\tb"]
    parsed = " ".join(argv)  # 실행기는 note를 기록 직전에 치환한다
    assert "\t" in parsed
    runner.append_ledger({"run_id": "E0001", "note": "a\tb".replace("\t", " ")})
    assert runner.read_ledger()[0]["note"] == "a b"


def test_holdout_is_declared_as_sealed_in_the_protocol():
    text = PROTOCOL_PATH.read_text(encoding="utf-8")
    assert "--confirm-holdout" in text
    assert re.search(r"홀드아웃.*FAIL.*재도전하지 않는다|재도전하지 않는다", text, re.S)
