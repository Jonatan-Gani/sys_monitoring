# sys_monitoring

Lightweight Linux/Windows system monitor with a SQLite-backed time-series
store and an interactive Telegram bot. Managed through a single CLI
(`sysmon`) so setup, updates, and day-to-day maintenance are all one-liners.

- Cron-driven (Linux) or Task Scheduler-driven (Windows) collector writes
  one row per minute into `logs/sysmon.db` (SQLite WAL).
- Edge-triggered Telegram alerts: notify on threshold crossing, again only
  after a cooldown, and once on recovery.
- Bot exposes live system insight via inline keyboards: status, top procs,
  disks, network, summaries, service status, day-by-day log export.
- Runtime tuning over Telegram: `/threshold`, `/alerts on|off`.

## Quick start

### Linux / macOS

```bash
git clone -b claude/improve-telegram-bot-cYtjX \
    https://github.com/Jonatan-Gani/sys_monitoring.git
cd sys_monitoring
./install.sh
```

### Windows (elevated PowerShell)

```powershell
git clone -b claude/improve-telegram-bot-cYtjX `
    https://github.com/Jonatan-Gani/sys_monitoring.git
cd sys_monitoring
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Both scripts:

1. Verify Python ≥ 3.10 and pip,
2. Create a `.venv/` and install the package in editable mode (so the
   `sysmon` command is on PATH),
3. Launch `sysmon init` — an interactive wizard that validates your
   Telegram credentials live, writes `.env`, initializes the database,
   sends a test message, and offers to install the OS service.

After install, every command below works from the project directory (or
from anywhere if the venv is active).

## The CLI

```
sysmon init                          interactive first-time setup
sysmon doctor                        run all health checks
sysmon update                        git pull + reinstall deps + migrate + restart
sysmon version                       print version info

sysmon logger run                    run one logger pass (what cron does)
sysmon bot run                       run the Telegram bot in the foreground

sysmon service install [--system]    install systemd units / Windows tasks
sysmon service uninstall [--system]  remove them
sysmon service start|stop|status     control the service

sysmon config list                   print full config
sysmon config get  <dotted.key>      e.g. thresholds.cpu_load
sysmon config set  <dotted.key> <v>  e.g. alerts.cooldown_minutes 15

sysmon db stats                      row count, size, time range
sysmon db backup                     online backup -> logs/sysmon_backup_*.db
sysmon db prune <days>               delete rows older than N days
sysmon db import-csv                 re-run the one-shot legacy CSV import

sysmon test telegram                 send a test message
```

`sysmon doctor` exits non-zero if any check fails — wire it into your own
monitoring or run it after `sysmon update`. Sample output:

```
✓ Python               Python 3.12.1
✓ Dependencies         deps: psutil 7.0.0, requests 2.32.3
✓ Environment          .env present with BOT_TOKEN and CHAT_ID
✓ Telegram API         bot @MyMonitorBot; chat 12345 reachable
✓ Database             DB 14,873 rows, 1.83 MB
✓ Logger freshness     latest row 22s old
✓ Disk space           30.0 GB free in repo dir
✓ Service              bot:active, logger:waiting
```

## Updating

```
sysmon update
```

`git pull --ff-only` on the current branch, `pip install -r requirements.txt`,
runs DB migrations (versioned via `schema_meta`), then restarts the bot
service if one is installed. No manual steps.

## Telegram bot commands

Compact-by-default — every screen has inline buttons to drill in.

| Command | What it shows |
|---|---|
| `/start`, `/menu`, `/status` | One-screen snapshot with action buttons |
| `/cpu` `/ram` `/disk` `/disks` `/temp` `/net` `/uptime` | Targeted live readings |
| `/top [cpu\|mem]` | Top processes (toggleable) |
| `/service <unit>` | systemd/Windows-service status |
| `/summary [hours]` | Min/avg/max + energy + net (SQL-aggregated, default 24h) |
| `/latest` | Today's metrics as CSV |
| `/export YYYY-MM-DD` | Export a specific day as CSV |
| `/getlog` | Browse stored days by year → month → day |
| `/db` | Database stats |
| `/alerts on\|off\|status` | Runtime alert toggle |
| `/threshold <name> <value>` | Tune a threshold without editing config |
| `/help` | Command reference |

Status icons: 🟢 below 85% of threshold, 🟡 85–100%, 🔴 over threshold.

## Files

| File | Role |
|------|------|
| `sysmon.py` | The management CLI. |
| `sysmon_lib.py` | Core library: SQLite layer, config, metrics, formatters. |
| `log_pi_status.py` | Cron / scheduled-task entry point. Inserts one DB row. |
| `tg_bot_loop.py` | Telegram bot main loop. |
| `install.sh` / `install.ps1` | Bootstrap (venv + deps + `sysmon init`). |
| `pyproject.toml` | Makes `sysmon` an installed CLI via `pip install -e .`. |
| `config.json` | Thresholds, alert policy, power model, retention, bot tuning. |
| `.env` | `BOT_TOKEN`, `CHAT_ID`, `AUTHORIZED_USERS`. Auto-chmod 600 on POSIX. |

## Storage

`logs/sysmon.db` (SQLite, WAL mode). Schema:

```sql
metrics       (ts PK, cpu_load, temperature, ram_usage, disk_usage,
               net_*_total_mb, net_*_delta_mb, load_avg_1m, power_w, interval_wh)
alert_events  (id, ts, metric, event['breach'|'recovery'|'continued'], value, threshold)
schema_meta   (key, value)              -- includes the current schema version
```

WAL mode + autocommit + `busy_timeout=5000` mean the bot reads freely while
the cron job writes. ~50 MB/year at one row/minute. Retention enforced by
`storage.retention_days` (default 365; `0` = forever) once per local day.

## Platform notes

| Concern | Linux | Windows |
|---|---|---|
| Scheduling | systemd timer (preferred) or cron | Task Scheduler |
| Bot persistence | systemd service | Task Scheduler (ONLOGON) or NSSM |
| Temperature | `/sys/class/thermal` or psutil sensors | usually `n/a` (no standard API) |
| `/service` | `systemctl show ...` | `psutil.win_service_get(...)` |

Both paths are handled inside `sysmon_lib`; the bot itself has no
platform-specific code.
