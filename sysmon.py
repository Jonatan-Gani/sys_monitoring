#!/usr/bin/env python3
"""sysmon — management CLI for the sys_monitoring project.

Subcommands:
    init                       Interactive first-time setup
    doctor                     Run health checks
    update                     git pull + reinstall deps + run migrations
    logger run                 Run one logger pass (same as cron does)
    bot run                    Run the Telegram bot in the foreground
    service install            Install OS service / scheduled task
    service uninstall          Remove them
    service start|stop|status  Control the OS service / scheduled task
    config list|get|set        Manage config.json
    db stats|backup|prune|import-csv
    test telegram              Send a test message via the configured bot
    version                    Print sysmon version

All commands are non-interactive unless `init` is used. Errors exit with
status 1; doctor returns 1 if any check fails (useful in CI / cron).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# Make the project root importable regardless of cwd.
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import sysmon_lib as sm  # noqa: E402


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

class _C:
    OK = "\033[32m"
    WARN = "\033[33m"
    ERR = "\033[31m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(s: str, code: str) -> str:
    return f"{code}{s}{_C.RESET}" if _color() else s


def ok(msg: str) -> None:   print(_c(f"✓ {msg}", _C.OK))
def warn(msg: str) -> None: print(_c(f"⚠ {msg}", _C.WARN))
def err(msg: str) -> None:  print(_c(f"✗ {msg}", _C.ERR))
def info(msg: str) -> None: print(msg)
def head(msg: str) -> None: print(_c(msg, _C.BOLD))


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        raw = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    """Prompt with optional default. Empty input (Enter) accepts the default."""
    if default is not None:
        suffix = f" [{default}] (Enter to keep)"
    else:
        suffix = ""
    label_full = f"{label}{suffix}: "
    while True:
        if secret:
            import getpass
            value = getpass.getpass(label_full).strip()
        else:
            try:
                value = input(label_full).strip()
            except EOFError:
                value = ""
        if value:
            return value
        if default is not None:
            return default


def prompt_user_ids(label: str, default: str | None = None) -> str:
    """Prompt for a comma-separated list of Telegram numeric user IDs.

    Loops until the input is either empty (accept default) or parses cleanly,
    so a one-char typo like 'y' can't quietly disable the whitelist.
    """
    while True:
        raw = prompt(label, default=default)
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        if all(s.isdigit() for s in ids) and ids:
            return ",".join(ids)
        err(f"'{raw}' doesn't look like a comma-separated list of Telegram numeric IDs.")
        warn("Press Enter on its own line to keep the default. Try again.")


# ---------------------------------------------------------------------------
# Common paths / facts
# ---------------------------------------------------------------------------

REPO_DIR = str(PROJECT_DIR)
PYTHON = sys.executable
SVC_BOT_NAME = "sysmon_bot"
SVC_LOGGER_NAME = "sysmon_logger"


# ---------------------------------------------------------------------------
# Verification primitives (used by both init and doctor)
# ---------------------------------------------------------------------------

REQUIRED_PY = (3, 10)
REQUIRED_DEPS = ("psutil", "requests")


def check_python() -> tuple[bool, str]:
    v = sys.version_info
    actual = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= REQUIRED_PY:
        return True, f"Python {actual}"
    return False, f"Python {actual} found; need >= {REQUIRED_PY[0]}.{REQUIRED_PY[1]}"


def check_deps() -> tuple[bool, str]:
    missing = []
    versions = []
    for mod in REQUIRED_DEPS:
        try:
            m = __import__(mod)
            versions.append(f"{mod} {getattr(m, '__version__', '?')}")
        except ImportError:
            missing.append(mod)
    if missing:
        return False, f"missing: {', '.join(missing)}. Run: {PYTHON} -m pip install -r requirements.txt"
    return True, "deps: " + ", ".join(versions)


def check_env() -> tuple[bool, str]:
    env = sm.load_env()
    missing = [k for k in ("BOT_TOKEN", "CHAT_ID") if not env.get(k)]
    if not os.path.exists(sm.ENV_FILE):
        return False, ".env not found — run `sysmon init`"
    if missing:
        return False, f".env present but missing keys: {', '.join(missing)}"
    return True, ".env present with BOT_TOKEN and CHAT_ID"


def check_telegram() -> tuple[bool, str]:
    env = sm.load_env()
    token = env.get("BOT_TOKEN")
    chat_id = env.get("CHAT_ID")
    if not token:
        return False, "no BOT_TOKEN in .env"
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code != 200:
            return False, f"getMe -> {r.status_code}: {r.text[:200]}"
        username = r.json().get("result", {}).get("username", "?")
    except Exception as e:
        return False, f"Telegram request failed: {e}"

    if chat_id:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": chat_id}, timeout=10,
            )
            if r.status_code != 200:
                return False, f"getChat({chat_id}) -> {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"getChat failed: {e}"

    return True, f"bot @{username}; chat {chat_id} reachable"


def check_db() -> tuple[bool, str]:
    try:
        sm.db_init(auto_migrate=False)
        s = sm.db_stats()
    except Exception as e:
        return False, f"DB error: {e}"
    size_mb = s["size_bytes"] / (1024 * 1024)
    return True, f"DB {s['rows']:,} rows, {size_mb:.2f} MB"


def check_logger_recent() -> tuple[bool, str]:
    """Pass if the most recent metric row is < 5 minutes old."""
    last = sm.db_last_metric()
    if not last:
        return False, "no rows in DB yet — has the logger run?"
    age = time.time() - last["ts"]
    if age > 300:
        return False, f"latest row is {int(age // 60)} min old — logger schedule may be missing"
    return True, f"latest row {int(age)}s old"


def check_disk_free() -> tuple[bool, str]:
    try:
        usage = shutil.disk_usage(REPO_DIR)
        free_mb = usage.free / (1024 * 1024)
    except OSError as e:
        return False, f"disk check failed: {e}"
    if free_mb < 100:
        return False, f"only {free_mb:.0f} MB free in {REPO_DIR}"
    return True, f"{free_mb / 1024:.1f} GB free in repo dir"


def check_service_installed() -> tuple[bool, str]:
    installed = _service_state()
    if not installed["any"]:
        return False, "no OS service installed (use `sysmon service install`)"
    parts = []
    if installed["bot"]:
        parts.append("bot:" + installed["bot_state"])
    if installed["logger"]:
        parts.append("logger:" + installed["logger_state"])
    return True, ", ".join(parts)


# ---------------------------------------------------------------------------
# Service install / uninstall (Linux + Windows)
# ---------------------------------------------------------------------------

def _service_state() -> dict:
    """Detect which services / scheduled tasks are installed and their states."""
    out = {"any": False, "bot": False, "logger": False,
           "bot_state": "—", "logger_state": "—"}

    if sm.IS_WINDOWS:
        for task, key in ((SVC_BOT_NAME, "bot"), (SVC_LOGGER_NAME, "logger")):
            r = _run(["schtasks", "/Query", "/TN", task, "/FO", "LIST"], check=False)
            if r.returncode == 0:
                out["any"] = True
                out[key] = True
                state = "unknown"
                for line in r.stdout.splitlines():
                    if line.strip().lower().startswith("status:"):
                        state = line.split(":", 1)[1].strip()
                        break
                out[f"{key}_state"] = state
        return out

    # Linux / systemd
    if shutil.which("systemctl"):
        for unit, key in ((f"{SVC_BOT_NAME}.service", "bot"),
                          (f"{SVC_LOGGER_NAME}.service", "logger")):
            r = _run(["systemctl", "list-unit-files", unit, "--no-legend"], check=False)
            if r.returncode == 0 and unit in r.stdout:
                out["any"] = True
                out[key] = True
                active = _run(["systemctl", "is-active", unit], check=False)
                out[f"{key}_state"] = active.stdout.strip() or "?"

        # Logger may also be installed as a cron line (only check if crontab exists)
        if not out["logger"] and shutil.which("crontab"):
            try:
                crontab = _run(["crontab", "-l"], check=False)
                if crontab.returncode == 0 and "log_pi_status.py" in crontab.stdout:
                    out["any"] = True
                    out["logger"] = True
                    out["logger_state"] = "cron"
            except (OSError, FileNotFoundError):
                pass

    return out


def _run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, **kw)


# --- Linux systemd units -----------------------------------------------------

_BOT_UNIT_TEMPLATE = """\
[Unit]
Description=sys_monitoring Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo}
ExecStart={python} {repo}/tg_bot_loop.py
Restart=on-failure
RestartSec=5
{user_line}

[Install]
WantedBy={target}
"""

_LOGGER_UNIT_TEMPLATE = """\
[Unit]
Description=sys_monitoring metrics collector (one-shot)
After=network.target

[Service]
Type=oneshot
WorkingDirectory={repo}
ExecStart={python} {repo}/log_pi_status.py
{user_line}
"""

_LOGGER_TIMER_TEMPLATE = """\
[Unit]
Description=Run sys_monitoring metrics collector every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
Unit={logger}.service

[Install]
WantedBy=timers.target
"""


def _systemd_paths(system_wide: bool) -> tuple[str, list[str]]:
    """Return (unit_dir, systemctl_args). system_wide=False uses user units."""
    if system_wide:
        return "/etc/systemd/system", ["sudo", "systemctl"]
    user_dir = os.path.expanduser("~/.config/systemd/user")
    return user_dir, ["systemctl", "--user"]


def _install_systemd(system_wide: bool) -> None:
    unit_dir, sctl = _systemd_paths(system_wide)
    target = "multi-user.target" if system_wide else "default.target"
    user_line = ""
    if system_wide:
        user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
        user_line = f"User={user}"

    os.makedirs(unit_dir, exist_ok=True) if not system_wide else None
    bot_unit = _BOT_UNIT_TEMPLATE.format(
        repo=REPO_DIR, python=PYTHON, target=target, user_line=user_line,
    )
    logger_unit = _LOGGER_UNIT_TEMPLATE.format(
        repo=REPO_DIR, python=PYTHON, user_line=user_line,
    )
    logger_timer = _LOGGER_TIMER_TEMPLATE.format(logger=SVC_LOGGER_NAME)

    bot_path = os.path.join(unit_dir, f"{SVC_BOT_NAME}.service")
    logger_path = os.path.join(unit_dir, f"{SVC_LOGGER_NAME}.service")
    timer_path = os.path.join(unit_dir, f"{SVC_LOGGER_NAME}.timer")

    def _write(path: str, content: str) -> None:
        if system_wide:
            _run(["sudo", "tee", path], input=content, check=True)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    _write(bot_path, bot_unit)
    _write(logger_path, logger_unit)
    _write(timer_path, logger_timer)

    _run(sctl + ["daemon-reload"])
    _run(sctl + ["enable", "--now", f"{SVC_BOT_NAME}.service"])
    _run(sctl + ["enable", "--now", f"{SVC_LOGGER_NAME}.timer"])
    ok(f"Installed systemd units in {unit_dir}")
    ok(f"  - {SVC_BOT_NAME}.service     (bot, restart on failure)")
    ok(f"  - {SVC_LOGGER_NAME}.timer    (logger, every 1 minute)")


def _uninstall_systemd(system_wide: bool) -> None:
    unit_dir, sctl = _systemd_paths(system_wide)
    for unit in (f"{SVC_LOGGER_NAME}.timer", f"{SVC_BOT_NAME}.service",
                 f"{SVC_LOGGER_NAME}.service"):
        _run(sctl + ["disable", "--now", unit], check=False)
    for name in (f"{SVC_BOT_NAME}.service", f"{SVC_LOGGER_NAME}.service",
                 f"{SVC_LOGGER_NAME}.timer"):
        path = os.path.join(unit_dir, name)
        if system_wide:
            _run(["sudo", "rm", "-f", path], check=False)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    _run(sctl + ["daemon-reload"])
    ok("Removed systemd units.")


# --- Windows Task Scheduler --------------------------------------------------

def _install_windows_tasks() -> None:
    # Logger: every minute, highest run level
    pyw = PYTHON.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = PYTHON  # fall back if pythonw isn't present
    bot_action = f'"{pyw}" "{os.path.join(REPO_DIR, "tg_bot_loop.py")}"'
    logger_action = f'"{PYTHON}" "{os.path.join(REPO_DIR, "log_pi_status.py")}"'

    _run(["schtasks", "/Create", "/F",
          "/TN", SVC_LOGGER_NAME,
          "/TR", logger_action,
          "/SC", "MINUTE", "/MO", "1",
          "/RL", "HIGHEST"])
    _run(["schtasks", "/Create", "/F",
          "/TN", SVC_BOT_NAME,
          "/TR", bot_action,
          "/SC", "ONLOGON",
          "/RL", "HIGHEST"])
    # Start the bot immediately
    _run(["schtasks", "/Run", "/TN", SVC_BOT_NAME], check=False)
    ok("Installed Windows scheduled tasks:")
    ok(f"  - {SVC_LOGGER_NAME}  (every minute)")
    ok(f"  - {SVC_BOT_NAME}     (at logon; started now)")


def _uninstall_windows_tasks() -> None:
    for task in (SVC_BOT_NAME, SVC_LOGGER_NAME):
        _run(["schtasks", "/End", "/TN", task], check=False)
        _run(["schtasks", "/Delete", "/F", "/TN", task], check=False)
    ok("Removed Windows scheduled tasks.")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_version(_args) -> int:
    print(f"sysmon {sm.__version__}")
    print(f"  python    {sys.version.split()[0]}")
    print(f"  platform  {sys.platform}")
    print(f"  repo      {REPO_DIR}")
    return 0


def cmd_doctor(args) -> int:
    checks = [
        ("Python",         check_python),
        ("Dependencies",   check_deps),
        ("Environment",    check_env),
        ("Telegram API",   check_telegram),
        ("Database",       check_db),
        ("Logger freshness", check_logger_recent),
        ("Disk space",     check_disk_free),
        ("Service",        check_service_installed),
    ]
    failed = 0
    for label, fn in checks:
        try:
            passing, msg = fn()
        except Exception as e:
            passing, msg = False, f"check crashed: {e}"
        prefix = f"{label:<20}"
        if passing:
            ok(f"{prefix} {msg}")
        else:
            err(f"{prefix} {msg}")
            failed += 1
    print()
    if failed:
        err(f"{failed} check(s) failed.")
        return 1
    ok("All checks passed.")
    return 0


def cmd_init(args) -> int:
    head("sys_monitoring — initial setup")
    print()

    passing, msg = check_python()
    (ok if passing else err)(msg)
    if not passing:
        return 1

    passing, msg = check_deps()
    if passing:
        ok(msg)
    else:
        warn(msg)
        if confirm("Install dependencies now?", True):
            _run([PYTHON, "-m", "pip", "install", "-r",
                  os.path.join(REPO_DIR, "requirements.txt")], check=True)
            ok("Dependencies installed.")
        else:
            return 1

    # --- .env ---
    env = sm.load_env()
    print()
    head("Telegram credentials")
    token = prompt("BOT_TOKEN", default=env.get("BOT_TOKEN"), secret=False)
    # Validate token now so the user finds out immediately
    import requests
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code != 200:
            err(f"Token rejected by Telegram: {r.text[:200]}")
            return 1
        username = r.json()["result"]["username"]
        ok(f"Token valid → @{username}")
    except Exception as e:
        err(f"Telegram unreachable: {e}")
        return 1

    chat_id = env.get("CHAT_ID")
    if not chat_id and confirm("Discover CHAT_ID by sending a message to your bot now?", True):
        chat_id = _discover_chat_id(token)
    if not chat_id:
        chat_id = prompt("CHAT_ID", default=env.get("CHAT_ID"))

    authorized = env.get("AUTHORIZED_USERS") or chat_id
    authorized = prompt_user_ids(
        "AUTHORIZED_USERS (Telegram numeric IDs, comma-separated)",
        default=authorized,
    )

    env_text = textwrap.dedent(f"""\
        BOT_TOKEN={token}
        CHAT_ID={chat_id}
        AUTHORIZED_USERS={authorized}
    """)
    with open(sm.ENV_FILE, "w", encoding="utf-8") as f:
        f.write(env_text)
    if not sm.IS_WINDOWS:
        try:
            os.chmod(sm.ENV_FILE, 0o600)
        except OSError:
            pass
    ok(f"Wrote {sm.ENV_FILE}")

    # --- DB ---
    print()
    head("Database")
    sm.db_init(auto_migrate=True)
    s = sm.db_stats()
    ok(f"Initialized at {sm.DB_PATH} ({s['rows']:,} rows)")

    # --- Test message ---
    print()
    head("Telegram test")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": "✅ sys_monitoring is set up."},
            timeout=10,
        )
        if r.status_code == 200:
            ok("Sent test message — check your Telegram chat.")
        else:
            warn(f"sendMessage -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        warn(f"Test message failed: {e}")

    # --- Service ---
    print()
    head("Service")
    if confirm("Install the bot as a service and schedule the logger every minute?", True):
        try:
            return _service_install_inner()
        except Exception as e:
            err(f"Service install failed: {e}")
            return 1
    else:
        info("Skipped. Run `sysmon service install` later when ready.")

    print()
    ok("Setup complete. Try: sysmon doctor")
    return 0


def _discover_chat_id(token: str) -> str | None:
    """Poll getUpdates for ~60s to grab the first chat_id that messages the bot."""
    import requests
    info("Open Telegram, search for your bot, and send any message.")
    info("Waiting up to 60 seconds…")
    deadline = time.time() + 60
    offset = None
    while time.time() < deadline:
        try:
            params = {"timeout": 10}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                             params=params, timeout=15)
            if r.status_code != 200:
                continue
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message") or {}
                chat = msg.get("chat") or {}
                if chat.get("id"):
                    cid = str(chat["id"])
                    ok(f"Detected chat_id {cid}")
                    return cid
        except Exception:
            pass
    warn("No message received in time.")
    return None


def cmd_update(args) -> int:
    head("Pulling latest changes")
    if not shutil.which("git"):
        err("git not installed; cannot self-update.")
        return 1

    pull = _run(["git", "-C", REPO_DIR, "pull", "--ff-only"], check=False)
    print(pull.stdout, end="")
    if pull.returncode != 0:
        err(pull.stderr.strip())
        return 1

    head("Reinstalling dependencies")
    _run([PYTHON, "-m", "pip", "install", "-r",
          os.path.join(REPO_DIR, "requirements.txt"), "--quiet"], check=True)
    ok("Dependencies up to date.")

    head("Running migrations")
    sm.db_init(auto_migrate=False)
    ok(f"Schema at version {sm.DB_SCHEMA_VERSION}")

    head("Restarting service")
    state = _service_state()
    if state["bot"]:
        cmd_service_restart_internal()
    else:
        info("No service installed; skipping restart.")

    print()
    ok(f"sys_monitoring now at v{sm.__version__}")
    return 0


def cmd_logger_run(args) -> int:
    import log_pi_status
    log_pi_status.main()
    return 0


def cmd_bot_run(args) -> int:
    import tg_bot_loop
    tg_bot_loop.poll_loop()
    return 0


# --- service subcommands -----------------------------------------------------

def _service_install_inner() -> int:
    if sm.IS_WINDOWS:
        _install_windows_tasks()
        return 0
    if sm.IS_LINUX and shutil.which("systemctl"):
        system_wide = os.geteuid() == 0
        if not system_wide:
            info("Installing as user services (no sudo). Pass `--system` for system-wide install.")
        _install_systemd(system_wide=system_wide)
        return 0
    err("Unsupported platform / no service manager available.")
    return 1


def cmd_service_install(args) -> int:
    try:
        if sm.IS_LINUX and shutil.which("systemctl"):
            _install_systemd(system_wide=getattr(args, "system", False))
            return 0
        return _service_install_inner()
    except subprocess.CalledProcessError as e:
        err(f"command failed: {e.stderr or e.stdout}")
        return 1


def cmd_service_uninstall(args) -> int:
    try:
        if sm.IS_WINDOWS:
            _uninstall_windows_tasks()
        elif sm.IS_LINUX and shutil.which("systemctl"):
            _uninstall_systemd(system_wide=getattr(args, "system", False))
        else:
            err("Unsupported platform.")
            return 1
    except subprocess.CalledProcessError as e:
        err(f"command failed: {e.stderr or e.stdout}")
        return 1
    return 0


def _service_action(action: str) -> int:
    if sm.IS_WINDOWS:
        verb = {"start": "/Run", "stop": "/End"}.get(action)
        if not verb:
            err(f"action {action!r} not supported on Windows")
            return 1
        for task in (SVC_BOT_NAME, SVC_LOGGER_NAME):
            r = _run(["schtasks", verb, "/TN", task], check=False)
            if r.returncode == 0:
                ok(f"{action}ed {task}")
            else:
                warn(f"{task}: {r.stderr.strip() or r.stdout.strip()}")
        return 0
    if sm.IS_LINUX and shutil.which("systemctl"):
        system_wide = os.path.exists(f"/etc/systemd/system/{SVC_BOT_NAME}.service")
        sctl = (["sudo", "systemctl"] if system_wide else ["systemctl", "--user"])
        for unit in (f"{SVC_BOT_NAME}.service", f"{SVC_LOGGER_NAME}.timer"):
            r = _run(sctl + [action, unit], check=False)
            if r.returncode == 0:
                ok(f"{action} {unit}")
            else:
                warn(f"{unit}: {r.stderr.strip() or 'ok'}")
        return 0
    err("Unsupported platform.")
    return 1


def cmd_service_start(args) -> int:    return _service_action("start")
def cmd_service_stop(args) -> int:     return _service_action("stop")
def cmd_service_restart_internal() -> int: return _service_action("restart")


def cmd_service_status(args) -> int:
    state = _service_state()
    if not state["any"]:
        warn("No services installed.")
        return 1
    head("Services")
    if state["bot"]:
        info(f"  bot    : {state['bot_state']}")
    if state["logger"]:
        info(f"  logger : {state['logger_state']}")
    return 0


# --- config ------------------------------------------------------------------

def _get_nested(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def _set_nested(d: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            raise KeyError(dotted)
    cur[parts[-1]] = value


def _coerce(value: str):
    """Best-effort type coercion for `config set` from command-line strings."""
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        pass
    return value


def cmd_config_list(_args) -> int:
    cfg = sm.load_config()
    print(json.dumps(cfg, indent=2, sort_keys=True))
    return 0


def cmd_config_get(args) -> int:
    cfg = sm.load_config()
    try:
        value = _get_nested(cfg, args.key)
    except KeyError:
        err(f"key not found: {args.key}")
        return 1
    print(json.dumps(value) if isinstance(value, (dict, list)) else value)
    return 0


def cmd_config_set(args) -> int:
    cfg = sm.load_config()
    try:
        _set_nested(cfg, args.key, _coerce(args.value))
    except KeyError:
        err(f"invalid key: {args.key}")
        return 1
    sm.save_config(cfg)
    ok(f"{args.key} = {args.value}")
    return 0


# --- db ----------------------------------------------------------------------

def cmd_db_stats(_args) -> int:
    sm.db_init(auto_migrate=False)
    s = sm.db_stats()
    size_mb = s["size_bytes"] / (1024 * 1024)
    head("Database")
    info(f"  path       : {sm.DB_PATH}")
    info(f"  rows       : {s['rows']:,}")
    info(f"  size       : {size_mb:.2f} MB")
    if s["first_ts"]:
        info(f"  first      : {dt.datetime.fromtimestamp(s['first_ts'])}")
        info(f"  last       : {dt.datetime.fromtimestamp(s['last_ts'])}")
    return 0


def cmd_db_backup(_args) -> int:
    sm.db_init(auto_migrate=False)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(sm.LOG_DIR, f"sysmon_backup_{stamp}.db")
    # Use SQLite's online backup API so it's safe while the logger is writing
    import sqlite3
    src = sqlite3.connect(sm.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close(); dst.close()
    ok(f"Backed up to {dest}")
    return 0


def cmd_db_prune(args) -> int:
    sm.db_init(auto_migrate=False)
    removed = sm.db_purge_older_than(args.days)
    ok(f"Pruned {removed} rows older than {args.days} days.")
    return 0


def cmd_db_import_csv(_args) -> int:
    sm.db_init(auto_migrate=False)
    n = sm.migrate_csv_to_db()
    ok(f"Imported {n} rows from legacy CSV files.")
    return 0


# --- test --------------------------------------------------------------------

def cmd_test_telegram(_args) -> int:
    env = sm.load_env()
    token = env.get("BOT_TOKEN")
    chat_id = env.get("CHAT_ID")
    if not (token and chat_id):
        err("BOT_TOKEN or CHAT_ID missing in .env")
        return 1
    import requests
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": "🛠️ sysmon test message"},
        timeout=10,
    )
    if r.status_code == 200:
        ok("Sent.")
        return 0
    err(f"Telegram error: {r.status_code} {r.text[:200]}")
    return 1


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sysmon",
        description="Management CLI for sys_monitoring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")
    sub.required = True

    sub.add_parser("init",    help="Interactive first-time setup").set_defaults(func=cmd_init)
    sub.add_parser("doctor",  help="Run health checks").set_defaults(func=cmd_doctor)
    sub.add_parser("update",  help="git pull, reinstall deps, run migrations, restart").set_defaults(func=cmd_update)
    sub.add_parser("version", help="Print version").set_defaults(func=cmd_version)

    p_logger = sub.add_parser("logger", help="Logger subcommands")
    p_logger_sub = p_logger.add_subparsers(dest="logger_cmd"); p_logger_sub.required = True
    p_logger_sub.add_parser("run", help="Run one logger pass").set_defaults(func=cmd_logger_run)

    p_bot = sub.add_parser("bot", help="Bot subcommands")
    p_bot_sub = p_bot.add_subparsers(dest="bot_cmd"); p_bot_sub.required = True
    p_bot_sub.add_parser("run", help="Run the Telegram bot (foreground)").set_defaults(func=cmd_bot_run)

    p_svc = sub.add_parser("service", help="OS service / scheduled task management")
    p_svc_sub = p_svc.add_subparsers(dest="svc_cmd"); p_svc_sub.required = True
    p_inst = p_svc_sub.add_parser("install",   help="Install service / scheduled task")
    p_inst.add_argument("--system", action="store_true", help="(Linux) install system-wide via sudo")
    p_inst.set_defaults(func=cmd_service_install)
    p_uninst = p_svc_sub.add_parser("uninstall", help="Remove service / scheduled task")
    p_uninst.add_argument("--system", action="store_true")
    p_uninst.set_defaults(func=cmd_service_uninstall)
    p_svc_sub.add_parser("start",  help="Start the service").set_defaults(func=cmd_service_start)
    p_svc_sub.add_parser("stop",   help="Stop the service").set_defaults(func=cmd_service_stop)
    p_svc_sub.add_parser("status", help="Show service status").set_defaults(func=cmd_service_status)

    p_cfg = sub.add_parser("config", help="Manage config.json")
    p_cfg_sub = p_cfg.add_subparsers(dest="cfg_cmd"); p_cfg_sub.required = True
    p_cfg_sub.add_parser("list", help="Print full config").set_defaults(func=cmd_config_list)
    pg = p_cfg_sub.add_parser("get", help="Get a config key (dotted path)")
    pg.add_argument("key"); pg.set_defaults(func=cmd_config_get)
    psr = p_cfg_sub.add_parser("set", help="Set a config key (dotted path)")
    psr.add_argument("key"); psr.add_argument("value")
    psr.set_defaults(func=cmd_config_set)

    p_db = sub.add_parser("db", help="Database operations")
    p_db_sub = p_db.add_subparsers(dest="db_cmd"); p_db_sub.required = True
    p_db_sub.add_parser("stats",      help="Show DB stats").set_defaults(func=cmd_db_stats)
    p_db_sub.add_parser("backup",     help="Copy DB to a timestamped file").set_defaults(func=cmd_db_backup)
    p_db_sub.add_parser("import-csv", help="Re-import legacy CSV archives").set_defaults(func=cmd_db_import_csv)
    p_prune = p_db_sub.add_parser("prune", help="Delete rows older than N days")
    p_prune.add_argument("days", type=int)
    p_prune.set_defaults(func=cmd_db_prune)

    p_test = sub.add_parser("test", help="Test integrations")
    p_test_sub = p_test.add_subparsers(dest="test_cmd"); p_test_sub.required = True
    p_test_sub.add_parser("telegram", help="Send a test message").set_defaults(func=cmd_test_telegram)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
