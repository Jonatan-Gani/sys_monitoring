import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json
import signal
import sys
import time

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USERS = os.getenv("AUTHORIZED_USERS", "").split(",")  # Comma-separated list of authorized users
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOGS_DIRECTORY = "logs/log_archive"
LATEST_LOG_FILE = "logs/power_log.csv"

# Load configuration
def load_config():
    with open("config.json", "r") as config_file:
        return json.load(config_file)

CONFIG = load_config()
DEBUG_MODE = CONFIG.get("debug", False)

# Debug function
def debug_log(message):
    if DEBUG_MODE:
        print(message)

# Graceful termination
def signal_handler(sig, frame):
    print("\nExiting bot...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# Helper functions
def get_available_years():
    if not os.path.exists(LOGS_DIRECTORY):
        return []
    return sorted([d for d in os.listdir(LOGS_DIRECTORY) if d.isdigit()])

def get_available_months(year):
    year_path = os.path.join(LOGS_DIRECTORY, year)
    if not os.path.exists(year_path):
        return []
    months = []
    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    for folder in sorted(os.listdir(year_path)):
        if os.path.isdir(os.path.join(year_path, folder)):
            month_number = folder.split("_")[-1].zfill(2)
            month_name = month_names[int(month_number) - 1]
            months.append(f"{month_name}\t{month_number}")
    return months

def get_available_days(year, month):
    month_path = os.path.join(LOGS_DIRECTORY, year, month)
    if not os.path.exists(month_path):
        return []
    days = []
    for file_name in os.listdir(month_path):
        if file_name.endswith(".csv"):
            day, weekday = file_name.split("_")
            day_number = day.zfill(2)  # Ensure zero-padded day
            days.append(f"{weekday.split('.')[0]}\t{day_number}")
    return sorted(days, key=lambda x: int(x.split('\t')[1]))

def user_is_authorized(user_id):
    return str(user_id) in AUTHORIZED_USERS

def send_message(chat_id, text):
    debug_log(f"Sending message to {chat_id}: {text}")
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})

def send_document(chat_id, file_path):
    debug_log(f"Sending document to {chat_id}: {file_path}")
    with open(file_path, "rb") as file:
        requests.post(f"{TELEGRAM_API_URL}/sendDocument", files={"document": file}, data={"chat_id": chat_id})

def reset_session(chat_id, user_sessions):
    debug_log(f"Resetting session for {chat_id}")
    user_sessions[chat_id] = {"stage": "year", "last_active": datetime.now()}
    years = get_available_years()
    send_message(chat_id, f"Session reset due to inactivity. Available years:\n{', '.join(years)}\nEnter the year:")

def handle_user_input(chat_id, user_id, text, user_sessions):
    if not user_is_authorized(user_id):
        debug_log(f"Unauthorized access attempt by user {user_id}")
        send_message(chat_id, "Unauthorized user.")
        return

    user_data = user_sessions.get(chat_id, {})
    stage = user_data.get("stage", "year")

    # Update last active time
    user_data["last_active"] = datetime.now()

    if text.lower() == "back":
        if stage == "month":
            user_data["stage"] = "year"
            years = get_available_years()
            send_message(chat_id, f"Available years:\n{', '.join(years)}\nEnter the year:")
        elif stage == "day":
            user_data["stage"] = "month"
            months = get_available_months(user_data.get("year"))
            send_message(chat_id, f"Available months:\n{chr(10).join(months)}\nEnter the month number (e.g., 12):")
        return

    if stage == "year":
        years = get_available_years()
        if text in years:
            user_data["year"] = text
            user_data["stage"] = "month"
            months = get_available_months(text)
            send_message(chat_id, f"Available months:\n{chr(10).join(months)}\nEnter the month number (e.g., 12). Type 'back' to go back:")
        else:
            send_message(chat_id, f"Invalid year. Available years:\n{', '.join(years)}")

    elif stage == "month":
        year = user_data.get("year")
        months = get_available_months(year)
        month_numbers = [m.split('\t')[1] for m in months]
        if text.zfill(2) in month_numbers:
            month_name = [m.split('\t')[0] for m in months if m.endswith(f"\t{text.zfill(2)}")][0]
            user_data["month"] = f"Mon_{text.zfill(2)}"
            user_data["stage"] = "day"
            days = get_available_days(year, user_data["month"])
            send_message(chat_id, f"Available days:\n{chr(10).join(days)}\nEnter the day number (e.g., 11). Type 'back' to go back:")
        else:
            send_message(chat_id, f"Invalid month. Available months:\n{chr(10).join(months)}")

    elif stage == "day":
        year, month = user_data.get("year"), user_data.get("month")
        days = get_available_days(year, month)
        day_numbers = [d.split('\t')[1] for d in days]
        if text.zfill(2) in day_numbers:
            day_number = text.zfill(2)
            weekday = [d.split('\t')[0] for d in days if d.endswith(f"\t{day_number}")][0]
            log_path = os.path.join(LOGS_DIRECTORY, year, month, f"{day_number}_{weekday}.csv")
            if os.path.exists(log_path):
                send_message(chat_id, "Fetching the log...")
                send_document(chat_id, log_path)
            else:
                send_message(chat_id, "Log not found for the specified date.")
        else:
            send_message(chat_id, f"Invalid day. Available days:\n{chr(10).join(days)}")

def poll_updates():
    offset = None
    user_sessions = {}

    while True:
        try:
            response = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params={"offset": offset, "timeout": 30}).json()

            for update in response.get("result", []):
                offset = update["update_id"] + 1
                chat_id = update["message"]["chat"]["id"]
                user_id = update["message"]["from"]["id"]
                text = update["message"].get("text", "")

                debug_log(f"Received message from {user_id}: {text}")

                # Reset session after 20 seconds of inactivity
                if chat_id in user_sessions:
                    last_active = user_sessions[chat_id].get("last_active")
                    if last_active and datetime.now() - last_active > timedelta(seconds=20):
                        reset_session(chat_id, user_sessions)
                        continue

                if chat_id not in user_sessions:
                    user_sessions[chat_id] = {"stage": "year", "last_active": datetime.now()}

                if text.lower() == "/getlog":
                    reset_session(chat_id, user_sessions)
                else:
                    handle_user_input(chat_id, user_id, text, user_sessions)
        except KeyboardInterrupt:
            print("\nGracefully shutting down...")
            break
        except Exception as e:
            debug_log(f"Error: {e}")

if __name__ == "__main__":
    print("Bot is starting...")
    poll_updates()
