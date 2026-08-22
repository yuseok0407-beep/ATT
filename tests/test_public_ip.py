import pytest

from src.data import public_ip


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _reset_cache():
    """모듈 전역 캐시(_cache)를 쓰므로 테스트 간에 값이 새어나가지 않도록 매번 초기화한다."""
    public_ip._cache["ip"] = None
    public_ip._cache["fetched_at"] = 0.0
    yield
    public_ip._cache["ip"] = None
    public_ip._cache["fetched_at"] = 0.0


def test_get_public_ip_returns_fetched_value(monkeypatch):
    monkeypatch.setattr(public_ip.requests, "get", lambda url, timeout=None: _FakeResponse("1.2.3.4"))
    assert public_ip.get_public_ip() == "1.2.3.4"


def test_get_public_ip_caches_within_ttl(monkeypatch):
    """바이낸스 IP 화이트리스트 비교용으로 매 5초 폴링마다(대시보드) 또는 15초 폴링마다(텔레그램
    봇) 이 외부 서비스를 두드리면 안 된다 — TTL 안에서는 실제 조회 없이 캐시된 값을 재사용."""
    call_count = {"n": 0}

    def _fake_get(url, timeout=None):
        call_count["n"] += 1
        return _FakeResponse("5.6.7.8")

    monkeypatch.setattr(public_ip.requests, "get", _fake_get)

    assert public_ip.get_public_ip() == "5.6.7.8"
    assert public_ip.get_public_ip() == "5.6.7.8"
    assert call_count["n"] == 1


def test_get_public_ip_refetches_after_ttl_expires(monkeypatch):
    call_count = {"n": 0}

    def _fake_get(url, timeout=None):
        call_count["n"] += 1
        return _FakeResponse("9.9.9.9")

    monkeypatch.setattr(public_ip.requests, "get", _fake_get)

    public_ip.get_public_ip(cache_seconds=0)
    public_ip.get_public_ip(cache_seconds=0)
    assert call_count["n"] == 2


def test_get_public_ip_returns_previous_value_on_fetch_failure(monkeypatch):
    """조회 실패 시(네트워크 hiccup 등) 이전에 성공했던 값이라도 있으면 그걸 그대로 돌려준다 —
    IP가 실제로 안 바뀌었는데도 "확인 불가"로 화면이 흔들리는 걸 막기 위함."""
    monkeypatch.setattr(public_ip.requests, "get", lambda url, timeout=None: _FakeResponse("1.1.1.1"))
    assert public_ip.get_public_ip() == "1.1.1.1"

    def _raise(url, timeout=None):
        raise Exception("network error")

    monkeypatch.setattr(public_ip.requests, "get", _raise)
    assert public_ip.get_public_ip(cache_seconds=0) == "1.1.1.1"


def test_get_public_ip_returns_none_when_never_fetched_and_fails(monkeypatch):
    def _raise(url, timeout=None):
        raise Exception("network error")

    monkeypatch.setattr(public_ip.requests, "get", _raise)
    assert public_ip.get_public_ip() is None
