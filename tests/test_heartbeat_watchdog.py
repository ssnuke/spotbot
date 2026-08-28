import json
import time

from scripts import heartbeat_watchdog as watchdog


def test_read_heartbeat_age_returns_none_when_file_missing(tmp_path):
    assert watchdog._read_heartbeat_age(str(tmp_path / "missing")) is None


def test_read_heartbeat_age_reflects_file_mtime(tmp_path):
    heartbeat_file = tmp_path / "HEARTBEAT"
    heartbeat_file.write_text(str(time.time()))

    age = watchdog._read_heartbeat_age(str(heartbeat_file))

    assert age is not None
    assert age < 1.0


def test_alert_state_round_trips(tmp_path):
    state_file = str(tmp_path / "state.json")
    watchdog._save_alert_state(state_file, {"stale_since": 123.0, "last_alert": 456.0})

    assert watchdog._load_alert_state(state_file) == {"stale_since": 123.0, "last_alert": 456.0}


def test_load_alert_state_defaults_when_missing_or_corrupt(tmp_path):
    missing = str(tmp_path / "missing.json")
    assert watchdog._load_alert_state(missing) == {"stale_since": None, "last_alert": None}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json")
    assert watchdog._load_alert_state(str(corrupt)) == {"stale_since": None, "last_alert": None}


def test_main_sends_alert_when_heartbeat_missing_and_persists_stale_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    sent = []
    monkeypatch.setattr(watchdog, "_send_telegram_message", lambda token, chat, text: sent.append(text) or True)
    monkeypatch.setattr(watchdog, "_systemd_unit_status", lambda unit: "active")

    heartbeat_file = tmp_path / "HEARTBEAT"  # never written -- simulates a dead process
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "heartbeat_watchdog.py",
            "--heartbeat-file", str(heartbeat_file),
            "--state-file", str(state_file),
            "--max-age-seconds", "900",
        ],
    )

    exit_code = watchdog.main()

    assert exit_code == 0
    assert len(sent) == 1
    assert "DEAD-MAN'S SWITCH" in sent[0]
    state = json.loads(state_file.read_text())
    assert state["stale_since"] is not None
    assert state["last_alert"] is not None


def test_main_does_not_realert_within_repeat_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    sent = []
    monkeypatch.setattr(watchdog, "_send_telegram_message", lambda token, chat, text: sent.append(text) or True)
    monkeypatch.setattr(watchdog, "_systemd_unit_status", lambda unit: "active")

    heartbeat_file = tmp_path / "HEARTBEAT"
    state_file = tmp_path / "state.json"
    watchdog._save_alert_state(str(state_file), {"stale_since": time.time(), "last_alert": time.time()})
    monkeypatch.setattr(
        "sys.argv",
        [
            "heartbeat_watchdog.py",
            "--heartbeat-file", str(heartbeat_file),
            "--state-file", str(state_file),
            "--max-age-seconds", "900",
            "--repeat-seconds", "1800",
        ],
    )

    exit_code = watchdog.main()

    assert exit_code == 0
    assert sent == []  # still within the repeat window -- must not spam


def test_main_sends_recovery_message_once_heartbeat_is_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    sent = []
    monkeypatch.setattr(watchdog, "_send_telegram_message", lambda token, chat, text: sent.append(text) or True)

    heartbeat_file = tmp_path / "HEARTBEAT"
    heartbeat_file.write_text(str(time.time()))  # fresh
    state_file = tmp_path / "state.json"
    watchdog._save_alert_state(str(state_file), {"stale_since": time.time() - 1000, "last_alert": time.time() - 1000})
    monkeypatch.setattr(
        "sys.argv",
        [
            "heartbeat_watchdog.py",
            "--heartbeat-file", str(heartbeat_file),
            "--state-file", str(state_file),
            "--max-age-seconds", "900",
        ],
    )

    exit_code = watchdog.main()

    assert exit_code == 0
    assert len(sent) == 1
    assert "recovered" in sent[0]
    state = json.loads(state_file.read_text())
    assert state == {"stale_since": None, "last_alert": None}


def test_main_exits_early_without_telegram_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["heartbeat_watchdog.py", "--heartbeat-file", str(tmp_path / "HEARTBEAT")]
    )

    exit_code = watchdog.main()

    assert exit_code == 1
