from app.telemetry import Telemetry


def test_telemetry_logs_messages_when_enabled():
    telemetry = Telemetry(enabled=True, logger=None)
    telemetry.log("test message")

    assert len(telemetry.entries) == 1
    assert "test message" in telemetry.entries[0]


def test_telemetry_does_not_log_when_disabled():
    telemetry = Telemetry(enabled=False, logger=None)
    telemetry.log("ignored")

    assert telemetry.entries == []
