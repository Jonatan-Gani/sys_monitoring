# sys_monitoring

Lightweight Linux system monitor with a CSV log archive and an interactive
Telegram bot. Built for low overhead — the cron logger does one short
psutil pass per run, and the bot uses long-polling with a single keep-alive
HTTPS session.

## Components

| File | Role |
|------|------|
| `log_pi_status.py` | Cron-invoked metrics collector. Appends to `logs/power_log.csv`, archives daily, fires edge-triggered Telegram alerts. |
| `tg_bot_loop.py`   | Long-running interactive Telegram bot. |
| `sysmon_lib.py`    | Shared library: config, metric collection, CSV tail reads, formatters. |
| `config.json`      | Thresholds, alert policy, power model, bot tuning. |
| `.env`             | Secrets: `BOT_TOKEN`, `CHAT_ID`, `AUTHORIZED_USERS`. |

## Install

```bash
git clone https://github.com/Jonatan-Gani/sys_monitoring.git
cd sys_monitoring
pip install -r requirements.txt
```

`.env`:

```
BOT_TOKEN=123456:abc...
CHAT_ID=11111111
AUTHORIZED_USERS=11111111,22222222
```

`AUTHORIZED_USERS` is a comma-separated list of Telegram user IDs allowed to
talk to the bot. If empty, only `CHAT_ID` is permitted.

## Run

Cron the logger every minute:

```
* * * * * /usr/bin/python3 /path/to/sys_monitoring/log_pi_status.py
```

Run the bot as a service (recommended) or directly:

```bash
python3 tg_bot_loop.py
```

A minimal systemd unit:

```ini
[Unit]
Description=sys_monitoring Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/sys_monitoring
ExecStart=/usr/bin/python3 /path/to/sys_monitoring/tg_bot_loop.py
Restart=on-failure
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

## Bot commands

Compact-by-default — every screen has inline buttons to drill in.

| Command | What it shows |
|---|---|
| `/start`, `/menu`, `/status` | One-screen snapshot with action buttons |
| `/cpu` `/ram` `/disk` `/disks` `/temp` `/net` `/uptime` | Targeted live readings |
| `/top [cpu\|mem]` | Top processes (toggleable) |
| `/service <unit>` | systemd unit status |
| `/summary [hours]` | Min/avg/max + energy from CSV (default 24h) |
| `/latest` | Send current day's CSV |
| `/getlog` | Browse archived CSVs by year → month → day |
| `/alerts on\|off\|status` | Runtime alert toggle |
| `/threshold <name> <value>` | Tune a threshold without editing config |
| `/help` | Command reference |

Status icons: 🟢 below 85% of threshold, 🟡 85–100%, 🔴 over threshold.

## Alerts

Edge-triggered, not polled. The first crossing of a threshold sends an
alert; further crossings are suppressed until either `cooldown_minutes`
elapses (set in `config.json`) or the metric recovers, at which point a
single `✅ Recovered` message is sent (toggleable via `alerts.send_recovery`).

State (offset, alert flags, last metric snapshot for network deltas) lives
in `logs/state/monitor_state.json`.

## CSV columns

```
Timestamp, CPU Load (%), Temperature (C), RAM Usage (%), Disk Usage (%),
Net Sent Total (MB), Net Recv Total (MB),
Net Sent Delta (MB), Net Recv Delta (MB),
Load Avg 1m, Estimated Power (W), Interval Wh
```

Net `Delta` columns are per-interval (true traffic per run); `Total` columns
are cumulative since boot.

## Directory layout

```
sys_monitoring/
├── log_pi_status.py
├── tg_bot_loop.py
├── sysmon_lib.py
├── config.json
├── .env
├── requirements.txt
└── logs/
    ├── power_log.csv
    ├── monitor.log
    ├── bot_logs/telegram_bot.log
    ├── state/monitor_state.json
    └── log_archive/YYYY/Mon_MM/D_Weekday.csv
```
