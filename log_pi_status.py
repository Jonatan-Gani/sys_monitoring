import os
import json
import datetime
import csv
import psutil
import requests

# Define paths and directories
BASE_DIR = os.path.expanduser("~/sys_monitoring")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE_TEXT = os.path.join(LOG_DIR, "power_log.log")
LOG_FILE_CSV = os.path.join(LOG_DIR, "power_log.csv")
ARCHIVE_DIR = os.path.join(LOG_DIR, "log_archive")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

# Manually load environment variables from .env
def load_env(env_file):
    env_vars = {}
    try:
        with open(env_file, "r") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    key, value = line.strip().split("=", 1)
                    env_vars[key] = value
    except FileNotFoundError:
        raise FileNotFoundError(f"Environment file {env_file} not found!")
    return env_vars

# Load configuration
try:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        THRESHOLDS = config["thresholds"]
    env_vars = load_env(ENV_FILE)
    TELEGRAM_BOT_TOKEN = env_vars.get("BOT_TOKEN")
    TELEGRAM_CHAT_ID = env_vars.get("CHAT_ID")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("BOT_TOKEN or CHAT_ID not found in .env file!")
except (FileNotFoundError, KeyError, ValueError) as e:
    raise ValueError(f"Configuration error: {e}")

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            print(f"Failed to send Telegram alert: {response.text}")
    except requests.RequestException as e:
        print(f"Error sending Telegram alert: {e}")

def get_system_metrics():
    cpu_load = psutil.cpu_percent(interval=1)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as temp_file:
            temperature = int(temp_file.read().strip()) / 1000
    except FileNotFoundError:
        temperature = None
    ram_usage = psutil.virtual_memory().percent
    disk_usage = psutil.disk_usage('/').percent
    net_io = psutil.net_io_counters()
    bytes_sent = net_io.bytes_sent / (1024 * 1024)
    bytes_recv = net_io.bytes_recv / (1024 * 1024)
    power_estimation = 4.5 + (7.5 * (cpu_load / 100))
    return {
        "cpu_load": cpu_load,
        "temperature": temperature,
        "ram_usage": ram_usage,
        "disk_usage": disk_usage,
        "bytes_sent": bytes_sent,
        "bytes_recv": bytes_recv,
        "power_estimation": power_estimation
    }

def get_last_run_and_kwh(log_file_csv):
    """Retrieve the last timestamp and cumulative kWh consumption from the CSV."""
    try:
        if os.path.exists(log_file_csv) and os.path.getsize(log_file_csv) > 0:
            with open(log_file_csv, "r") as csvfile:
                reader = csv.DictReader(csvfile)
                rows = list(reader)
                if rows:
                    last_entry = rows[-1]
                    last_timestamp = datetime.datetime.strptime(last_entry["Timestamp"], "%Y-%m-%d %H:%M:%S")
                    last_kwh = float(last_entry.get("Cumulative kWh", 0))
                    return last_timestamp, last_kwh
    except Exception as e:
        print(f"Error reading last run: {e}")
    return None, 0.0

def archive_old_logs_if_date_changed():
    """Check if the existing log files are from a previous day, if so archive them and start fresh."""
    today = datetime.date.today()

    # Function to check if a file exists and if it's from a previous day
    def needs_archiving(file_path):
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            mtime = datetime.date.fromtimestamp(os.path.getmtime(file_path))
            if mtime < today:  # file last modified before today
                return True
        return False

    to_archive = []
    if needs_archiving(LOG_FILE_TEXT):
        to_archive.append(LOG_FILE_TEXT)
    if needs_archiving(LOG_FILE_CSV):
        to_archive.append(LOG_FILE_CSV)

    if to_archive:
        # Archive files based on yesterday's date since they contain yesterday's logs
        archive_date = today - datetime.timedelta(days=1)
        weekday_name = archive_date.strftime("%A")
        day_number = archive_date.day
        archive_name = f"{day_number}_{weekday_name}"

        archive_dir_text = os.path.join(
            ARCHIVE_DIR,
            str(archive_date.year),
            f"{archive_date.strftime('%b')}_{archive_date.month:02d}"
        )

        os.makedirs(archive_dir_text, exist_ok=True)

        for fpath in to_archive:
            ext = os.path.splitext(fpath)[1]  # .log or .csv
            archive_path = os.path.join(archive_dir_text, f"{archive_name}{ext}")
            if os.path.exists(archive_path):
                # If archive file exists, append a unique suffix
                count = 1
                base_name = archive_path.replace(ext, "")
                while os.path.exists(archive_path):
                    archive_path = f"{base_name}_{count}{ext}"
                    count += 1
            os.rename(fpath, archive_path)


def log_status():
    # Archive logs if the date has changed
    archive_old_logs_if_date_changed()

    # Retrieve last log time
    last_timestamp, _ = get_last_run_and_kwh(LOG_FILE_CSV)

    # Get current system metrics
    metrics = get_system_metrics()
    timestamp = datetime.datetime.now()
    timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    # Calculate elapsed time in hours (down to seconds)
    if last_timestamp:
        elapsed_hours = (timestamp - last_timestamp).total_seconds() / 3600
    else:
        elapsed_hours = 0  # First run, no consumption calculated

    # Calculate power consumed for the interval in Wh
    if elapsed_hours > 0:
        wh_consumed = metrics["power_estimation"] * elapsed_hours
    else:
        wh_consumed = 0.0

    # Ensure the log directory exists
    os.makedirs(LOG_DIR, exist_ok=True)

    # Check if CSV exists
    file_exists = os.path.exists(LOG_FILE_CSV) and os.path.getsize(LOG_FILE_CSV) > 0

    # Write metrics to CSV
    with open(LOG_FILE_CSV, "a", newline="") as log_file_csv:
        csv_writer = csv.writer(log_file_csv)
        if not file_exists:
            csv_writer.writerow([
                "Timestamp", "CPU Load (%)", "Temperature (°C)", "RAM Usage (%)",
                "Disk Usage (%)", "Network Sent (MB)", "Network Received (MB)",
                "Estimated Power (W)", "Interval Wh"
            ])
        csv_writer.writerow([
            timestamp_str,
            f"{metrics['cpu_load']:.2f}",
            f"{metrics['temperature']:.2f}" if metrics['temperature'] is not None else "N/A",
            f"{metrics['ram_usage']:.2f}",
            f"{metrics['disk_usage']:.2f}",
            f"{metrics['bytes_sent']:.2f}",
            f"{metrics['bytes_recv']:.2f}",
            f"{metrics['power_estimation']:.2f}",
            f"{wh_consumed:.4f}"
        ])

    # Check thresholds and send alerts
    if metrics["cpu_load"] > THRESHOLDS["cpu_load"]:
        send_telegram_alert(f"⚠️ High CPU Load: {metrics['cpu_load']:.2f}%")
    if metrics["temperature"] is not None and metrics["temperature"] > THRESHOLDS["temperature"]:
        send_telegram_alert(f"🔥 High Temperature: {metrics['temperature']:.2f}°C")
    if metrics["power_estimation"] > THRESHOLDS["power"]:
        send_telegram_alert(f"⚡ High Power Consumption: {metrics['power_estimation']:.2f} W")
    if metrics["ram_usage"] > THRESHOLDS["ram_usage"]:
        send_telegram_alert(f"⚠️ High RAM Usage: {metrics['ram_usage']:.2f}%")
    if metrics["disk_usage"] > THRESHOLDS["disk_usage"]:
        send_telegram_alert(f"⚠️ High Disk Usage: {metrics['disk_usage']:.2f}%")



if __name__ == "__main__":
    log_status()
