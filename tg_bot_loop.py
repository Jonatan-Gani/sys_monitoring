"""Interactive Telegram bot for the sys_monitoring project.

Goals:
  - Compact-by-default views so the user is not overwhelmed
  - On-demand drill-downs via inline keyboards
  - Live system insight (CPU, RAM, disk, temp, network, top procs, services)
  - Historical browsing of archived CSV logs
  - Runtime control: alerts on/off, threshold tuning
  - Light footprint: long polling, connection reuse, no extra threads
"""

from __future__ import annotations

import datetime as dt
import io
import math
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import psutil
import requests

import sysmon_lib as sm


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

ENV = sm.load_env()
CONFIG = sm.load_config()

BOT_TOKEN = ENV.get("BOT_TOKEN") or ""
CHAT_ID_DEFAULT = ENV.get("CHAT_ID") or ""
AUTHORIZED_USERS = {u.strip() for u in (ENV.get("AUTHORIZED_USERS") or "").split(",") if u.strip()}
# Allow CHAT_ID to receive messages too if AUTHORIZED_USERS is empty
if not AUTHORIZED_USERS and CHAT_ID_DEFAULT:
    AUTHORIZED_USERS.add(CHAT_ID_DEFAULT)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing from .env")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

LOG_FILE = os.path.join(sm.BOT_LOGS_DIR, "telegram_bot.log")
logger = sm.get_logger("bot", LOG_FILE)

SESSION = requests.Session()
SESSION.headers.update({"Connection": "keep-alive"})

BOT_CFG = CONFIG.get("bot", {})
POLL_TIMEOUT = int(BOT_CFG.get("poll_timeout", 30))
SESSION_TIMEOUT = int(BOT_CFG.get("session_timeout_seconds", 120))
ITEMS_PER_PAGE = int(BOT_CFG.get("items_per_page", 6))
SHOW_PROCS = int(BOT_CFG.get("show_processes", 5))


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _post(method: str, payload: dict, files: dict | None = None) -> dict | None:
    try:
        if files:
            r = SESSION.post(f"{API_URL}/{method}", data=payload, files=files, timeout=30)
        else:
            r = SESSION.post(f"{API_URL}/{method}", json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning("API %s -> %s %s", method, r.status_code, r.text[:200])
            return None
        return r.json()
    except requests.RequestException as e:
        logger.warning("API %s network error: %s", method, e)
        return None


def send_message(chat_id: int | str, text: str, reply_markup: dict | None = None,
                 parse_mode: str | None = "Markdown") -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    _post("sendMessage", payload)


def edit_message(chat_id: int | str, message_id: int, text: str,
                 reply_markup: dict | None = None, parse_mode: str | None = "Markdown") -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    _post("editMessageText", payload)


def answer_callback(callback_id: str, text: str | None = None, alert: bool = False) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    _post("answerCallbackQuery", payload)


def send_document(chat_id: int | str, file_path: str, caption: str | None = None) -> None:
    if not os.path.exists(file_path):
        send_message(chat_id, f"_File not found:_ `{os.path.basename(file_path)}`")
        return
    try:
        with open(file_path, "rb") as f:
            payload = {"chat_id": chat_id}
            if caption:
                payload["caption"] = caption
            _post("sendDocument", payload, files={"document": f})
    except OSError as e:
        logger.error("send_document failed: %s", e)
        send_message(chat_id, f"_Failed to send file: {e}_")


def is_authorized(user_id: int | str) -> bool:
    ok = str(user_id) in AUTHORIZED_USERS
    if not ok:
        logger.warning("Unauthorized access by user %s", user_id)
    return ok


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def md_escape(text: str) -> str:
    """Escape characters that are special in Telegram's legacy Markdown."""
    return text.replace("_", r"\_").replace("*", r"\*").replace("`", r"\`").replace("[", r"\[")


def code_block(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Session:
    def __init__(self) -> None:
        self.stage: str | None = None
        self.year: str | None = None
        self.month: str | None = None
        self.page: int = 0
        self.proc_sort: str = "cpu"
        self.last_active: datetime = datetime.now()


SESSIONS: dict[int, Session] = {}


def get_session(chat_id: int) -> Session:
    sess = SESSIONS.get(chat_id)
    now = datetime.now()
    if sess is None or (now - sess.last_active) > timedelta(seconds=SESSION_TIMEOUT):
        sess = Session()
        SESSIONS[chat_id] = sess
    sess.last_active = now
    return sess


# ---------------------------------------------------------------------------
# Status views
# ---------------------------------------------------------------------------

def _thresholds() -> dict[str, float]:
    return CONFIG.get("thresholds", {})


def _emoji_for(metric: str, value: float) -> str:
    t = _thresholds()
    danger = float(t.get(metric, 100))
    warn = danger * 0.85
    return sm.status_emoji(value, warn, danger)


def render_status_compact() -> tuple[str, dict]:
    """One-screen summary of current system state."""
    m = sm.collect_metrics(CONFIG.get("power_model", {}), blocking=False)
    info = sm.system_info()

    temp_str = f"{m.temperature:.1f}°C" if m.temperature is not None else "n/a"
    temp_emoji = "🟢"
    if m.temperature is not None:
        temp_emoji = _emoji_for("temperature", m.temperature)

    lines = [
        f"*{info.get('hostname','host')}* · up {sm.format_uptime(m.uptime_seconds)}",
        "",
        f"{_emoji_for('cpu_load', m.cpu_load)} CPU `{m.cpu_load:5.1f}%`  load `{m.load_avg_1m:.2f}`",
        f"{temp_emoji} Temp `{temp_str}`",
        f"{_emoji_for('ram_usage', m.ram_usage)} RAM `{m.ram_usage:5.1f}%`",
        f"{_emoji_for('disk_usage', m.disk_usage)} Disk `{m.disk_usage:5.1f}%` (/)",
        f"⚡ Power `{m.power_estimation:.2f} W`",
    ]
    text = "\n".join(lines)

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔄 Refresh", "callback_data": "view:status"},
                {"text": "📊 Detail", "callback_data": "view:detail"},
            ],
            [
                {"text": "🧠 Top Procs", "callback_data": "view:top"},
                {"text": "💾 Disks", "callback_data": "view:disks"},
            ],
            [
                {"text": "🌐 Network", "callback_data": "view:net"},
                {"text": "📈 24h Summary", "callback_data": "view:summary24"},
            ],
            [
                {"text": "📁 Logs", "callback_data": "view:logs"},
                {"text": "⚙️ Settings", "callback_data": "view:settings"},
            ],
        ]
    }
    return text, keyboard


def render_detail() -> str:
    m = sm.collect_metrics(CONFIG.get("power_model", {}))
    info = sm.system_info()
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    boot = datetime.fromtimestamp(info["boot_time"]).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"*System detail*",
        f"`Host    `: {info.get('hostname','?')}",
        f"`OS      `: {info.get('os','?')}",
        f"`CPU     `: {info.get('cpu_model','?')} ({info.get('cpu_count_logical','?')} cores)",
        f"`Booted  `: {boot}",
        f"`Uptime  `: {sm.format_uptime(m.uptime_seconds)}",
        "",
        f"*CPU*   {m.cpu_load:.1f}%   load1 {m.load_avg_1m:.2f}",
        f"*Temp*  " + (f"{m.temperature:.1f}°C" if m.temperature is not None else "n/a"),
        f"*RAM*   {m.ram_usage:.1f}%   {sm.format_bytes(vm.used)}/{sm.format_bytes(vm.total)}",
        f"*Swap*  {swap.percent:.1f}%   {sm.format_bytes(swap.used)}/{sm.format_bytes(swap.total)}",
        f"*Power* {m.power_estimation:.2f} W (est.)",
    ]
    return "\n".join(lines)


def render_top(sort_by: str = "cpu") -> tuple[str, dict]:
    procs = sm.top_processes(by=sort_by, n=SHOW_PROCS)
    header = f"{'PID':>6} {'CPU%':>6} {'MEM%':>6}  NAME"
    rows = [header]
    for p in procs:
        rows.append(f"{p['pid']:>6} {p['cpu']:>6.1f} {p['mem']:>6.1f}  {p['name']}")
    text = f"*Top processes by {sort_by.upper()}*\n" + code_block("\n".join(rows))
    other = "mem" if sort_by == "cpu" else "cpu"
    keyboard = {
        "inline_keyboard": [[
            {"text": f"Sort by {other.upper()}", "callback_data": f"top:{other}"},
            {"text": "🔄 Refresh", "callback_data": f"top:{sort_by}"},
        ], [
            {"text": "⬅️ Back", "callback_data": "view:status"},
        ]]
    }
    return text, keyboard


def render_disks() -> str:
    disks = sm.list_disks()
    if not disks:
        return "_No mounted disks reported._"
    rows = [f"{'MOUNT':<20} {'USE%':>6}  USED/TOTAL"]
    for d in disks:
        used = sm.format_bytes(d["used"])
        total = sm.format_bytes(d["total"])
        rows.append(f"{d['mount'][:20]:<20} {d['percent']:>6.1f}  {used}/{total}")
    return "*Disks*\n" + code_block("\n".join(rows))


def render_net() -> str:
    n = psutil.net_io_counters()
    per_nic = psutil.net_io_counters(pernic=True)
    addrs = psutil.net_if_addrs()
    lines = [
        "*Network (since boot)*",
        f"Sent: `{sm.format_bytes(n.bytes_sent)}`   Recv: `{sm.format_bytes(n.bytes_recv)}`",
        f"Pkts: `{n.packets_sent}` / `{n.packets_recv}`   Err: `{n.errin + n.errout}`",
        "",
        "*Interfaces*",
    ]
    for nic, counts in per_nic.items():
        # Skip loopback on both Linux ("lo") and Windows
        # ("Loopback Pseudo-Interface 1", etc.)
        nic_lower = nic.lower()
        if nic == "lo" or "loopback" in nic_lower:
            continue
        ip = ""
        for addr in addrs.get(nic, []):
            if addr.family.name in ("AF_INET", "AddressFamily.AF_INET"):
                ip = addr.address
                break
        # Truncate long Windows adapter names for the fixed-width column
        short = nic if len(nic) <= 14 else nic[:13] + "…"
        lines.append(
            f"`{short:<14}` {ip:<15} ↑ {sm.format_bytes(counts.bytes_sent)} "
            f"↓ {sm.format_bytes(counts.bytes_recv)}"
        )
    return "\n".join(lines)


def render_summary(window_hours: int) -> str:
    """SQL-aggregated summary — fast regardless of window length."""
    since = int(time.time()) - window_hours * 3600
    s = sm.db_summarize_window(since)
    if not s.get("n"):
        return f"_No data points in the last {window_hours}h._"

    lines = [f"*Summary · last {window_hours}h* ({s['n']} samples)"]
    rows_out = [f"{'METRIC':<8} {'MIN':>7} {'AVG':>7} {'MAX':>7}"]
    metric_map = [
        ("CPU%",   "cpu_min", "cpu_avg", "cpu_max"),
        ("Temp°C", "temp_min", "temp_avg", "temp_max"),
        ("RAM%",   "ram_min", "ram_avg", "ram_max"),
        ("Disk%",  "disk_min", "disk_avg", "disk_max"),
        ("Power W","pow_min", "pow_avg", "pow_max"),
    ]
    for label, kmin, kavg, kmax in metric_map:
        if s.get(kmin) is None:
            continue
        rows_out.append(f"{label:<8} {s[kmin]:>7.2f} {s[kavg]:>7.2f} {s[kmax]:>7.2f}")
    lines.append(code_block("\n".join(rows_out)))
    lines.append(
        f"⚡ Energy: `{s['energy_wh']:.2f} Wh`   "
        f"🌐 Net: ↑`{s['net_sent_mb']:.1f} MB`  ↓`{s['net_recv_mb']:.1f} MB`"
    )
    return "\n".join(lines)


def render_settings() -> tuple[str, dict]:
    t = _thresholds()
    a = CONFIG.get("alerts", {})
    text = (
        "*Settings*\n"
        f"Alerts: *{'ON' if a.get('enabled') else 'OFF'}*  "
        f"cooldown `{a.get('cooldown_minutes',30)}m`\n\n"
        "*Thresholds*\n"
        f"`cpu_load    {t.get('cpu_load',0):>6.1f}`\n"
        f"`temperature {t.get('temperature',0):>6.1f}`\n"
        f"`ram_usage   {t.get('ram_usage',0):>6.1f}`\n"
        f"`disk_usage  {t.get('disk_usage',0):>6.1f}`\n"
        f"`power       {t.get('power',0):>6.1f}`\n\n"
        "Use `/threshold <name> <value>` to change.\n"
        "Use `/alerts on|off` to toggle.\n"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "Alerts ON" if not a.get("enabled") else "Alerts OFF",
         "callback_data": "alerts:toggle"},
        {"text": "⬅️ Back", "callback_data": "view:status"},
    ]]}
    return text, keyboard


# ---------------------------------------------------------------------------
# Archive browser (DB-backed)
# ---------------------------------------------------------------------------

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _dates_grouped() -> dict[str, dict[str, list[str]]]:
    """{year: {month_num: [day, ...]}}, sorted descending by year/month, ascending by day."""
    grouped: dict[str, dict[str, list[str]]] = {}
    for date_iso in sm.db_available_dates():
        try:
            y, m, d = date_iso.split("-")
        except ValueError:
            continue
        grouped.setdefault(y, {}).setdefault(m, []).append(d)
    return grouped


def get_available_years() -> list[str]:
    return sorted(_dates_grouped().keys(), reverse=True)


def get_available_months(year: str) -> list[str]:
    g = _dates_grouped().get(year, {})
    return sorted(g.keys())


def get_available_days(year: str, month: str) -> list[str]:
    return sorted(_dates_grouped().get(year, {}).get(month, []))


def pagination_keyboard(items: list[dict], page: int, prefix: str,
                        extra_rows: list[list[dict]] | None = None) -> dict:
    total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * ITEMS_PER_PAGE
    keyboard = [[item] for item in items[start:start + ITEMS_PER_PAGE]]
    nav: list[dict] = []
    if total_pages > 1:
        if page > 0:
            nav.append({"text": "◀️", "callback_data": f"page:{prefix}:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
        if page < total_pages - 1:
            nav.append({"text": "▶️", "callback_data": f"page:{prefix}:{page + 1}"})
    if nav:
        keyboard.append(nav)
    if extra_rows:
        keyboard.extend(extra_rows)
    return {"inline_keyboard": keyboard}


def show_years(chat_id: int, message_id: int | None = None) -> None:
    years = get_available_years()
    if not years:
        msg = "_No archived logs available yet._"
        keyboard = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "view:status"}]]}
    else:
        sess = get_session(chat_id)
        sess.stage = "year"
        items = [{"text": y, "callback_data": f"year:{y}"} for y in years]
        keyboard = pagination_keyboard(items, sess.page, "year",
                                       extra_rows=[[{"text": "⬅️ Back", "callback_data": "view:status"}]])
        msg = "*Logs · select a year*"
    if message_id:
        edit_message(chat_id, message_id, msg, keyboard)
    else:
        send_message(chat_id, msg, keyboard)


def show_months(chat_id: int, message_id: int | None = None) -> None:
    sess = get_session(chat_id)
    if not sess.year:
        show_years(chat_id, message_id)
        return
    months = get_available_months(sess.year)
    if not months:
        edit_message(chat_id, message_id or 0, f"_No data for {sess.year}._")
        return
    sess.stage = "month"
    items = []
    for num in months:
        try:
            name = _MONTH_ABBR[int(num) - 1]
        except (ValueError, IndexError):
            name = num
        items.append({"text": f"{name} ({num})", "callback_data": f"month:{num}"})
    keyboard = pagination_keyboard(items, sess.page, "month",
                                   extra_rows=[[{"text": "⬅️ Back", "callback_data": "view:logs"}]])
    msg = f"*Logs · {sess.year} · select month*"
    if message_id:
        edit_message(chat_id, message_id, msg, keyboard)
    else:
        send_message(chat_id, msg, keyboard)


def show_days(chat_id: int, message_id: int | None = None) -> None:
    sess = get_session(chat_id)
    if not (sess.year and sess.month):
        show_months(chat_id, message_id)
        return
    days = get_available_days(sess.year, sess.month)
    if not days:
        edit_message(chat_id, message_id or 0, "_No days available._")
        return
    sess.stage = "day"
    items = []
    for day in days:
        try:
            d = dt.date(int(sess.year), int(sess.month), int(day))
            label = f"{d.strftime('%a')} {day}"
        except ValueError:
            label = day
        items.append({"text": label, "callback_data": f"day:{day}"})
    keyboard = pagination_keyboard(
        items, sess.page, "day",
        extra_rows=[[{"text": "⬅️ Back", "callback_data": "back:month"}]],
    )
    msg = f"*Logs · {sess.year}-{sess.month} · select day*"
    if message_id:
        edit_message(chat_id, message_id, msg, keyboard)
    else:
        send_message(chat_id, msg, keyboard)


def send_csv_for_date(chat_id: int, date_iso: str, caption: str | None = None) -> bool:
    """Export DB rows for the given local date as CSV and send as a document."""
    data = sm.db_export_csv_for_date(date_iso)
    if not data:
        send_message(chat_id, f"_No data for {date_iso}._")
        return False
    payload: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    files = {"document": (f"sysmon_{date_iso}.csv", io.BytesIO(data.encode("utf-8")), "text/csv")}
    _post("sendDocument", payload, files=files)
    return True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_start(chat_id: int, _args: str) -> None:
    text, kb = render_status_compact()
    send_message(chat_id, text, kb)


def cmd_status(chat_id: int, _args: str) -> None:
    text, kb = render_status_compact()
    send_message(chat_id, text, kb)


def cmd_help(chat_id: int, _args: str) -> None:
    text = (
        "*sys_monitoring bot*\n\n"
        "Quick view:\n"
        "`/status`  one-screen system snapshot\n"
        "`/menu`    same as /status\n\n"
        "Live metrics:\n"
        "`/cpu` `/ram` `/disk` `/temp` `/net` `/uptime`\n"
        "`/top [cpu|mem]`  top processes\n"
        "`/disks`  per-partition usage\n"
        "`/service <unit>`  systemd status\n\n"
        "History:\n"
        "`/summary [24|72|168]`  last N hours stats (SQL-aggregated)\n"
        "`/latest`  today's metrics as CSV\n"
        "`/export YYYY-MM-DD`  export a specific day\n"
        "`/getlog`  browse stored days\n"
        "`/db`  database stats\n\n"
        "Control:\n"
        "`/alerts on|off|status`\n"
        "`/threshold <name> <value>`\n"
        "`/help`  this message"
    )
    send_message(chat_id, text)


def cmd_cpu(chat_id: int, _args: str) -> None:
    m = sm.collect_metrics(CONFIG.get("power_model", {}), blocking=True)
    per_cpu = psutil.cpu_percent(percpu=True)
    per = " ".join(f"{v:.0f}" for v in per_cpu)
    text = (
        f"*CPU* {_emoji_for('cpu_load', m.cpu_load)}\n"
        f"Total: `{m.cpu_load:.1f}%`\n"
        f"Load avg: `{m.load_avg_1m:.2f}`\n"
        f"Per-core: `{per}`"
    )
    send_message(chat_id, text)


def cmd_ram(chat_id: int, _args: str) -> None:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    text = (
        f"*RAM* {_emoji_for('ram_usage', vm.percent)}\n"
        f"`{vm.percent:.1f}%`  {sm.format_bytes(vm.used)} / {sm.format_bytes(vm.total)}\n"
        f"Avail: `{sm.format_bytes(vm.available)}`\n"
        f"*Swap*: `{swap.percent:.1f}%`  {sm.format_bytes(swap.used)} / {sm.format_bytes(swap.total)}"
    )
    send_message(chat_id, text)


def cmd_disk(chat_id: int, _args: str) -> None:
    send_message(chat_id, render_disks())


def cmd_disks(chat_id: int, _args: str) -> None:
    send_message(chat_id, render_disks())


def cmd_temp(chat_id: int, _args: str) -> None:
    t = sm.read_cpu_temperature()
    if t is None:
        send_message(chat_id, "_Temperature sensor not available._")
    else:
        send_message(chat_id, f"*Temperature* {_emoji_for('temperature', t)} `{t:.1f}°C`")


def cmd_net(chat_id: int, _args: str) -> None:
    send_message(chat_id, render_net())


def cmd_uptime(chat_id: int, _args: str) -> None:
    secs = time.time() - psutil.boot_time()
    boot = datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M")
    send_message(chat_id, f"*Uptime* `{sm.format_uptime(secs)}`\nBooted: `{boot}`")


def cmd_top(chat_id: int, args: str) -> None:
    by = "mem" if args.strip().lower() in ("mem", "ram", "memory") else "cpu"
    text, kb = render_top(by)
    send_message(chat_id, text, kb)


def cmd_service(chat_id: int, args: str) -> None:
    unit = args.strip()
    if not unit:
        send_message(chat_id, "_Usage: `/service <unit-or-service-name>`_")
        return
    state, sub = sm.service_status(unit)
    emoji = {"active": "🟢", "inactive": "⚫", "failed": "🔴",
             "activating": "🟡", "deactivating": "🟡",
             "not-found": "❔", "error": "⚠️"}.get(state, "❔")
    sub_part = f" ({sub})" if sub else ""
    send_message(chat_id, f"{emoji} `{unit}` → *{state}*{sub_part}")


def cmd_latest(chat_id: int, _args: str) -> None:
    """Send today's data as a CSV exported from the DB."""
    today_iso = dt.date.today().isoformat()
    sent = send_csv_for_date(chat_id, today_iso, caption=f"Today's metrics · {today_iso}")
    if not sent:
        # Fallback: send yesterday if today is empty (e.g. early-morning request)
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        send_csv_for_date(chat_id, yesterday, caption=f"Yesterday's metrics · {yesterday}")


def cmd_export(chat_id: int, args: str) -> None:
    """`/export YYYY-MM-DD` — export a specific day as CSV."""
    date_iso = args.strip()
    if not date_iso:
        send_message(chat_id, "_Usage: `/export YYYY-MM-DD`_")
        return
    try:
        dt.datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        send_message(chat_id, "_Date must be YYYY-MM-DD._")
        return
    send_csv_for_date(chat_id, date_iso, caption=f"Metrics · {date_iso}")


def cmd_summary(chat_id: int, args: str) -> None:
    try:
        hours = int(args.strip()) if args.strip() else 24
    except ValueError:
        hours = 24
    hours = max(1, min(hours, 24 * 30))
    send_message(chat_id, render_summary(hours))


def cmd_alerts(chat_id: int, args: str) -> None:
    arg = args.strip().lower()
    a = CONFIG.setdefault("alerts", {})
    if arg in ("on", "enable", "true"):
        a["enabled"] = True
        sm.save_config(CONFIG)
        send_message(chat_id, "✅ Alerts *enabled*.")
    elif arg in ("off", "disable", "false"):
        a["enabled"] = False
        sm.save_config(CONFIG)
        send_message(chat_id, "🛑 Alerts *disabled*.")
    else:
        state = "ON" if a.get("enabled", True) else "OFF"
        cd = a.get("cooldown_minutes", 30)
        send_message(chat_id, f"Alerts: *{state}*  ·  cooldown `{cd}m`")


def cmd_threshold(chat_id: int, args: str) -> None:
    parts = args.strip().split()
    if len(parts) != 2:
        send_message(chat_id, "_Usage: `/threshold <name> <value>`_\n"
                              "Names: cpu_load, temperature, ram_usage, disk_usage, power")
        return
    name, raw = parts
    t = CONFIG.setdefault("thresholds", {})
    if name not in t:
        send_message(chat_id, f"_Unknown threshold `{md_escape(name)}`._")
        return
    try:
        value = float(raw)
    except ValueError:
        send_message(chat_id, "_Value must be numeric._")
        return
    old = t[name]
    t[name] = value
    sm.save_config(CONFIG)
    send_message(chat_id, f"✅ `{name}` updated  `{old}` → `{value}`")


def cmd_getlog(chat_id: int, _args: str) -> None:
    sess = get_session(chat_id)
    sess.page = 0
    sess.year = None
    sess.month = None
    show_years(chat_id)


def cmd_db(chat_id: int, _args: str) -> None:
    s = sm.db_stats()
    size_mb = s["size_bytes"] / (1024 * 1024)
    first = dt.datetime.fromtimestamp(s["first_ts"]).strftime("%Y-%m-%d %H:%M") if s["first_ts"] else "—"
    last = dt.datetime.fromtimestamp(s["last_ts"]).strftime("%Y-%m-%d %H:%M") if s["last_ts"] else "—"
    text = (
        "*Database*\n"
        f"Rows: `{s['rows']:,}`\n"
        f"Size: `{size_mb:.2f} MB`\n"
        f"From: `{first}`\n"
        f"To:   `{last}`\n"
        f"Path: `{os.path.relpath(sm.DB_PATH)}`"
    )
    send_message(chat_id, text)


COMMANDS: dict[str, Callable[[int, str], None]] = {
    "/start": cmd_start,
    "/menu": cmd_start,
    "/status": cmd_status,
    "/help": cmd_help,
    "/cpu": cmd_cpu,
    "/ram": cmd_ram,
    "/mem": cmd_ram,
    "/memory": cmd_ram,
    "/disk": cmd_disk,
    "/disks": cmd_disks,
    "/temp": cmd_temp,
    "/temperature": cmd_temp,
    "/net": cmd_net,
    "/network": cmd_net,
    "/uptime": cmd_uptime,
    "/top": cmd_top,
    "/service": cmd_service,
    "/services": cmd_service,
    "/latest": cmd_latest,
    "/export": cmd_export,
    "/summary": cmd_summary,
    "/alerts": cmd_alerts,
    "/threshold": cmd_threshold,
    "/getlog": cmd_getlog,
    "/db": cmd_db,
}


# ---------------------------------------------------------------------------
# Callback (inline button) routing
# ---------------------------------------------------------------------------

def handle_callback(cb: dict) -> None:
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    user_id = cb["from"]["id"]
    data = cb.get("data", "")
    cb_id = cb["id"]

    if not is_authorized(user_id):
        answer_callback(cb_id, "Unauthorized.", alert=True)
        return

    sess = get_session(chat_id)

    if data == "noop":
        answer_callback(cb_id)
        return

    answer_callback(cb_id)  # always ack so spinner disappears

    if data.startswith("view:"):
        view = data.split(":", 1)[1]
        if view == "status":
            text, kb = render_status_compact()
            edit_message(chat_id, message_id, text, kb)
        elif view == "detail":
            kb = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "view:status"}]]}
            edit_message(chat_id, message_id, render_detail(), kb)
        elif view == "top":
            text, kb = render_top(sess.proc_sort)
            edit_message(chat_id, message_id, text, kb)
        elif view == "disks":
            kb = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "view:status"}]]}
            edit_message(chat_id, message_id, render_disks(), kb)
        elif view == "net":
            kb = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "view:status"}]]}
            edit_message(chat_id, message_id, render_net(), kb)
        elif view == "summary24":
            kb = {"inline_keyboard": [
                [{"text": "24h", "callback_data": "sum:24"},
                 {"text": "72h", "callback_data": "sum:72"},
                 {"text": "7d",  "callback_data": "sum:168"}],
                [{"text": "⬅️ Back", "callback_data": "view:status"}],
            ]}
            edit_message(chat_id, message_id, render_summary(24), kb)
        elif view == "logs":
            sess.page = 0
            show_years(chat_id, message_id)
        elif view == "settings":
            text, kb = render_settings()
            edit_message(chat_id, message_id, text, kb)
        return

    if data.startswith("sum:"):
        hours = int(data.split(":", 1)[1])
        kb = {"inline_keyboard": [
            [{"text": "24h", "callback_data": "sum:24"},
             {"text": "72h", "callback_data": "sum:72"},
             {"text": "7d",  "callback_data": "sum:168"}],
            [{"text": "⬅️ Back", "callback_data": "view:status"}],
        ]}
        edit_message(chat_id, message_id, render_summary(hours), kb)
        return

    if data.startswith("top:"):
        sort_by = data.split(":", 1)[1]
        sess.proc_sort = sort_by
        text, kb = render_top(sort_by)
        edit_message(chat_id, message_id, text, kb)
        return

    if data == "alerts:toggle":
        a = CONFIG.setdefault("alerts", {})
        a["enabled"] = not a.get("enabled", True)
        sm.save_config(CONFIG)
        text, kb = render_settings()
        edit_message(chat_id, message_id, text, kb)
        return

    if data.startswith("page:"):
        _, prefix, page_str = data.split(":", 2)
        sess.page = int(page_str)
        if prefix == "year":
            show_years(chat_id, message_id)
        elif prefix == "month":
            show_months(chat_id, message_id)
        elif prefix == "day":
            show_days(chat_id, message_id)
        return

    if data.startswith("back:"):
        target = data.split(":", 1)[1]
        sess.page = 0
        if target == "month":
            show_months(chat_id, message_id)
        elif target == "year":
            show_years(chat_id, message_id)
        else:
            text, kb = render_status_compact()
            edit_message(chat_id, message_id, text, kb)
        return

    if data.startswith("year:"):
        sess.year = data.split(":", 1)[1]
        sess.page = 0
        show_months(chat_id, message_id)
        return

    if data.startswith("month:"):
        sess.month = data.split(":", 1)[1]
        sess.page = 0
        show_days(chat_id, message_id)
        return

    if data.startswith("day:"):
        day = data.split(":", 1)[1]
        if not (sess.year and sess.month):
            edit_message(chat_id, message_id, "_Pick a year/month first._")
            return
        date_iso = f"{sess.year}-{sess.month}-{day}"
        sent = send_csv_for_date(chat_id, date_iso, caption=f"Metrics · {date_iso}")
        kb = {"inline_keyboard": [[{"text": "⬅️ Back to status", "callback_data": "view:status"}]]}
        edit_message(chat_id, message_id, "✅ Log sent." if sent else f"_No data for {date_iso}._", kb)
        return


# ---------------------------------------------------------------------------
# Message dispatch
# ---------------------------------------------------------------------------

def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if not is_authorized(user_id):
        send_message(chat_id, "Unauthorized.")
        return

    if not text:
        return

    if text.startswith("/"):
        # Strip @botname suffix Telegram adds in groups
        first, _, rest = text.partition(" ")
        cmd = first.split("@", 1)[0].lower()
        handler = COMMANDS.get(cmd)
        if handler:
            try:
                handler(chat_id, rest)
            except Exception:
                logger.exception("handler %s failed", cmd)
                send_message(chat_id, f"_Internal error handling_ `{md_escape(cmd)}`")
        else:
            send_message(chat_id, f"_Unknown command `{md_escape(cmd)}`. Try /help._")
    else:
        # Friendly default: show status
        send_message(chat_id, "Try /menu, /status, /top, /help.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _save_offset(offset: int) -> None:
    state = sm.load_state()
    state["bot_offset"] = offset
    sm.save_state(state)


def _load_offset() -> int | None:
    return sm.load_state().get("bot_offset")


def poll_loop() -> None:
    sm.db_init(auto_migrate=bool(CONFIG.get("storage", {}).get("auto_migrate_csv", True)))
    offset = _load_offset()
    stats = sm.db_stats()
    logger.info(
        "Bot starting · users=%d · poll_timeout=%ds · db_rows=%d",
        len(AUTHORIZED_USERS), POLL_TIMEOUT, stats["rows"],
    )
    backoff = 1.0
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            r = SESSION.get(f"{API_URL}/getUpdates", params=params, timeout=POLL_TIMEOUT + 10)
            if r.status_code != 200:
                logger.warning("getUpdates %s: %s", r.status_code, r.text[:200])
                time.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        handle_callback(update["callback_query"])
                    elif "message" in update:
                        handle_message(update["message"])
                except Exception:
                    logger.exception("update handler crashed")
            if data.get("result"):
                _save_offset(offset)  # persist only on progress
        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.RequestException as e:
            logger.warning("Network error: %s", e)
            time.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)
        except KeyboardInterrupt:
            logger.info("Interrupted — shutting down.")
            return


def _signal_handler(signum, _frame) -> None:
    logger.info("Signal %s received — exiting.", signum)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    # SIGTERM exists on Windows but cannot have a handler installed via
    # signal.signal() — guard so we don't crash on startup there.
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, OSError, AttributeError):
            pass
    poll_loop()
