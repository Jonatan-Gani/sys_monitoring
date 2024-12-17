import os
import json
import datetime
import csv
import psutil
import requests




# Define paths and directories
BASE_DIR = os.path.expanduser("~/sys_monitoring")
LOG_FILE_TEXT = os.path.join(BASE_DIR, "logs", "power_log.txt")
LOG_FILE_CSV = os.path.join(BASE_DIR, "logs", "power_log.csv")
ARCHIVE_DIR = os.path.join(BASE_DIR, "logs", "log_archive")
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
    # Load thresholds from config.json
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        THRESHOLDS = config["thresholds"]

    # Load sensitive data from .env file
    env_vars = load_env(ENV_FILE)
    TELEGRAM_BOT_TOKEN = env_vars.get("BOT_TOKEN")
    TELEGRAM_CHAT_ID = env_vars.get("CHAT_ID")

    # Validate that required environment variables are present
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("BOT_TOKEN or CHAT_ID not found in .env file!")

except (FileNotFoundError, KeyError, ValueError) as e:
    raise ValueError(f"Configuration error: {e}")





def send_telegram_alert(message):
    """Send a Telegram message via the bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            print(f"Failed to send Telegram alert: {response.text}")
    except requests.RequestException as e:
        print(f"Error sending Telegram alert: {e}")

def get_system_metrics():
    """Retrieve system metrics: CPU load, temperature, RAM usage, disk usage, and network stats."""
    # CPU load
    cpu_load = psutil.cpu_percent(interval=1)

    # Temperature (if available)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as temp_file:
            temperature = int(temp_file.read().strip()) / 1000  # Convert millidegrees to Celsius
    except FileNotFoundError:
        temperature = None

    # RAM usage
    ram_usage = psutil.virtual_memory().percent

    # Disk usage
    disk_usage = psutil.disk_usage('/').percent

    # Network activity
    net_io = psutil.net_io_counters()
    bytes_sent = net_io.bytes_sent / (1024 * 1024)  # Convert to MB
    bytes_recv = net_io.bytes_recv / (1024 * 1024)  # Convert to MB

    # Estimate power usage
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

def archive_logs_if_new_day():
    """Archive logs if the date has changed."""
    today = datetime.date.today()
    archive_date = today - datetime.timedelta(days=1)

    # Create filenames with day number and weekday name
    weekday_name = archive_date.strftime("%A")
    day_number = archive_date.day
    archive_name = f"{day_number}_{weekday_name}"

    # Paths for text and CSV archive files
    archive_path_text = os.path.join(
        ARCHIVE_DIR,
        str(archive_date.year),
        f"{archive_date.month:02d}",
        f"{archive_name}.txt"
    )
    archive_path_csv = os.path.join(
        ARCHIVE_DIR,
        str(archive_date.year),
        f"{archive_date.month:02d}",
        f"{archive_name}.csv"
    )

    # If logs exist and it’s a new day, archive them
    if os.path.exists(LOG_FILE_TEXT):
        os.makedirs(os.path.dirname(archive_path_text), exist_ok=True)
        os.rename(LOG_FILE_TEXT, archive_path_text)

    if os.path.exists(LOG_FILE_CSV):
        os.makedirs(os.path.dirname(archive_path_csv), exist_ok=True)
        os.rename(LOG_FILE_CSV, archive_path_csv)

def log_status():
    """Log system metrics and send alerts if thresholds are exceeded."""
    # Check and archive logs if needed
    archive_logs_if_new_day()

    # Get current system metrics
    metrics = get_system_metrics()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Prepare log entry
    log_entry_text = (
        f"{timestamp}, CPU Load: {metrics['cpu_load']:.2f}%, Temperature: {metrics['temperature']}°C, "
        f"RAM Usage: {metrics['ram_usage']:.2f}%, Disk Usage: {metrics['disk_usage']:.2f}%, "
        f"Network Sent: {metrics['bytes_sent']:.2f} MB, Network Received: {metrics['bytes_recv']:.2f} MB, "
        f"Estimated Power: {metrics['power_estimation']:.2f} W\n"
    )
    log_entry_csv = [
        timestamp,
        f"{metrics['cpu_load']:.2f}",
        f"{metrics['temperature']}" if metrics['temperature'] else "N/A",
        f"{metrics['ram_usage']:.2f}",
        f"{metrics['disk_usage']:.2f}",
        f"{metrics['bytes_sent']:.2f}",
        f"{metrics['bytes_recv']:.2f}",
        f"{metrics['power_estimation']:.2f}"
    ]

    # Ensure the base directory exists
    os.makedirs(BASE_DIR, exist_ok=True)

    # Write to the text log
    with open(LOG_FILE_TEXT, "a") as log_file_text:
        log_file_text.write(log_entry_text)

    # Write to the CSV log
    write_header = not os.path.exists(LOG_FILE_CSV)
    with open(LOG_FILE_CSV, "a", newline="") as log_file_csv:
        csv_writer = csv.writer(log_file_csv)
        if write_header:
            csv_writer.writerow([
                "Timestamp", "CPU Load (%)", "Temperature (°C)", "RAM Usage (%)",
                "Disk Usage (%)", "Network Sent (MB)", "Network Received (MB)", "Estimated Power (W)"
            ])
        csv_writer.writerow(log_entry_csv)

    # Check thresholds and send alerts
    if metrics["cpu_load"] > THRESHOLDS["cpu_load"]:
        send_telegram_alert(f"⚠️ High CPU Load: {metrics['cpu_load']:.2f}%")
    if metrics["temperature"] and metrics["temperature"] > THRESHOLDS["temperature"]:
        send_telegram_alert(f"🔥 High Temperature: {metrics['temperature']:.2f}°C")
    if metrics["power_estimation"] > THRESHOLDS["power"]:
        send_telegram_alert(f"⚡ High Power Consumption: {metrics['power_estimation']:.2f} W")
    if metrics["ram_usage"] > THRESHOLDS["ram_usage"]:
        send_telegram_alert(f"⚠️ High RAM Usage: {metrics['ram_usage']:.2f}%")
    if metrics["disk_usage"] > THRESHOLDS["disk_usage"]:
        send_telegram_alert(f"⚠️ High Disk Usage: {metrics['disk_usage']:.2f}%")


if __name__ == "__main__":
    log_status()
