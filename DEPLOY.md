# Deployment Guide

Runs the bot 24/7 on a small VPS via systemd, so it survives reboots and restarts
automatically if it ever crashes. State (open positions, cumulative PnL, risk
counters) persists to a local SQLite file, so a restart doesn't lose history.

Target: a 1 vCPU / 1GB RAM / Ubuntu 22.04 or 24.04 (or Debian 12) VPS. This is
comfortably more than the bot needs — it's idle almost all the time, waking up
every `poll_interval_seconds` (default 300s) to check prices.

## 1. Provision the server

Spin up the smallest Ubuntu 22.04/24.04 or Debian 12 instance from your VPS
provider. Note its public IP.

SSH in as root (or whatever the provider gives you) and create a dedicated
non-root user to run the bot under — don't run it as root:

```bash
adduser botuser
usermod -aG sudo botuser   # optional, only if you need sudo later
su - botuser
```

From here on, run everything as `botuser`.

## 2. Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
```

Confirm Python 3.11+ is available:

```bash
python3 --version
```

## 3. Add swap (cheap insurance on a 1GB box)

Most 1GB VPS plans ship with no swap. This workload shouldn't need it, but it's
free insurance against an unexpected memory spike killing the process:

```bash
sudo fallocate -l 512M /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Get the code onto the server

Either clone from your own git remote, or copy the project up directly:

```bash
# Option A: git clone (if you've pushed this repo somewhere)
git clone <your-repo-url> ~/trading-bot

# Option B: copy from your local machine (run this from your laptop, not the server)
scp -r /Users/snehith/Work/Development/Trading botuser@<server-ip>:~/trading-bot
```

Either way, end up with the project at `~/trading-bot` on the server.

## 5. Set up the Python environment

```bash
cd ~/trading-bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 6. Configure secrets

Never commit real secrets — `.env` should stay untracked (check it's in
`.gitignore` if you added one).

```bash
cp .env.example .env
nano .env
```

For the default `simulated` execution mode (no exchange account, fills against
live mainnet prices), you only need:

```
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHAT_ID=your_chat_id
```

Leave `BINANCE_API_KEY` / `BINANCE_API_SECRET` blank unless you're switching
`live.execution_mode` to `"testnet"` in `config.json`.

To get the Telegram values: message `@BotFather` → `/newbot` → copy the token.
Then message `@userinfobot` to get your numeric chat ID.

## 7. Sanity-check it runs in the foreground first

Before wiring up systemd, run it directly and confirm it connects and logs
correctly. Let it sit for a couple of minutes, then `Ctrl+C`:

```bash
python3 main.py --mode bot --verbose
```

You should see a "Starting live paper trading (simulated ...)" line, then
periodic "No trading signal generated" (or an opened position) roughly every
5 minutes. If Telegram is configured, you won't get a message until an actual
trade opens or closes — that's expected, not a bug.

## 8. Install the systemd service

A template unit file is at `deploy/trading-bot.service`. Copy it and fix the
paths/user to match your setup — **the path substitution must run before the
username substitution**, otherwise `/home/botuser/...` silently becomes
`/home/<user>/...` even when running as `root` (whose real home is `/root`,
not `/home/root`), leaving a stale path that breaks systemd's sandboxing:

```bash
sed -e "s#/home/botuser/trading-bot#$HOME/trading-bot#g" -e "s/botuser/$(whoami)/g" \
  deploy/trading-bot.service | sudo tee /etc/systemd/system/trading-bot.service
```

If you're running this as `root` (common on VPS providers that only give you
root SSH access directly), double-check the result — `WorkingDirectory`,
`ExecStart`, and `ReadWritePaths` should all read `/root/trading-bot`, not
`/home/root/trading-bot`:

```bash
grep -E "WorkingDirectory|ExecStart|ReadWritePaths|User=" /etc/systemd/system/trading-bot.service
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
sudo systemctl status trading-bot
```

`status` should show `active (running)`. If it shows `failed`, check logs
(next step) before troubleshooting further.

## 9. Watch it run

```bash
journalctl -u trading-bot -f
```

`-f` follows the log live, same as `tail -f`. `Ctrl+C` to stop watching (the
service keeps running in the background).

## 10. Talking to the bot on Telegram

Beyond automatic notifications (bot started/stopped, trade opened/closed,
periodic status updates every `live.summary_interval_seconds`, default
hourly), the bot listens for commands sent to it in your configured chat:

- `/status` — open positions, cumulative PnL, win/loss counts, capital, cooldown state
- `/pnl` — leaner performance view: cumulative PnL, win/loss counts, avg PnL per trade
- `/trades` or `/openpositions` — details of any currently open trade(s)
- `/history [N]` — last N closed trades with entry/exit price, exit reason, and PnL
  for each (default 5, capped at 20, e.g. `/history 10`)
- `/price` — current market price for the configured symbol
- `/pause` — stop opening new positions; existing ones are still monitored and will
  exit normally (stop-loss/take-profit/trailing-stop keep working while paused)
- `/resume` — resume opening new positions after a `/pause`
- `/kill` or `/stop` — remote kill switch: shuts the bot down gracefully (same
  clean-shutdown path as `systemctl stop`/SIGTERM) without needing SSH access
- `/help` — lists all commands

Only messages from the `TELEGRAM_CHAT_ID` configured in `.env` get a reply —
commands from any other chat are silently ignored.

Sending `systemctl stop`, `Ctrl+C`, any signal that terminates the process
(SIGTERM/SIGINT), or the `/kill`/`/stop` Telegram command all trigger the same
graceful shutdown: the bot finishes its current loop iteration, sends a final
"Bot stopped" summary to Telegram, and exits within a few seconds — it does not
need to wait out a full poll interval. `/kill` is the one that works from your
phone without needing terminal access to the server.

## 11. Common operations

```bash
sudo systemctl stop trading-bot       # stop the bot
sudo systemctl start trading-bot      # start it again
sudo systemctl restart trading-bot    # restart (e.g. after editing .env or config.json)
sudo systemctl disable trading-bot    # stop it from auto-starting on reboot
journalctl -u trading-bot --since "1 hour ago"   # recent logs
```

After pulling code changes or editing `config.json`/`.env`, always
`sudo systemctl restart trading-bot` to pick them up — the running process
won't reload them on its own.

## 12. Back up the state file

`live_state.db` is the only record of cumulative PnL, win/loss counts, and any
open positions if the server is ever lost. Back it up periodically:

```bash
scp botuser@<server-ip>:~/trading-bot/live_state.db ./backups/live_state-$(date +%F).db
```

Consider a small cron job on your local machine (or another server) to do this
daily rather than relying on remembering to do it manually.

## 13. Basic security hardening (recommended, not required to run)

- Disable SSH password auth, use key-based auth only.
- Set up `ufw` and only allow SSH (and nothing else — this bot makes outbound
  connections only, it doesn't need any inbound ports open):
  ```bash
  sudo ufw allow OpenSSH
  sudo ufw enable
  ```
- Keep `.env` permissions locked down: `chmod 600 .env`.

## 14. Going live with real money

Everything above runs in `simulated` mode by default: no exchange account, no
real orders, fills happen locally against live prices. `execution_mode: "live"`
places REAL orders with REAL funds on Binance. Don't skip any step here —
this is the one part of the project where a bug costs real money instead of a
wrong number in a report.

1. **Create a trading-only API key.** In Binance's API Management settings,
   create a new key with **trading permission only** — leave withdrawals
   disabled. If your provider's IP is static, IP-whitelist the key to it. Never
   reuse your Spot Testnet key/secret here; they're for a completely different
   system.

2. **Add the live credentials to `.env`**, as their own distinct variables
   (kept separate from the testnet ones on purpose, so a copy-paste mistake
   can't send real orders with testnet credentials or vice versa):

   ```text
   BINANCE_LIVE_API_KEY=your_real_key
   BINANCE_LIVE_API_SECRET=your_real_secret
   ```

   The bot refuses to start in `live` mode if this key can withdraw funds, or
   can't trade at all — that's checked automatically on every startup.

3. **Prove the new code path on Testnet first.** The `live` order-execution
   code shares its implementation with the already-proven `testnet` path, but
   validate it end to end before ever pointing it at real funds: temporarily
   set `"execution_mode": "testnet"` in `config.json`, restart, and confirm
   orders place and fill correctly (`/status`, `/trades`, and the Telegram
   open/close notifications should all look normal) for at least a few real
   trade cycles.

4. **Start with a small trial amount, not your full intended capital.**
   Backtest and testnet validation don't fully capture real execution quirks
   (fills, real fees, rejected orders). Set `"capital"` in the `risk` section
   of `config.json` to something you're fully comfortable being at risk while
   you observe it — $50-100, not $300+ — and only scale up after it's proven
   itself live for a week or two.

5. **Set `"execution_mode": "live"`** in `config.json`'s `"live"` section and
   restart (`sudo systemctl restart trading-bot`). Watch the startup message
   (Telegram and `journalctl -u trading-bot -f`) — it will clearly say
   `Mode: 🔴 LIVE — REAL MONEY`. If you see `testnet` or `simulated` instead,
   the config change didn't take (check you edited the right file and
   restarted).

6. **Watch it closely for the first day.** Errors are now always printed to
   the journal (not just with `--verbose`), so `journalctl -u trading-bot -f`
   is your primary window into anything going wrong.

`market_type` (also in the `live` section) only supports `"spot"` today.
Binance Futures is a different, higher-risk product (leverage, liquidation,
funding rates) with its own API — the config field is reserved for it, but
nothing beyond spot is implemented.

## Updating the bot later

```bash
cd ~/trading-bot
git pull                 # or re-upload changed files
source .venv/bin/activate
pip install -r requirements.txt   # in case dependencies changed
sudo systemctl restart trading-bot
```
