import pytest

from src.execution import excursion


def _path(tmp_path):
    return str(tmp_path / "excursion.json")


# ---------- to_r ----------

def test_to_r_measures_the_stop_as_exactly_minus_one():
    assert excursion.to_r(99.0, 100.0, 99.0, "long") == pytest.approx(-1.0)
    assert excursion.to_r(101.0, 100.0, 101.0, "short") == pytest.approx(-1.0)


def test_to_r_flips_sign_for_shorts():
    # 숏은 가격이 내려가야 이익이다
    assert excursion.to_r(98.0, 100.0, 101.0, "short") == pytest.approx(2.0)
    assert excursion.to_r(98.0, 100.0, 99.0, "long") == pytest.approx(-2.0)


def test_to_r_returns_none_when_the_risk_is_unusable():
    assert excursion.to_r(100.0, 100.0, 100.0, "long") is None  # 손절폭 0
    assert excursion.to_r(100.0, 100.0, None, "long") is None


# ---------- update / pop ----------

def test_update_tracks_the_best_and_worst_points(tmp_path):
    path = _path(tmp_path)
    for mark in (101.0, 101.6, 100.4, 99.5, 100.2):
        excursion.update("BTC/USDT:USDT", entry_price=100.0, stop_loss_price=99.0,
                         mark_price=mark, side="long", path=path)

    record = excursion.read_all(path=path)["BTC/USDT:USDT"]
    assert record["max_favorable_r"] == pytest.approx(1.6)
    assert record["max_adverse_r"] == pytest.approx(-0.5)
    assert record["current_r"] == pytest.approx(0.2)


def test_update_starts_over_when_a_new_position_replaces_the_old_one(tmp_path):
    """같은 심볼에 새 포지션이 열리면(진입가/손절가가 달라지면) 이전 포지션의 최고점이
    새 포지션에 섞이면 안 된다."""
    path = _path(tmp_path)
    excursion.update("BTC/USDT:USDT", entry_price=100.0, stop_loss_price=99.0,
                     mark_price=103.0, side="long", path=path)
    excursion.update("BTC/USDT:USDT", entry_price=200.0, stop_loss_price=198.0,
                     mark_price=200.5, side="long", path=path)

    record = excursion.read_all(path=path)["BTC/USDT:USDT"]
    assert record["max_favorable_r"] == pytest.approx(0.25)
    assert record["entry_price"] == 200.0


def test_update_keeps_symbols_separate(tmp_path):
    path = _path(tmp_path)
    excursion.update("BTC/USDT:USDT", entry_price=100.0, stop_loss_price=99.0,
                     mark_price=102.0, side="long", path=path)
    excursion.update("ETH/USDT:USDT", entry_price=50.0, stop_loss_price=49.5,
                     mark_price=49.75, side="long", path=path)

    data = excursion.read_all(path=path)
    assert data["BTC/USDT:USDT"]["max_favorable_r"] == pytest.approx(2.0)
    assert data["ETH/USDT:USDT"]["max_favorable_r"] == pytest.approx(-0.5)


def test_update_does_nothing_when_the_stop_is_unknown(tmp_path):
    path = _path(tmp_path)
    assert excursion.update("BTC/USDT:USDT", entry_price=100.0, stop_loss_price=None,
                            mark_price=101.0, side="long", path=path) is None
    assert excursion.read_all(path=path) == {}


def test_pop_returns_and_clears_the_record(tmp_path):
    path = _path(tmp_path)
    excursion.update("BTC/USDT:USDT", entry_price=100.0, stop_loss_price=99.0,
                     mark_price=101.5, side="long", path=path)

    record = excursion.pop("BTC/USDT:USDT", path=path)
    assert record["max_favorable_r"] == pytest.approx(1.5)
    assert excursion.read_all(path=path) == {}
    assert excursion.pop("BTC/USDT:USDT", path=path) is None


def test_read_all_survives_a_corrupted_file(tmp_path):
    path = _path(tmp_path)
    open(path, "w", encoding="utf-8").write("{broken")
    assert excursion.read_all(path=path) == {}
