import requests

import app.notifications as notifications_module
from app.notifications import TelegramNotifier


def test_missing_credentials_do_not_send_message():
    notifier = TelegramNotifier(bot_token="", chat_id="")
    assert notifier.send("hello") is False


class FakeResponse:
    def raise_for_status(self):
        pass


class FailingResponse:
    """Simulates Telegram's real 400 response for a Markdown parse error -- the exact failure
    mode that let every close-trade notification vanish silently before this was fixed."""

    status_code = 400

    def raise_for_status(self):
        raise requests.HTTPError("400 Client Error", response=self)

    def json(self):
        return {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities: Can't find end of Italic entity at byte offset 20"}


def test_rejected_send_prints_telegrams_actual_error(monkeypatch, capsys):
    # Regression: a rejected message (bad Markdown, etc.) used to just return False with
    # nothing printed anywhere -- indistinguishable from a network hiccup, and the actual
    # reason (visible only in Telegram's response body) was lost entirely.
    monkeypatch.setattr(notifications_module.requests, "post", lambda url, json, timeout: FailingResponse())
    notifier = TelegramNotifier(bot_token="token", chat_id="123")

    result = notifier.send("bad _markdown", parse_mode="Markdown")

    assert result is False
    captured = capsys.readouterr()
    assert "Telegram send failed" in captured.out
    assert "Can't find end of Italic entity" in captured.out


def test_send_includes_parse_mode_when_given(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(json)
        return FakeResponse()

    monkeypatch.setattr(notifications_module.requests, "post", fake_post)
    notifier = TelegramNotifier(bot_token="token", chat_id="123")

    notifier.send("hello", parse_mode="Markdown")
    assert captured["parse_mode"] == "Markdown"


def test_send_omits_parse_mode_by_default(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(json)
        return FakeResponse()

    monkeypatch.setattr(notifications_module.requests, "post", fake_post)
    notifier = TelegramNotifier(bot_token="token", chat_id="123")

    notifier.send("hello")
    assert "parse_mode" not in captured
