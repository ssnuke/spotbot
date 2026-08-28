#!/usr/bin/env python3
"""Dead-man's-switch for the trading bot -- runs as its OWN systemd timer, entirely
independent of trading-bot.service (see deploy/heartbeat-watchdog.{service,timer}), so it
keeps working even if the bot process has died and systemd's own Restart= has already
given up (StartLimitBurst exceeded, disk full, host trouble, etc.). Deliberately does not
import anything from app/ -- a bug in the bot's own code must never be able to take this
down with it. Only stdlib plus python-dotenv (best-effort) and urllib for the Telegram call.

Checks trading-bot's HEARTBEAT file (written every main-loop tick by
LiveRunner._write_heartbeat_file); if it's missing or older than --max-age-seconds, alerts
via Telegram, using the same TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID the bot itself uses. Repeats
the alert every --repeat-seconds while still stale (so it can't be missed and forgotten),
and sends a one-time recovery message once the heartbeat is fresh again. Run on a short
timer (a few minutes) via cron/systemd -- this script itself does no looping or sleeping.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _read_heartbeat_age(heartbeat_path: str) -> float | None:
    """Returns seconds since the heartbeat file was last written, or None if it doesn't
    exist at all (treated as maximally stale by the caller)."""
    try:
        return time.time() - os.path.getmtime(heartbeat_path)
    except OSError:
        return None


def _load_alert_state(state_path: str) -> dict:
    try:
        with open(state_path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"stale_since": None, "last_alert": None}


def _save_alert_state(state_path: str, state: dict) -> None:
    try:
        with open(state_path, "w") as handle:
            json.dump(state, handle)
    except OSError as exc:
        print(f"Failed to persist watchdog alert state: {exc}")


def _systemd_unit_status(unit_name: str) -> str:
    """Best-effort -- systemctl may not exist or the unit may not be manageable by this
    user; either way the watchdog must still be able to alert on the heartbeat alone."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit_name], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=payload, timeout=10) as response:
            return response.status == 200
    except Exception as exc:
        print(f"Failed to send Telegram alert: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat-file", default=os.getenv("HEARTBEAT_FILE", "HEARTBEAT"))
    parser.add_argument("--state-file", default=os.getenv("WATCHDOG_STATE_FILE", "watchdog_state.json"))
    parser.add_argument("--max-age-seconds", type=float, default=float(os.getenv("HEARTBEAT_MAX_AGE_SECONDS", 900)))
    parser.add_argument("--repeat-seconds", type=float, default=float(os.getenv("WATCHDOG_REPEAT_SECONDS", 1800)))
    parser.add_argument("--systemd-unit", default=os.getenv("WATCHDOG_SYSTEMD_UNIT", "trading-bot"))
    args = parser.parse_args()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- watchdog cannot alert, exiting")
        return 1

    age = _read_heartbeat_age(args.heartbeat_file)
    is_stale = age is None or age > args.max_age_seconds
    state = _load_alert_state(args.state_file)
    now = time.time()

    if is_stale:
        unit_status = _systemd_unit_status(args.systemd_unit)
        age_desc = "no heartbeat file found" if age is None else f"last heartbeat {age / 60:.1f} min ago"
        first_time_stale = state.get("stale_since") is None
        if state.get("stale_since") is None:
            state["stale_since"] = now
        due_for_alert = first_time_stale or (
            state.get("last_alert") is None or now - state["last_alert"] >= args.repeat_seconds
        )
        if due_for_alert:
            stale_minutes = (now - state["stale_since"]) / 60
            _send_telegram_message(
                bot_token,
                chat_id,
                f"\U0001F480 DEAD-MAN'S SWITCH: trading bot heartbeat is stale ({age_desc}).\n"
                f"Stale for {stale_minutes:.1f} min. systemd unit '{args.systemd_unit}' is "
                f"'{unit_status}'.\nThis alert repeats every {args.repeat_seconds / 60:.0f} "
                f"min until the heartbeat recovers -- check the VPS now.",
            )
            state["last_alert"] = now
        print(f"Heartbeat stale ({age_desc}), unit={unit_status}")
    else:
        if state.get("stale_since") is not None:
            _send_telegram_message(
                bot_token,
                chat_id,
                f"✅ Trading bot heartbeat recovered (last heartbeat {age:.0f}s ago).",
            )
        state = {"stale_since": None, "last_alert": None}
        print(f"Heartbeat OK ({age:.0f}s old)")

    _save_alert_state(args.state_file, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
