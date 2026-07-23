from __future__ import annotations

import os
from typing import Optional

import requests


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    def send(self, message: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False
