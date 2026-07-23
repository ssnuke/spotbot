import subprocess
import sys


def test_cli_help_runs():
    result = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd="/Users/snehith/Work/Development/Trading",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "backtest" in result.stdout.lower()
