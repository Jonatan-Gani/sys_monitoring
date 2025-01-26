import os
import requests
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USERS = os.getenv("AUTHORIZED_USERS", "").split(",")  # Comma-separated list of authorized users
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOGS_DIRECTORY = "logs/log_archive"
LATEST_LOG_FILE = "logs/power_log.csv"

# Helper functions
def get_available_years():
    if not os.path.exists(LOGS_DIRECTORY):
        return []
    return sorted([d for d in os.listdir(LOGS_DIRECTORY) if d.isdigit()])

def get_available_months(year):
    year_path = os.path.join(LOGS_DIRECTORY, year)
    if not os.path.exists(year_path):
        return []
    return sorted([d for d in os.listdir(year_path) if d.startswith("Mon_")])

def get_available_days(year, month):
    month_path = os.path.join(LOGS_DIRECTORY, year, month)
    if not os.path.exists(month_path):
        return []
    return sorted([f.split("_")[0] for f in os.listdir(month_path) if f.endswith(".csv")])

def user_is_authorized(user_id):
    return str(user_id) in AUTHORIZED_USERS

def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})

def send_document(chat_id, file_path):
    with open(file_path, "rb") as file:
        requests.post(f"{TELEGRAM_API_URL}/sendDocument", files={"document": file}, data={"chat_id": chat_id})

def handle_user_input(chat_id, user_id, text, user_data):
    if not user_is_authorized(user_id):
        send_message(chat_id, "Unauthorized user.")
        return

    stage = user_data.get("stage", "year")

    if stage == "year":
        years = get_available_years()
        if text in years:
            user_data["year"] = text
            user_data["stage"] = "month"
            months = get_available_months(text)
            send_message(chat_id, f"Available months: {', '.join(months)}\nEnter the month (e.g., Mon_12):")
        else:
            send_message(chat_id, f"Invalid year. Available years: {', '.join(years)}")

    elif stage == "month":
        year = user_data.get("year")
        months = get_available_months(year)
        if text in months:
            user_data["month"] = text
            user_data["stage"] = "day"
            days = get_available_days(year, text)
            send_message(chat_id, f"Available days: {', '.join(days)}\nEnter the day:")
        else:
            send_message(chat_id, f"Invalid month. Available months: {', '.join(months)}")

    elif stage == "day":
        year, month = user_data.get("year"), user_data.get("month")
        days = get_available_days(year, month)
        if text in days:
            log_path = os.path.join(LOGS_DIRECTORY, year, month, f"{text}_{datetime.strptime(text, '%d').strftime('%A')}.csv")
            if os.path.exists(log_path):
                send_message(chat_id, "Fetching the log...")
                send_document(chat_id, log_path)
            else:
                send_message(chat_id, "Log not found for the specified date.")
        else:
            send_message(chat_id, f"Invalid day. Available days: {', '.join(days)}")

def poll_updates():
    offset = None
    user_sessions = {}

    while True:
        response = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params={"offset": offset, "timeout": 30}).json()

        for update in response.get("result", []):
            offset = update["update_id"] + 1
            chat_id = update["message"]["chat"]["id"]
            user_id = update["message"]["from"]["id"]
            text = update["message"].get("text", "")

            if chat_id not in user_sessions:
                user_sessions[chat_id] = {}

            if text.lower() == "/getlog":
                user_sessions[chat_id] = {"stage": "year"}
                years = get_available_years()
                send_message(chat_id, f"Available years: {', '.join(years)}\nEnter the year:")
            else:
                handle_user_input(chat_id, user_id, text, user_sessions[chat_id])

if __name__ == "__main__":
    print("Bot is starting...")
    poll_updates()
