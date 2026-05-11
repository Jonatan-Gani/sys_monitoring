"""Shared utilities for the sys_monitoring project.

This module is the single source of truth for:
  * filesystem paths
  * config loading/saving
  * lightweight system metric collection (non-blocking where possible)
  * CSV tail reads and aggregations
  * formatters for human-friendly output

Designed to be safe to import from short-lived cron scripts and a
long-running Telegram bot.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, Iterable, Iterator

import psutil


__version__ = "1.0.0"

IS_WINDOWS = os.name == "nt"
IS_LINUX = os.name == "posix" and platform.system() == "Linux"


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOG_DIR, "log_archive")           # legacy: kept for import
LOG_FILE_CSV = os.path.join(LOG_DIR, "power_log.csv")        # legacy: kept for import
BOT_LOGS_DIR = os.path.join(LOG_DIR, "bot_logs")
STATE_DIR = os.path.join(LOG_DIR, "state")
STATE_FILE = os.path.join(STATE_DIR, "monitor_state.json")
DB_PATH = os.path.join(LOG_DIR, "sysmon.db")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")

CSV_HEADER = [
    "Timestamp",
    "CPU Load (%)",
    "Temperature (C)",
    "RAM Usage (%)",
    "Disk Usage (%)",
    "Net Sent Total (MB)",
    "Net Recv Total (MB)",
    "Net Sent Delta (MB)",
    "Net Recv Delta (MB)",
    "Load Avg 1m",
    "Estimated Power (W)",
    "Interval Wh",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "debug": False,
    "thresholds": {
        "cpu_load": 90.0,
        "temperature": 70.0,
        "power": 10.0,
        "ram_usage": 85.0,
        "disk_usage": 90.0,
    },
    "alerts": {
        "enabled": True,
        "cooldown_minutes": 30,
        "send_recovery": True,
    },
    "power_model": {
        "idle_watts": 4.5,
        "load_watts": 7.5,
    },
    "storage": {
        "retention_days": 365,        # 0 = keep forever
        "auto_migrate_csv": True,     # one-shot import from legacy CSV on first DB use
    },
    "bot": {
        "poll_timeout": 30,
        "session_timeout_seconds": 120,
        "items_per_page": 6,
        "show_processes": 5,
    },
}

DB_SCHEMA_VERSION = 2
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    ts                INTEGER PRIMARY KEY,
    cpu_load          REAL NOT NULL,
    temperature       REAL,
    ram_usage         REAL NOT NULL,
    disk_usage        REAL NOT NULL,
    net_sent_total_mb REAL,
    net_recv_total_mb REAL,
    net_sent_delta_mb REAL,
    net_recv_delta_mb REAL,
    load_avg_1m       REAL,
    power_w           REAL NOT NULL,
    interval_wh       REAL,
    -- v2 columns (NULL on rows written before the migration)
    cpu_user          REAL,
    cpu_system        REAL,
    cpu_iowait        REAL,
    cpu_steal         REAL,
    load_avg_5m       REAL,
    load_avg_15m      REAL,
    mem_available_mb  REAL,
    mem_cached_mb     REAL,
    mem_buffers_mb    REAL,
    swap_used_mb      REAL,
    disk_read_mb_s    REAL,
    disk_write_mb_s   REAL,
    procs_total       INTEGER,
    procs_running     INTEGER,
    open_fds          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);

CREATE TABLE IF NOT EXISTS alert_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    metric    TEXT NOT NULL,
    event     TEXT NOT NULL,        -- 'breach' | 'recovery' | 'continued'
    value     REAL,
    threshold REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alert_events(ts);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_V2_COLUMNS = [
    ("cpu_user", "REAL"),
    ("cpu_system", "REAL"),
    ("cpu_iowait", "REAL"),
    ("cpu_steal", "REAL"),
    ("load_avg_5m", "REAL"),
    ("load_avg_15m", "REAL"),
    ("mem_available_mb", "REAL"),
    ("mem_cached_mb", "REAL"),
    ("mem_buffers_mb", "REAL"),
    ("swap_used_mb", "REAL"),
    ("disk_read_mb_s", "REAL"),
    ("disk_write_mb_s", "REAL"),
    ("procs_total", "INTEGER"),
    ("procs_running", "INTEGER"),
    ("open_fds", "INTEGER"),
]


def _migrate_v2(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(metrics)")}
    for col, typ in _V2_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")


# ---------------------------------------------------------------------------
# Env + config
# ---------------------------------------------------------------------------

def load_env(env_file: str = ENV_FILE) -> dict[str, str]:
    """Minimal .env loader (no external dependency)."""
    out: dict[str, str] = {}
    if not os.path.exists(env_file):
        return out
    with open(env_file, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            out[key.strip()] = value
    return out


_config_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: str = CONFIG_FILE) -> dict[str, Any]:
    """Load config.json, filling missing keys from DEFAULT_CONFIG."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            _deep_merge(cfg, user_cfg)
        except (json.JSONDecodeError, OSError) as e:
            logging.getLogger("sysmon").error("Failed to load %s: %s", path, e)
    return cfg


def save_config(cfg: dict[str, Any], path: str = CONFIG_FILE) -> None:
    """Atomic config save."""
    with _config_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def get_logger(name: str, log_file: str | None = None, level: int = logging.INFO) -> logging.Logger:
    """Create a logger with rotating file + console handlers exactly once."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=5)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# State (persisted across script runs)
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class Metrics:
    timestamp: dt.datetime
    cpu_load: float
    temperature: float | None
    ram_usage: float
    disk_usage: float
    net_sent_mb: float
    net_recv_mb: float
    net_sent_delta_mb: float
    net_recv_delta_mb: float
    load_avg_1m: float
    power_estimation: float
    uptime_seconds: float
    # v2 fields
    cpu_user: float = 0.0
    cpu_system: float = 0.0
    cpu_iowait: float = 0.0
    cpu_steal: float = 0.0
    load_avg_5m: float = 0.0
    load_avg_15m: float = 0.0
    mem_available_mb: float = 0.0
    mem_cached_mb: float = 0.0
    mem_buffers_mb: float = 0.0
    swap_used_mb: float = 0.0
    disk_read_mb_s: float = 0.0
    disk_write_mb_s: float = 0.0
    procs_total: int = 0
    procs_running: int = 0
    open_fds: int | None = None

    def as_row(self, interval_wh: float) -> list[str]:
        return [
            self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            f"{self.cpu_load:.2f}",
            f"{self.temperature:.2f}" if self.temperature is not None else "N/A",
            f"{self.ram_usage:.2f}",
            f"{self.disk_usage:.2f}",
            f"{self.net_sent_mb:.2f}",
            f"{self.net_recv_mb:.2f}",
            f"{self.net_sent_delta_mb:.2f}",
            f"{self.net_recv_delta_mb:.2f}",
            f"{self.load_avg_1m:.2f}",
            f"{self.power_estimation:.2f}",
            f"{interval_wh:.4f}",
        ]


_THERMAL_CANDIDATES = (
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/thermal/thermal_zone1/temp",
)


def read_cpu_temperature() -> float | None:
    """Best-effort CPU temperature read.

    Tries psutil sensors first (works on most Linux distros), falls back to
    /sys/class/thermal for Raspberry Pi-style systems.
    """
    try:
        sensors = psutil.sensors_temperatures(fahrenheit=False)
    except (AttributeError, OSError):
        sensors = {}

    if sensors:
        # Prefer CPU-ish keys
        for key in ("cpu_thermal", "coretemp", "k10temp", "cpu-thermal", "soc_thermal"):
            if key in sensors and sensors[key]:
                return float(sensors[key][0].current)
        # Otherwise first available
        for entries in sensors.values():
            if entries:
                return float(entries[0].current)

    for path in _THERMAL_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return int(f.read().strip()) / 1000.0
        except (FileNotFoundError, ValueError, OSError):
            continue
    return None


def collect_metrics(power_model: dict[str, float], blocking: bool = False) -> Metrics:
    """Snapshot system metrics.

    blocking=False uses psutil's stateful cpu_percent (interval=None) which
    returns the percent since the last call. The cron path calls it once on
    startup with a short interval to seed the counter.
    """
    interval = 0.5 if blocking else None
    cpu_load = psutil.cpu_percent(interval=interval)
    cpu_times = psutil.cpu_times_percent(interval=None)
    temperature = read_cpu_temperature()

    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    ram_usage = vm.percent
    disk_usage = psutil.disk_usage("/").percent

    net = psutil.net_io_counters()
    net_sent_mb = net.bytes_sent / (1024 * 1024)
    net_recv_mb = net.bytes_recv / (1024 * 1024)

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    idle = power_model.get("idle_watts", 4.5)
    load_w = power_model.get("load_watts", 7.5)
    power_estimation = idle + (load_w * (cpu_load / 100.0))

    try:
        uptime = time.time() - psutil.boot_time()
    except Exception:
        uptime = 0.0

    # Process counts (cheap — single iteration).
    procs_total = 0
    procs_running = 0
    for p in psutil.process_iter(attrs=["status"]):
        procs_total += 1
        if p.info.get("status") == psutil.STATUS_RUNNING:
            procs_running += 1

    # Open FDs only on POSIX.
    open_fds: int | None = None
    if not IS_WINDOWS:
        try:
            with open("/proc/sys/fs/file-nr", "r", encoding="utf-8") as f:
                open_fds = int(f.read().split()[0])
        except (OSError, ValueError):
            open_fds = None

    return Metrics(
        timestamp=dt.datetime.now(),
        cpu_load=cpu_load,
        temperature=temperature,
        ram_usage=ram_usage,
        disk_usage=disk_usage,
        net_sent_mb=net_sent_mb,
        net_recv_mb=net_recv_mb,
        net_sent_delta_mb=0.0,
        net_recv_delta_mb=0.0,
        load_avg_1m=load1,
        power_estimation=power_estimation,
        uptime_seconds=uptime,
        cpu_user=getattr(cpu_times, "user", 0.0),
        cpu_system=getattr(cpu_times, "system", 0.0),
        cpu_iowait=getattr(cpu_times, "iowait", 0.0),
        cpu_steal=getattr(cpu_times, "steal", 0.0),
        load_avg_5m=load5,
        load_avg_15m=load15,
        mem_available_mb=vm.available / (1024 * 1024),
        mem_cached_mb=getattr(vm, "cached", 0) / (1024 * 1024),
        mem_buffers_mb=getattr(vm, "buffers", 0) / (1024 * 1024),
        swap_used_mb=swap.used / (1024 * 1024),
        # Disk IO rates filled in by log_pi_status, which has the previous-snapshot state.
        disk_read_mb_s=0.0,
        disk_write_mb_s=0.0,
        procs_total=procs_total,
        procs_running=procs_running,
        open_fds=open_fds,
    )


def disk_io_totals() -> tuple[float, float]:
    """Return (cumulative read MB, cumulative write MB) across all disks."""
    try:
        io = psutil.disk_io_counters(perdisk=False)
    except Exception:
        return (0.0, 0.0)
    if io is None:
        return (0.0, 0.0)
    return (io.read_bytes / (1024 * 1024), io.write_bytes / (1024 * 1024))


# ---------------------------------------------------------------------------
# Top processes / disks / services
# ---------------------------------------------------------------------------

def top_processes(by: str = "cpu", n: int = 5) -> list[dict[str, Any]]:
    """Return top N processes by 'cpu' or 'memory'.

    Calls cpu_percent twice with a short delay to get meaningful values
    without long blocking.
    """
    procs: list[psutil.Process] = []
    for p in psutil.process_iter(attrs=["pid", "name", "username"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.3)

    results: list[dict[str, Any]] = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
            mem = p.memory_percent()
            name = (p.info.get("name") or "?")[:25]
            results.append({
                "pid": p.info["pid"],
                "name": name,
                "user": p.info.get("username") or "?",
                "cpu": cpu,
                "mem": mem,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = "cpu" if by == "cpu" else "mem"
    results.sort(key=lambda r: r[key], reverse=True)
    return results[:n]


def list_disks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append({
            "mount": part.mountpoint,
            "fstype": part.fstype,
            "total": usage.total,
            "used": usage.used,
            "percent": usage.percent,
        })
    return out


_SAFE_UNIT = re.compile(r"^[A-Za-z0-9@:._\- ]+$")


def service_status(name: str) -> tuple[str, str]:
    """Cross-platform service status. Returns (state, detail).

    States are normalized so the bot can map them to one set of emojis:
        active | inactive | failed | activating | deactivating | not-found | error | unknown
    """
    name = name.strip()
    if not name or not _SAFE_UNIT.match(name):
        return ("error", "invalid service name")

    if IS_WINDOWS:
        # Use psutil's win_service_get when available — no subprocess parsing needed.
        get = getattr(psutil, "win_service_get", None)
        if get is None:
            return ("error", "win_service_get unavailable in this psutil build")
        try:
            svc = get(name)
            info = svc.as_dict()
        except Exception as e:
            # psutil.NoSuchProcess on missing service, else generic
            cls = type(e).__name__.lower()
            if "nosuch" in cls:
                return ("not-found", "")
            return ("error", str(e))
        mapping = {
            "running": "active",
            "stopped": "inactive",
            "stop_pending": "deactivating",
            "start_pending": "activating",
            "pause_pending": "deactivating",
            "paused": "inactive",
            "continue_pending": "activating",
        }
        state = mapping.get(info.get("status", "unknown"), info.get("status", "unknown"))
        detail = info.get("display_name") or info.get("start_type") or ""
        return (state, str(detail))

    # POSIX / Linux: systemd
    if not shutil.which("systemctl"):
        return ("error", "no service manager available")
    try:
        import subprocess
        res = subprocess.run(
            ["systemctl", "show", name, "--property=ActiveState,SubState,LoadState"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode not in (0, 3):
            return ("error", res.stderr.strip() or f"exit {res.returncode}")
        kv: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v
        if kv.get("LoadState") == "not-found":
            return ("not-found", "")
        return (kv.get("ActiveState", "unknown"), kv.get("SubState", ""))
    except Exception as e:
        return ("error", str(e))


# Backwards-compatible alias.
systemd_status = service_status


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "boot_time": psutil.boot_time(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False) or 0,
        "platform": platform.system(),
    }

    # Linux-specific files first; portable fallbacks via `platform` afterwards.
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass

    if "cpu_model" not in info:
        info["cpu_model"] = platform.processor() or platform.machine() or "?"
    if "os" not in info:
        # platform.platform() is verbose; trim common Windows form
        info["os"] = platform.platform(terse=True)
    return info


# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------

_db_init_lock = threading.Lock()
_db_initialized = False


@contextmanager
def db_connect(readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the metrics DB. WAL mode → writers don't block readers.

    Use as `with db_connect() as conn:`. Connections auto-close on exit.
    `readonly=True` uses URI mode for explicit read-only access.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    if readonly:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)
    else:
        # isolation_level=None → autocommit; explicit BEGIN/COMMIT still possible.
        conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _get_db_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


# Schema migrations keyed by target version. Each function receives an open
# connection and is responsible for the upgrade FROM the previous version.
# Add new entries here in future releases; do not edit historical ones.
_MIGRATIONS: dict[int, "callable"] = {
    # 1: initial schema — handled by DB_SCHEMA itself
    2: _migrate_v2,
}


def _run_migrations(conn: sqlite3.Connection) -> int:
    current = _get_db_version(conn)
    target = DB_SCHEMA_VERSION
    if current >= target:
        return 0
    applied = 0
    for v in range(current + 1, target + 1):
        fn = _MIGRATIONS.get(v)
        if fn is not None:
            logging.getLogger("sysmon").info("Applying DB migration -> v%d", v)
            fn(conn)
            applied += 1
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
            (str(v),),
        )
    return applied


def db_init(auto_migrate: bool = True) -> None:
    """Create schema if missing. Idempotent. Called automatically on first use."""
    global _db_initialized
    with _db_init_lock:
        if _db_initialized:
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        with db_connect() as conn:
            conn.executescript(DB_SCHEMA)
            _run_migrations(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('migrated_from_csv', '0')"
            )
            migrated = conn.execute(
                "SELECT value FROM schema_meta WHERE key='migrated_from_csv'"
            ).fetchone()
        _db_initialized = True

        if auto_migrate and migrated and migrated["value"] == "0":
            try:
                imported = migrate_csv_to_db()
                if imported:
                    logging.getLogger("sysmon").info(
                        "Imported %d rows from legacy CSV archives", imported,
                    )
            except Exception:
                logging.getLogger("sysmon").exception("CSV migration failed")
            finally:
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE schema_meta SET value=? WHERE key='migrated_from_csv'",
                        (dt.datetime.now().isoformat(),),
                    )


def db_ensure() -> None:
    """Initialize the DB lazily (called by all read/write helpers)."""
    if not _db_initialized:
        db_init()


def db_insert_metric(m: "Metrics", interval_wh: float) -> None:
    db_ensure()
    ts = int(m.timestamp.timestamp())
    with db_connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO metrics (
                ts, cpu_load, temperature, ram_usage, disk_usage,
                net_sent_total_mb, net_recv_total_mb,
                net_sent_delta_mb, net_recv_delta_mb,
                load_avg_1m, power_w, interval_wh,
                cpu_user, cpu_system, cpu_iowait, cpu_steal,
                load_avg_5m, load_avg_15m,
                mem_available_mb, mem_cached_mb, mem_buffers_mb, swap_used_mb,
                disk_read_mb_s, disk_write_mb_s,
                procs_total, procs_running, open_fds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts, m.cpu_load, m.temperature, m.ram_usage, m.disk_usage,
                m.net_sent_mb, m.net_recv_mb,
                m.net_sent_delta_mb, m.net_recv_delta_mb,
                m.load_avg_1m, m.power_estimation, interval_wh,
                m.cpu_user, m.cpu_system, m.cpu_iowait, m.cpu_steal,
                m.load_avg_5m, m.load_avg_15m,
                m.mem_available_mb, m.mem_cached_mb, m.mem_buffers_mb, m.swap_used_mb,
                m.disk_read_mb_s, m.disk_write_mb_s,
                m.procs_total, m.procs_running, m.open_fds,
            ),
        )


def db_insert_alert(metric: str, event: str, value: float, threshold: float | None) -> None:
    db_ensure()
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO alert_events (ts, metric, event, value, threshold) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), metric, event, value, threshold),
        )


def db_last_metric() -> dict[str, Any] | None:
    db_ensure()
    with db_connect(readonly=True) as conn:
        row = conn.execute("SELECT * FROM metrics ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def db_recent_metrics(n: int = 60) -> list[dict[str, Any]]:
    db_ensure()
    with db_connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM metrics ORDER BY ts DESC LIMIT ?", (int(n),),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def db_summarize_window(since_ts: int, until_ts: int | None = None) -> dict[str, Any]:
    """Min/avg/max aggregation done by SQL — fast even over months of data."""
    db_ensure()
    until_clause = ""
    params: list[Any] = [since_ts]
    if until_ts is not None:
        until_clause = "AND ts < ?"
        params.append(until_ts)
    with db_connect(readonly=True) as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)           AS n,
                MIN(cpu_load)      AS cpu_min, AVG(cpu_load)      AS cpu_avg, MAX(cpu_load)      AS cpu_max,
                MIN(temperature)   AS temp_min, AVG(temperature)  AS temp_avg, MAX(temperature)  AS temp_max,
                MIN(ram_usage)     AS ram_min, AVG(ram_usage)     AS ram_avg, MAX(ram_usage)     AS ram_max,
                MIN(disk_usage)    AS disk_min, AVG(disk_usage)   AS disk_avg, MAX(disk_usage)   AS disk_max,
                MIN(power_w)       AS pow_min, AVG(power_w)       AS pow_avg, MAX(power_w)       AS pow_max,
                COALESCE(SUM(interval_wh), 0) AS energy_wh,
                COALESCE(SUM(net_sent_delta_mb), 0) AS net_sent_mb,
                COALESCE(SUM(net_recv_delta_mb), 0) AS net_recv_mb
            FROM metrics WHERE ts >= ? {until_clause}
            """,
            params,
        ).fetchone()
    return dict(row) if row else {"n": 0}


def db_available_dates() -> list[str]:
    """Distinct local-date strings ('YYYY-MM-DD') that have any data."""
    db_ensure()
    with db_connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') AS d "
            "FROM metrics ORDER BY d DESC"
        ).fetchall()
    return [r["d"] for r in rows]


def db_export_csv_for_date(date_iso: str) -> str | None:
    """Export rows for a local-date as a CSV string. Returns None if empty."""
    db_ensure()
    try:
        day = dt.datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        return None
    start = int(dt.datetime.combine(day, dt.time.min).timestamp())
    end = int(dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).timestamp())
    with db_connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM metrics WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        ).fetchall()
    if not rows:
        return None
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = [
        "Timestamp", "CPU Load (%)", "Temperature (C)", "RAM Usage (%)",
        "Disk Usage (%)", "Net Sent Total (MB)", "Net Recv Total (MB)",
        "Net Sent Delta (MB)", "Net Recv Delta (MB)",
        "Load Avg 1m", "Estimated Power (W)", "Interval Wh",
        "CPU User (%)", "CPU System (%)", "CPU IOWait (%)", "CPU Steal (%)",
        "Load Avg 5m", "Load Avg 15m",
        "Mem Available (MB)", "Mem Cached (MB)", "Mem Buffers (MB)", "Swap Used (MB)",
        "Disk Read (MB/s)", "Disk Write (MB/s)",
        "Processes Total", "Processes Running", "Open FDs",
    ]
    writer.writerow(header)

    def f(v, fmt=".2f", default="N/A"):
        return default if v is None else format(v, fmt)

    for r in rows:
        ts_str = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            ts_str,
            f(r["cpu_load"]),
            f(r["temperature"]),
            f(r["ram_usage"]),
            f(r["disk_usage"]),
            f(r["net_sent_total_mb"]),
            f(r["net_recv_total_mb"]),
            f(r["net_sent_delta_mb"]),
            f(r["net_recv_delta_mb"]),
            f(r["load_avg_1m"]),
            f(r["power_w"]),
            f(r["interval_wh"], ".4f"),
            f(r["cpu_user"]),
            f(r["cpu_system"]),
            f(r["cpu_iowait"]),
            f(r["cpu_steal"]),
            f(r["load_avg_5m"]),
            f(r["load_avg_15m"]),
            f(r["mem_available_mb"]),
            f(r["mem_cached_mb"]),
            f(r["mem_buffers_mb"]),
            f(r["swap_used_mb"]),
            f(r["disk_read_mb_s"]),
            f(r["disk_write_mb_s"]),
            f(r["procs_total"], "d"),
            f(r["procs_running"], "d"),
            f(r["open_fds"], "d"),
        ])
    return buf.getvalue()


def db_purge_older_than(days: int) -> int:
    """Delete rows older than `days`. days <= 0 means keep forever. Returns row count deleted."""
    if days <= 0:
        return 0
    db_ensure()
    cutoff = int(time.time()) - days * 86400
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM alert_events WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


def db_stats() -> dict[str, Any]:
    """Compact DB stats: row count, time range, file size on disk."""
    db_ensure()
    out: dict[str, Any] = {"rows": 0, "first_ts": None, "last_ts": None, "size_bytes": 0}
    try:
        out["size_bytes"] = os.path.getsize(DB_PATH)
    except OSError:
        pass
    with db_connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS mn, MAX(ts) AS mx FROM metrics"
        ).fetchone()
        if row:
            out["rows"] = row["n"]
            out["first_ts"] = row["mn"]
            out["last_ts"] = row["mx"]
    return out


def migrate_csv_to_db() -> int:
    """Import legacy CSVs (current power_log.csv + log_archive/**) into the DB.

    Idempotent thanks to INSERT OR IGNORE on the primary key (ts).
    Returns the count of rows inserted.
    """
    db_ensure()
    candidates: list[str] = []
    if os.path.exists(LOG_FILE_CSV):
        candidates.append(LOG_FILE_CSV)
    if os.path.isdir(ARCHIVE_DIR):
        for root, _dirs, files in os.walk(ARCHIVE_DIR):
            for name in files:
                if name.endswith(".csv"):
                    candidates.append(os.path.join(root, name))

    inserted = 0
    sql = """
        INSERT OR IGNORE INTO metrics (
            ts, cpu_load, temperature, ram_usage, disk_usage,
            net_sent_total_mb, net_recv_total_mb,
            net_sent_delta_mb, net_recv_delta_mb,
            load_avg_1m, power_w, interval_wh
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with db_connect() as conn:
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    batch: list[tuple] = []
                    for row in reader:
                        ts_str = row.get("Timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = int(dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp())
                        except ValueError:
                            continue
                        def num(key: str) -> float | None:
                            v = row.get(key)
                            if v in (None, "", "N/A"):
                                return None
                            try:
                                return float(v)
                            except ValueError:
                                return None
                        batch.append((
                            ts,
                            num("CPU Load (%)") or 0.0,
                            num("Temperature (C)") if "Temperature (C)" in row else num("Temperature (°C)"),
                            num("RAM Usage (%)") or 0.0,
                            num("Disk Usage (%)") or 0.0,
                            num("Net Sent Total (MB)") if "Net Sent Total (MB)" in row else num("Network Sent (MB)"),
                            num("Net Recv Total (MB)") if "Net Recv Total (MB)" in row else num("Network Received (MB)"),
                            num("Net Sent Delta (MB)"),
                            num("Net Recv Delta (MB)"),
                            num("Load Avg 1m"),
                            num("Estimated Power (W)") or 0.0,
                            num("Interval Wh") or 0.0,
                        ))
                    if batch:
                        cur = conn.executemany(sql, batch)
                        inserted += cur.rowcount or 0
            except OSError:
                continue
    return inserted


# ---------------------------------------------------------------------------
# CSV helpers (legacy / migration only)
# ---------------------------------------------------------------------------

def _read_last_lines(path: str, n: int = 1, block_size: int = 4096) -> list[str]:
    """Read last n non-empty lines from a file efficiently (reverse seek)."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        buf = b""
        lines: list[str] = []
        while size > 0 and len(lines) <= n:
            read_size = min(block_size, size)
            size -= read_size
            f.seek(size)
            buf = f.read(read_size) + buf
            lines = buf.splitlines()
        return [ln.decode("utf-8", errors="replace") for ln in lines[-n:] if ln.strip()]


def read_csv_tail(path: str = LOG_FILE_CSV, n: int = 60) -> list[dict[str, str]]:
    """Read last n rows of the metrics CSV without loading the whole file."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        header_line = f.readline().rstrip("\n")
    if not header_line:
        return []
    header = next(csv.reader([header_line]))
    tail = _read_last_lines(path, n + 1)
    # Drop header line if it appears at start
    if tail and tail[0] == header_line:
        tail = tail[1:]
    rows: list[dict[str, str]] = []
    for raw in tail:
        try:
            cells = next(csv.reader([raw]))
            if len(cells) == len(header):
                rows.append(dict(zip(header, cells)))
        except StopIteration:
            continue
    return rows


def get_last_csv_entry(path: str = LOG_FILE_CSV) -> dict[str, str] | None:
    rows = read_csv_tail(path, n=1)
    return rows[-1] if rows else None


def summarize_rows(rows: Iterable[dict[str, str]]) -> dict[str, Any]:
    """Compute min/avg/max for the key numeric columns."""
    fields = {
        "CPU Load (%)": "cpu",
        "Temperature (C)": "temp",
        "RAM Usage (%)": "ram",
        "Disk Usage (%)": "disk",
        "Estimated Power (W)": "power",
    }
    buckets: dict[str, list[float]] = {short: [] for short in fields.values()}
    interval_wh = 0.0
    n = 0
    for row in rows:
        n += 1
        for col, short in fields.items():
            v = row.get(col)
            if v in (None, "", "N/A"):
                continue
            try:
                buckets[short].append(float(v))
            except ValueError:
                pass
        try:
            interval_wh += float(row.get("Interval Wh") or 0)
        except ValueError:
            pass
    out: dict[str, Any] = {"count": n, "interval_wh_total": interval_wh}
    for short, vals in buckets.items():
        if vals:
            out[short] = {
                "min": min(vals),
                "max": max(vals),
                "avg": sum(vals) / len(vals),
            }
    return out


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_bytes(num: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    n = float(num)
    for u in units:
        if abs(n) < 1024.0 or u == units[-1]:
            return f"{n:.1f} {u}"
        n /= 1024.0
    return f"{n:.1f} PB"


def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 30) -> str:
    """Map a sequence of floats to a fixed-width unicode sparkline.

    Resamples to `width` points and scales between observed min and max.
    Returns an empty string when given fewer than 2 points.
    """
    if not values or len(values) < 2:
        return ""
    # Resample to width buckets by averaging
    n = len(values)
    if n == width:
        sampled = list(values)
    elif n > width:
        bucket = n / width
        sampled = []
        for i in range(width):
            lo = int(i * bucket)
            hi = max(lo + 1, int((i + 1) * bucket))
            chunk = values[lo:hi]
            sampled.append(sum(chunk) / len(chunk) if chunk else 0.0)
    else:
        # Stretch: nearest-neighbor sample to width
        sampled = [values[int(i * n / width)] for i in range(width)]
    lo, hi = min(sampled), max(sampled)
    if hi - lo < 1e-9:
        return SPARK_CHARS[0] * width
    return "".join(
        SPARK_CHARS[
            min(len(SPARK_CHARS) - 1, int((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1)))
        ]
        for v in sampled
    )


def db_recent_values(column: str, hours: float = 1.0, limit: int = 240) -> list[float]:
    """Pull a single column's values over the past N hours, oldest first.

    Used by sparklines and /chart. Restricted column name to whitelist to
    keep this safe.
    """
    allowed = {
        "cpu_load", "temperature", "ram_usage", "disk_usage", "power_w",
        "load_avg_1m", "load_avg_5m", "load_avg_15m",
        "cpu_user", "cpu_system", "cpu_iowait",
        "mem_available_mb", "swap_used_mb",
        "disk_read_mb_s", "disk_write_mb_s",
        "net_sent_delta_mb", "net_recv_delta_mb",
        "procs_total", "procs_running", "open_fds",
    }
    if column not in allowed:
        raise ValueError(f"column {column!r} not allowed")
    db_ensure()
    since = int(time.time() - hours * 3600)
    with db_connect(readonly=True) as conn:
        rows = conn.execute(
            f"SELECT {column} FROM metrics WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def status_emoji(value: float, warn: float, danger: float) -> str:
    if value >= danger:
        return "🔴"
    if value >= warn:
        return "🟡"
    return "🟢"
