# sys_monitoring

Lightweight Linux system monitor with a SQLite-backed time-series store and
an interactive Telegram bot. Built for low overhead — the cron logger does
one short psutil pass per run and a single SQL insert, and the bot uses
long-polling with a keep-alive HTTPS session. SQLite WAL mode lets the bot
read freely while the logger writes.

## Components

| File | Role |
|------|------|
| `log_pi_status.py` | Cron-invoked metrics collector. Inserts one row into `logs/sysmon.db`, fires edge-triggered Telegram alerts, prunes per retention. |
| `tg_bot_loop.py`   | Long-running interactive Telegram bot. |
| `sysmon_lib.py`    | Shared library: SQLite layer, config, metric collection, formatters, legacy-CSV importer. |
| `config.json`      | Thresholds, alert policy, power model, retention, bot tuning. |
| `.env`             | Secrets: `BOT_TOKEN`, `CHAT_ID`, `AUTHORIZED_USERS`. |

## Storage

Metrics live in `logs/sysmon.db` (SQLite, WAL mode). Schema:

```sql
metrics       (ts INTEGER PK, cpu_load, temperature, ram_usage, disk_usage,
               net_*_total_mb, net_*_delta_mb, load_avg_1m, power_w, interval_wh)
alert_events  (id, ts, metric, event['breach'|'recovery'|'continued'], value, threshold)
schema_meta   (key, value)
```

At ~1 row/min the DB grows about 50 MB/year. Retention is enforced via
`storage.retention_days` (default 365; `0` = forever). On first run after
upgrade, existing `logs/power_log.csv` and `logs/log_archive/**/*.csv` files
are imported automatically (idempotent — re-runs are no-ops).

Runs on **Linux** and **Windows**. On Linux it reads `/sys/class/thermal`
for temperature and uses `systemctl` for `/service`. On Windows it uses
psutil's WMI-backed sensors (temperature usually shows `n/a` — Windows
doesn't expose CPU thermals through a standard API) and `psutil.win_service_get`
for `/service`.

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

## Run · Linux

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

## Run · Windows

Schedule the logger every minute with Task Scheduler. From an elevated
PowerShell prompt (replace paths to match your install):

```powershell
$exe = "C:\Python312\python.exe"
$arg = "C:\path\to\sys_monitoring\log_pi_status.py"
schtasks /Create /TN "sys_monitoring_logger" `
  /TR "`"$exe`" `"$arg`"" `
  /SC MINUTE /MO 1 /RL HIGHEST /F
```

Run the bot. For an interactive test:

```powershell
python tg_bot_loop.py
```

For a hands-off persistent run, either:

1. **Task Scheduler** with trigger "At log on" / "At startup" and action
   `pythonw.exe C:\path\to\sys_monitoring\tg_bot_loop.py` (note `pythonw.exe`
   — runs without a console window). Tick "If the task fails, restart every
   1 minute".
2. **NSSM** (Non-Sucking Service Manager) to wrap it as a proper Windows
   service:
   ```cmd
   nssm install sys_monitoring_bot "C:\Python312\python.exe" "C:\path\to\sys_monitoring\tg_bot_loop.py"
   nssm set    sys_monitoring_bot AppDirectory "C:\path\to\sys_monitoring"
   nssm start  sys_monitoring_bot
   ```

`/service <name>` on Windows expects a service name from
`Get-Service` (e.g. `Spooler`, `wuauserv`), not a `.service` unit name.

## Bot commands

Compact-by-default — every screen has inline buttons to drill in.

| Command | What it shows |
|---|---|
| `/start`, `/menu`, `/status` | One-screen snapshot with action buttons |
| `/cpu` `/ram` `/disk` `/disks` `/temp` `/net` `/uptime` | Targeted live readings |
| `/top [cpu\|mem]` | Top processes (toggleable) |
| `/service <unit>` | systemd unit status |
| `/summary [hours]` | Min/avg/max + energy + net (SQL-aggregated, default 24h) |
| `/latest` | Today's metrics exported as CSV |
| `/export YYYY-MM-DD` | Export a specific day as CSV |
| `/getlog` | Browse stored days by year → month → day |
| `/db` | Database stats (rows, size, time range) |
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

## CSV export columns

When the bot exports a day via `/latest`, `/export`, or `/getlog`:

```
Timestamp, CPU Load (%), Temperature (C), RAM Usage (%), Disk Usage (%),
Net Sent Total (MB), Net Recv Total (MB),
Net Sent Delta (MB), Net Recv Delta (MB),
Load Avg 1m, Estimated Power (W), Interval Wh
```

`Delta` columns are per-interval traffic; `Total` columns are cumulative
since boot.

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
    ├── sysmon.db                # SQLite metrics + alert history (WAL)
    ├── sysmon.db-wal            # write-ahead log
    ├── sysmon.db-shm            # shared memory
    ├── monitor.log              # cron logger output
    ├── bot_logs/telegram_bot.log
    ├── state/monitor_state.json # bot offset, alert flags, last-prune date
    └── log_archive/             # legacy CSVs (imported on first run; safe to remove after)
```
