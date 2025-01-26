import os
import requests
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USERS = os.getenv("AUTHORIZED_USERS", "").split(",")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOGS_DIRECTORY = "logs/log_archive"
LATEST_LOG_FILE = "logs/power_log.csv"


def get_log_file_path(year, month, day):
    return os.path.join(LOGS_DIRECTORY, f"{year}/Mon_{str(month).zfill(2)}/{day}_{datetime(year, month, day).strftime('%A')}.csv")


def user_is_authorized(user_id):
    return str(user_id) in AUTHORIZED_USERS


def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})


def send_document(chat_id, file_path):
    with open(file_path, "rb") as file:
        requests.post(f"{TELEGRAM_API_URL}/sendDocument", files={"document": file}, data={"chat_id": chat_id})


def handle_user_input(chat_id, user_id, text):
    if not user_is_authorized(user_id):
        send_message(chat_id, "Unauthorized user.")
        return

    if text.lower() == "c":
        if os.path.exists(LATEST_LOG_FILE):
            send_message(chat_id, "Fetching the latest log...")
            send_document(chat_id, LATEST_LOG_FILE)
        else:
            send_message(chat_id, "No logs available.")
        return

    parts = text.split("-")
    if len(parts) == 3:
        try:
            year, month, day = map(int, parts)
            log_path = get_log_file_path(year, month, day)
            if os.path.exists(log_path):
                send_message(chat_id, "Fetching the log...")
                send_document(chat_id, log_path)
            else:
                send_message(chat_id, "Log not found for the specified date.")
        except ValueError:
            send_message(chat_id, "Invalid date format. Use YYYY-MM-DD.")
    else:
        send_message(chat_id, "Invalid input. Use 'c' for the latest log or provide a date in YYYY-MM-DD format.")


def poll_updates():
    offset = None
    while True:
        response = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params={"offset": offset, "timeout": 30}).json()
        for update in response.get("result", []):
            offset = update["update_id"] + 1
            chat_id = update["message"]["chat"]["id"]
            user_id = update["message"]["from"]["id"]
            text = update["message"].get("text", "")
            handle_user_input(chat_id, user_id, text)


if __name__ == "__main__":
    poll_updates()
