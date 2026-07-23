from app.notifications import TelegramNotifier


def test_missing_credentials_do_not_send_message():
    notifier = TelegramNotifier(bot_token="", chat_id="")
    assert notifier.send("hello") is False
