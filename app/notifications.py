from __future__ import annotations

import os
from typing import Optional

import requests


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    def send(self, message: str, parse_mode: Optional[str] = None) -> bool:
        if not self.bot_token or not self.chat_id:
            return False

        payload = {"chat_id": self.chat_id, "text": message}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            # Printed, not just returned as False -- a rejected message (e.g. a Markdown parse
            # error from an unescaped "_" in dynamic text) previously vanished with no trace
            # anywhere: no exception, no log line, nothing. This was the actual cause of trade
            # close notifications silently never arriving despite the trade closing correctly.
            detail = self._describe_error(exc)
            print(f"Telegram send failed: {detail}")
            return False

    @staticmethod
    def _describe_error(exc: requests.RequestException) -> str:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
                description = body.get("description")
                if description:
                    return f"{response.status_code}: {description}"
            except ValueError:
                pass
        return str(exc)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> list:
        """Fetches new incoming messages (e.g. slash commands) sent to the bot."""
        if not self.bot_token or not self.chat_id:
            return []

        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        try:
            response = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
                params=params,
                timeout=timeout + 10,
            )
            response.raise_for_status()
            return response.json().get("result", [])
        except requests.RequestException:
            return []
