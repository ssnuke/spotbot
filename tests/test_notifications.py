import app.notifications as notifications_module
from app.notifications import TelegramNotifier


def test_missing_credentials_do_not_send_message():
    notifier = TelegramNotifier(bot_token="", chat_id="")
    assert notifier.send("hello") is False


class FakeResponse:
    def raise_for_status(self):
        pass


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
