"""Periodic system metrics logger.

Designed to be invoked by cron (e.g. every minute). Writes one CSV row per
invocation, archives the previous day's logs on date rollover, and emits
edge-triggered Telegram alerts (with per-metric cooldown + recovery message).
"""

from __future__ import annotations

import csv
import datetime as dt
import os
from typing import Any

import requests

import sysmon_lib as sm


CONFIG = sm.load_config()
ENV = sm.load_env()
BOT_TOKEN = ENV.get("BOT_TOKEN")
CHAT_ID = ENV.get("CHAT_ID")

logger = sm.get_logger("monitor", os.path.join(sm.LOG_DIR, "monitor.log"))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _telegram_send(text: str) -> None:
    if not (BOT_TOKEN and CHAT_ID):
        logger.warning("BOT_TOKEN/CHAT_ID missing — skipping alert: %s", text)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning("Telegram send failed (%s): %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        logger.warning("Telegram network error: %s", e)


def _check_alerts(metrics: sm.Metrics, state: dict[str, Any]) -> None:
    """Edge-triggered alerts with cooldown & recovery messages.

    state["alerts"] = {
        "<metric>": {"active": bool, "last_sent": iso-timestamp},
    }
    """
    cfg_alerts = CONFIG.get("alerts", {})
    if not cfg_alerts.get("enabled", True):
        return

    thresholds = CONFIG.get("thresholds", {})
    cooldown = dt.timedelta(minutes=int(cfg_alerts.get("cooldown_minutes", 30)))
    send_recovery = bool(cfg_alerts.get("send_recovery", True))
    alert_state = state.setdefault("alerts", {})
    now = dt.datetime.now()

    checks = [
        ("cpu_load", metrics.cpu_load, "⚠️", "CPU load", "%"),
        ("ram_usage", metrics.ram_usage, "⚠️", "RAM usage", "%"),
        ("disk_usage", metrics.disk_usage, "⚠️", "Disk usage", "%"),
        ("power", metrics.power_estimation, "⚡", "Power", "W"),
    ]
    if metrics.temperature is not None:
        checks.append(("temperature", metrics.temperature, "🔥", "Temperature", "°C"))

    for key, value, emoji, label, unit in checks:
        threshold = thresholds.get(key)
        if threshold is None:
            continue
        s = alert_state.setdefault(key, {"active": False, "last_sent": ""})
        breached = value > threshold

        if breached and not s["active"]:
            _telegram_send(f"{emoji} *High {label}*: `{value:.2f}{unit}` (> {threshold}{unit})")
            s["active"] = True
            s["last_sent"] = now.isoformat()
        elif breached and s["active"]:
            last_sent = s.get("last_sent") or ""
            try:
                last = dt.datetime.fromisoformat(last_sent)
            except ValueError:
                last = now - cooldown
            if now - last >= cooldown:
                _telegram_send(f"{emoji} *Still high — {label}*: `{value:.2f}{unit}`")
                s["last_sent"] = now.isoformat()
        elif (not breached) and s["active"]:
            s["active"] = False
            s["last_sent"] = now.isoformat()
            if send_recovery:
                _telegram_send(f"✅ *Recovered — {label}*: `{value:.2f}{unit}` (≤ {threshold}{unit})")


# ---------------------------------------------------------------------------
# Archiving
# ---------------------------------------------------------------------------

def _archive_if_rolled_over() -> None:
    """If the current CSV's last entry is from before today, move it to archive."""
    if not (os.path.exists(sm.LOG_FILE_CSV) and os.path.getsize(sm.LOG_FILE_CSV) > 0):
        return

    last = sm.get_last_csv_entry(sm.LOG_FILE_CSV)
    if not last:
        return
    try:
        last_ts = dt.datetime.strptime(last["Timestamp"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        # Corrupt header/timestamp — fall back to file mtime
        last_ts = dt.datetime.fromtimestamp(os.path.getmtime(sm.LOG_FILE_CSV))

    today = dt.date.today()
    if last_ts.date() >= today:
        return  # still today, no archive

    archive_date = last_ts.date()
    archive_dir = os.path.join(
        sm.ARCHIVE_DIR,
        f"{archive_date.year}",
        f"{archive_date.strftime('%b')}_{archive_date.month:02d}",
    )
    os.makedirs(archive_dir, exist_ok=True)
    archive_name = f"{archive_date.day}_{archive_date.strftime('%A')}.csv"
    archive_path = os.path.join(archive_dir, archive_name)

    if os.path.exists(archive_path):
        i = 1
        base, ext = os.path.splitext(archive_path)
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        archive_path = f"{base}_{i}{ext}"

    os.rename(sm.LOG_FILE_CSV, archive_path)
    logger.info("Archived %s -> %s", sm.LOG_FILE_CSV, archive_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs(sm.LOG_DIR, exist_ok=True)

    _archive_if_rolled_over()

    state = sm.load_state()

    # Net deltas: use stored cumulative bytes (since boot) to compute interval
    metrics = sm.collect_metrics(CONFIG.get("power_model", {}), blocking=True)

    prev = state.get("last", {})
    prev_sent = float(prev.get("net_sent_mb", metrics.net_sent_mb))
    prev_recv = float(prev.get("net_recv_mb", metrics.net_recv_mb))
    # Handle counter reset across reboots
    delta_sent = max(0.0, metrics.net_sent_mb - prev_sent)
    delta_recv = max(0.0, metrics.net_recv_mb - prev_recv)
    metrics.net_sent_delta_mb = delta_sent
    metrics.net_recv_delta_mb = delta_recv

    # Compute interval Wh from elapsed wall-clock since last run
    last_ts_str = prev.get("timestamp")
    if last_ts_str:
        try:
            last_ts = dt.datetime.fromisoformat(last_ts_str)
            elapsed_hours = max(0.0, (metrics.timestamp - last_ts).total_seconds() / 3600)
        except ValueError:
            elapsed_hours = 0.0
    else:
        elapsed_hours = 0.0
    interval_wh = metrics.power_estimation * elapsed_hours if elapsed_hours < 1 else 0.0

    # Write CSV row
    new_file = not (os.path.exists(sm.LOG_FILE_CSV) and os.path.getsize(sm.LOG_FILE_CSV) > 0)
    with open(sm.LOG_FILE_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(sm.CSV_HEADER)
        w.writerow(metrics.as_row(interval_wh))

    # Persist state
    state["last"] = {
        "timestamp": metrics.timestamp.isoformat(),
        "net_sent_mb": metrics.net_sent_mb,
        "net_recv_mb": metrics.net_recv_mb,
    }

    _check_alerts(metrics, state)
    sm.save_state(state)

    logger.debug(
        "logged cpu=%.1f temp=%s ram=%.1f disk=%.1f power=%.2f wh=%.4f",
        metrics.cpu_load,
        f"{metrics.temperature:.1f}" if metrics.temperature is not None else "n/a",
        metrics.ram_usage, metrics.disk_usage, metrics.power_estimation, interval_wh,
    )


if __name__ == "__main__":
    main()
