from unittest.mock import MagicMock

import pytest

from src.execution import telegram_client


def test_send_message_posts_to_correct_url_and_payload(monkeypatch):
    mock_post = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr(telegram_client.requests, "post", mock_post)

    result = telegram_client.send_message("hello", chat_id="123", token="tok")

    assert result is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/bottok/sendMessage"
    assert kwargs["json"] == {"chat_id": "123", "text": "hello"}


def test_send_message_returns_false_without_raising_on_network_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr(telegram_client.requests, "post", _raise)

    assert telegram_client.send_message("hello", chat_id="123", token="tok") is False


def test_send_message_returns_false_when_token_or_chat_id_missing(monkeypatch):
    monkeypatch.setattr(telegram_client, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(telegram_client, "TELEGRAM_CHAT_ID", "")
    assert telegram_client.send_message("hello") is False


def test_get_updates_returns_result_list(monkeypatch):
    mock_get = MagicMock()
    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = {"ok": True, "result": [{"update_id": 1}]}
    monkeypatch.setattr(telegram_client.requests, "get", mock_get)

    result = telegram_client.get_updates(offset=5, timeout=20, token="tok")

    assert result == [{"update_id": 1}]
    args, kwargs = mock_get.call_args
    assert args[0] == "https://api.telegram.org/bottok/getUpdates"
    assert kwargs["params"] == {"timeout": 20, "offset": 5}


def test_get_updates_omits_offset_when_not_given(monkeypatch):
    mock_get = MagicMock()
    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = {"result": []}
    monkeypatch.setattr(telegram_client.requests, "get", mock_get)

    telegram_client.get_updates(timeout=20, token="tok")

    assert "offset" not in mock_get.call_args.kwargs["params"]


def test_get_updates_returns_empty_list_on_network_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr(telegram_client.requests, "get", _raise)

    assert telegram_client.get_updates(token="tok") == []


def test_get_updates_returns_empty_list_when_token_missing(monkeypatch):
    monkeypatch.setattr(telegram_client, "TELEGRAM_BOT_TOKEN", "")
    assert telegram_client.get_updates() == []
