import subprocess
import sys


def test_cli_backtest_timeline_help_runs():
    result = subprocess.run(
        [sys.executable, "main.py", "--mode", "backtest", "--help"],
        cwd="/Users/snehith/Work/Development/Trading",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "start-date" in result.stdout
    assert "end-date" in result.stdout
