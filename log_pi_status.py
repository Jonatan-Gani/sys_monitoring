"""Periodic system metrics logger (SQLite-backed).

Designed to be invoked by cron (e.g. every minute). Writes one row to the
metrics table per invocation, fires edge-triggered Telegram alerts (with
per-metric cooldown + recovery message), and applies retention.
"""

from __future__ import annotations

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
    """Edge-triggered alerts with cooldown + recovery messages.

    Each fired alert is also recorded in the alert_events table for history.
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
            sm.db_insert_alert(key, "breach", value, threshold)
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
                sm.db_insert_alert(key, "continued", value, threshold)
                s["last_sent"] = now.isoformat()
        elif (not breached) and s["active"]:
            s["active"] = False
            s["last_sent"] = now.isoformat()
            sm.db_insert_alert(key, "recovery", value, threshold)
            if send_recovery:
                _telegram_send(f"✅ *Recovered — {label}*: `{value:.2f}{unit}` (≤ {threshold}{unit})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    auto_migrate = bool(CONFIG.get("storage", {}).get("auto_migrate_csv", True))
    sm.db_init(auto_migrate=auto_migrate)

    state = sm.load_state()
    metrics = sm.collect_metrics(CONFIG.get("power_model", {}), blocking=True)

    # Net delta vs. previous run, with reboot-counter-reset handling
    prev = state.get("last", {})
    prev_sent = float(prev.get("net_sent_mb", metrics.net_sent_mb))
    prev_recv = float(prev.get("net_recv_mb", metrics.net_recv_mb))
    metrics.net_sent_delta_mb = max(0.0, metrics.net_sent_mb - prev_sent)
    metrics.net_recv_delta_mb = max(0.0, metrics.net_recv_mb - prev_recv)

    # Interval Wh from elapsed wall-clock since previous run
    last_ts_str = prev.get("timestamp")
    elapsed_hours = 0.0
    if last_ts_str:
        try:
            last_ts = dt.datetime.fromisoformat(last_ts_str)
            elapsed_hours = max(0.0, (metrics.timestamp - last_ts).total_seconds() / 3600)
        except ValueError:
            pass
    interval_wh = metrics.power_estimation * elapsed_hours if 0 < elapsed_hours < 1 else 0.0

    sm.db_insert_metric(metrics, interval_wh)

    state["last"] = {
        "timestamp": metrics.timestamp.isoformat(),
        "net_sent_mb": metrics.net_sent_mb,
        "net_recv_mb": metrics.net_recv_mb,
    }

    _check_alerts(metrics, state)
    sm.save_state(state)

    # Retention
    retention = int(CONFIG.get("storage", {}).get("retention_days", 365))
    if retention > 0:
        # Run prune at most once a day to avoid pointless writes
        last_prune = state.get("last_prune_date")
        today = dt.date.today().isoformat()
        if last_prune != today:
            removed = sm.db_purge_older_than(retention)
            state["last_prune_date"] = today
            sm.save_state(state)
            if removed:
                logger.info("Pruned %d rows older than %d days", removed, retention)

    logger.debug(
        "logged cpu=%.1f temp=%s ram=%.1f disk=%.1f power=%.2f wh=%.4f",
        metrics.cpu_load,
        f"{metrics.temperature:.1f}" if metrics.temperature is not None else "n/a",
        metrics.ram_usage, metrics.disk_usage, metrics.power_estimation, interval_wh,
    )


if __name__ == "__main__":
    main()
