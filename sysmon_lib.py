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
import re
import shutil
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, Iterable

import psutil


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOG_DIR, "log_archive")
LOG_FILE_CSV = os.path.join(LOG_DIR, "power_log.csv")
BOT_LOGS_DIR = os.path.join(LOG_DIR, "bot_logs")
STATE_DIR = os.path.join(LOG_DIR, "state")
STATE_FILE = os.path.join(STATE_DIR, "monitor_state.json")
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
    "bot": {
        "poll_timeout": 30,
        "session_timeout_seconds": 120,
        "items_per_page": 6,
        "show_processes": 5,
    },
}


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
    temperature = read_cpu_temperature()
    ram_usage = psutil.virtual_memory().percent
    disk_usage = psutil.disk_usage("/").percent
    net = psutil.net_io_counters()
    net_sent_mb = net.bytes_sent / (1024 * 1024)
    net_recv_mb = net.bytes_recv / (1024 * 1024)

    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0

    idle = power_model.get("idle_watts", 4.5)
    load_w = power_model.get("load_watts", 7.5)
    power_estimation = idle + (load_w * (cpu_load / 100.0))

    try:
        uptime = time.time() - psutil.boot_time()
    except Exception:
        uptime = 0.0

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
    )


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


_SAFE_UNIT = re.compile(r"^[A-Za-z0-9@:._\-]+$")


def systemd_status(unit: str) -> tuple[str, str]:
    """Return (active_state, sub_state) for a systemd unit or ('error', msg)."""
    if not _SAFE_UNIT.match(unit):
        return ("error", "invalid unit name")
    if not shutil.which("systemctl"):
        return ("error", "systemctl unavailable")
    try:
        import subprocess
        res = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveState,SubState,LoadState"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode not in (0, 3):  # 3 = unit not active, still valid output
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


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "boot_time": psutil.boot_time(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False) or 0,
    }
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    return info


# ---------------------------------------------------------------------------
# CSV helpers
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


def status_emoji(value: float, warn: float, danger: float) -> str:
    if value >= danger:
        return "🔴"
    if value >= warn:
        return "🟡"
    return "🟢"
