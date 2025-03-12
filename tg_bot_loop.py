import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json
import signal
import sys
import math
import logging
from logging.handlers import RotatingFileHandler

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USERS = os.getenv("AUTHORIZED_USERS", "").split(",")  # Comma-separated list of authorized users
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOGS_DIRECTORY = "logs/log_archive"
BOT_LOGS_DIRECTORY = "logs/bot_logs"
LATEST_LOG_FILE = "logs/power_log.csv"
ITEMS_PER_PAGE = 5  # Number of items to show per page for pagination

# Create bot logs directory if it doesn't exist
os.makedirs(BOT_LOGS_DIRECTORY, exist_ok=True)

# Set up logging
log_file = os.path.join(BOT_LOGS_DIRECTORY, "telegram_bot.log")
logger = logging.getLogger("TelegramBot")
logger.setLevel(logging.INFO)

# Create handlers
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=10)  # 10MB per file, keep 10 backups
console_handler = logging.StreamHandler()

# Create formatters and add to handlers
log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(log_format)
console_handler.setFormatter(log_format)

# Add handlers to logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)


# Load configuration
def load_config():
    with open("config.json", "r") as config_file:
        return json.load(config_file)


CONFIG = load_config()
DEBUG_MODE = CONFIG.get("debug", False)


# Debug function
def debug_log(message):
    if DEBUG_MODE:
        logger.debug(message)


# Graceful termination
def signal_handler(sig, frame):
    logger.info("Bot shutting down gracefully...")
    print("\nExiting bot...")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


# Helper functions
def get_available_years():
    if not os.path.exists(LOGS_DIRECTORY):
        logger.warning(f"Logs directory not found: {LOGS_DIRECTORY}")
        return []
    return sorted([d for d in os.listdir(LOGS_DIRECTORY) if d.isdigit()])


def get_available_months(year):
    year_path = os.path.join(LOGS_DIRECTORY, year)
    if not os.path.exists(year_path):
        logger.warning(f"Year directory not found: {year_path}")
        return []
    months = []
    for folder in sorted(os.listdir(year_path)):
        if os.path.isdir(os.path.join(year_path, folder)):
            parts = folder.split("_")
            if len(parts) == 2:
                month_name, month_number = parts
                months.append(f"{month_name}\t{month_number.zfill(2)}")
    return months


def get_available_days(year, month):
    month_path = os.path.join(LOGS_DIRECTORY, year, month)
    if not os.path.exists(month_path):
        logger.warning(f"Month directory not found: {month_path}")
        return []
    days = []
    for file_name in os.listdir(month_path):
        if file_name.endswith(".csv"):
            parts = file_name.split("_")
            if len(parts) == 2:
                day, weekday = parts
                day_number = day.zfill(2)  # Ensure zero-padded day
                days.append(f"{weekday.split('.')[0]}\t{day_number}")
    return sorted(days, key=lambda x: int(x.split("\t")[1]))


def user_is_authorized(user_id):
    authorized = str(user_id) in AUTHORIZED_USERS
    if not authorized:
        logger.warning(f"Unauthorized access attempt by user ID: {user_id}")
    return authorized


def send_message(chat_id, text, reply_markup=None):
    logger.info(f"Sending message to chat ID {chat_id}: {text[:50]}..." if len(
        text) > 50 else f"Sending message to chat ID {chat_id}: {text}")
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
        if response.status_code != 200:
            logger.error(f"Failed to send message: {response.text}")
    except Exception as e:
        logger.error(f"Error sending message: {e}")


def send_document(chat_id, file_path):
    logger.info(f"Sending document to chat ID {chat_id}: {file_path}")
    try:
        with open(file_path, "rb") as file:
            response = requests.post(f"{TELEGRAM_API_URL}/sendDocument", files={"document": file},
                                     data={"chat_id": chat_id})
        if response.status_code != 200:
            logger.error(f"Failed to send document: {response.text}")
    except Exception as e:
        logger.error(f"Error sending document: {e}")


def create_keyboard(items, page=0, item_type=""):
    """Create an inline keyboard with items and pagination controls"""
    total_pages = math.ceil(len(items) / ITEMS_PER_PAGE)
    start_idx = page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(items))

    # Create buttons for items
    keyboard = []
    for item in items[start_idx:end_idx]:
        if "\t" in item:
            label, value = item.split("\t")
            keyboard.append([{"text": f"{label} ({value})", "callback_data": f"{item_type}:{value}"}])
        else:
            keyboard.append([{"text": item, "callback_data": f"{item_type}:{item}"}])

    # Add pagination controls if needed
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append({"text": "◀️ Previous", "callback_data": f"page:{item_type}:{page - 1}"})

        # Add page indicator
        nav_buttons.append({"text": f"Page {page + 1}/{total_pages}", "callback_data": "noop"})

        if page < total_pages - 1:
            nav_buttons.append({"text": "Next ▶️", "callback_data": f"page:{item_type}:{page + 1}"})

    # Add back button
    back_button = [{"text": "⬅️ Back", "callback_data": "back"}]

    # Add navigation row if we have pagination buttons
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Add back button in a new row
    keyboard.append(back_button)

    return {"inline_keyboard": keyboard}


def reset_session(chat_id, user_sessions):
    logger.info(f"Resetting session for chat ID {chat_id}")
    user_sessions[chat_id] = {"stage": "year", "last_active": datetime.now(), "page": 0}
    years = get_available_years()
    keyboard = create_keyboard(years, item_type="year")
    send_message(chat_id, "Session reset. Please select a year:", keyboard)


def handle_help_command(chat_id):
    logger.info(f"Help command requested by chat ID {chat_id}")
    help_text = (
        "📚 *Power Log Bot Help* 📚\n\n"
        "This bot allows you to retrieve power logs from the archive.\n\n"
        "*Available Commands:*\n"
        "/getlog - Start browsing logs by year, month, and day\n"
        "/help - Show this help message\n"
        "/latest - Get the most recent log file\n\n"
        "*Navigation:*\n"
        "• Use the inline buttons to navigate through years, months, and days\n"
        "• Use the ⬅️ Back button to go back to the previous level\n"
        "• Use the Previous/Next buttons to navigate through pages when there are many items\n\n"
        "*Session Timeout:*\n"
        "Your session will reset after 20 seconds of inactivity."
    )
    send_message(chat_id, help_text)


def handle_latest_command(chat_id, user_id):
    logger.info(f"Latest log requested by user ID {user_id} in chat ID {chat_id}")
    if not user_is_authorized(user_id):
        debug_log(f"Unauthorized access attempt by user {user_id}")
        send_message(chat_id, "Unauthorized user.")
        return

    if os.path.exists(LATEST_LOG_FILE):
        send_message(chat_id, "Fetching the latest power log...")
        send_document(chat_id, LATEST_LOG_FILE)
    else:
        logger.warning(f"Latest log file not found: {LATEST_LOG_FILE}")
        send_message(chat_id, "Latest log file not found.")


def handle_callback_query(callback_query, user_sessions):
    chat_id = callback_query["message"]["chat"]["id"]
    user_id = callback_query["from"]["id"]
    callback_data = callback_query["data"]
    message_id = callback_query["message"]["message_id"]

    logger.info(f"Callback query from user ID {user_id} in chat ID {chat_id}: {callback_data}")

    if not user_is_authorized(user_id):
        logger.warning(f"Unauthorized callback query from user {user_id}")
        requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery",
                      json={"callback_query_id": callback_query["id"], "text": "Unauthorized user."})
        return

    # Initialize or get session data
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {"stage": "year", "last_active": datetime.now(), "page": 0}

    user_data = user_sessions[chat_id]
    user_data["last_active"] = datetime.now()

    # Handle pagination callbacks
    if callback_data.startswith("page:"):
        parts = callback_data.split(":")
        item_type = parts[1]
        page = int(parts[2])
        user_data["page"] = page

        logger.info(f"Pagination request for {item_type}, page {page}")

        if item_type == "year":
            years = get_available_years()
            keyboard = create_keyboard(years, page, "year")
            edit_message(chat_id, message_id, "Please select a year:", keyboard)

        elif item_type == "month":
            months = get_available_months(user_data.get("year"))
            keyboard = create_keyboard(months, page, "month")
            edit_message(chat_id, message_id, "Please select a month:", keyboard)

        elif item_type == "day":
            days = get_available_days(user_data.get("year"), user_data.get("month"))
            keyboard = create_keyboard(days, page, "day")
            edit_message(chat_id, message_id, "Please select a day:", keyboard)

        return

    # Handle back button
    if callback_data == "back":
        logger.info(f"Back button pressed at stage {user_data['stage']}")

        if user_data["stage"] == "month":
            user_data["stage"] = "year"
            user_data["page"] = 0
            years = get_available_years()
            keyboard = create_keyboard(years, 0, "year")
            edit_message(chat_id, message_id, "Please select a year:", keyboard)

        elif user_data["stage"] == "day":
            user_data["stage"] = "month"
            user_data["page"] = 0
            months = get_available_months(user_data.get("year"))
            keyboard = create_keyboard(months, 0, "month")
            edit_message(chat_id, message_id, "Please select a month:", keyboard)

        return

    # Handle no-operation button (page indicator)
    if callback_data == "noop":
        requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery",
                      json={"callback_query_id": callback_query["id"]})
        return

    # Handle item selections
    parts = callback_data.split(":")
    if len(parts) == 2:
        item_type, value = parts

        if item_type == "year":
            logger.info(f"Year selected: {value}")
            user_data["year"] = value
            user_data["stage"] = "month"
            user_data["page"] = 0
            months = get_available_months(value)
            keyboard = create_keyboard(months, 0, "month")
            edit_message(chat_id, message_id, f"Year: {value}\nPlease select a month:", keyboard)

        elif item_type == "month":
            year = user_data.get("year")
            months = get_available_months(year)
            month_number = value.zfill(2)
            selected_month = [m for m in months if m.endswith(f"\t{month_number}")][0]
            month_name = selected_month.split("\t")[0]
            logger.info(f"Month selected: {month_name} ({month_number})")

            user_data["month"] = f"{month_name}_{month_number}"
            user_data["stage"] = "day"
            user_data["page"] = 0
            days = get_available_days(year, user_data["month"])
            keyboard = create_keyboard(days, 0, "day")
            edit_message(chat_id, message_id, f"Year: {year}, Month: {month_name}\nPlease select a day:", keyboard)

        elif item_type == "day":
            year, month = user_data.get("year"), user_data.get("month")
            days = get_available_days(year, month)
            day_number = str(int(value))
            weekday = [d.split("\t")[0] for d in days if d.endswith(f"\t{day_number}")][0]
            logger.info(f"Day selected: {day_number} ({weekday})")

            log_path = os.path.join(LOGS_DIRECTORY, year, month, f"{day_number}_{weekday}.csv")

            # Answer the callback query
            requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery",
                          json={"callback_query_id": callback_query["id"]})

            if os.path.exists(log_path):
                logger.info(f"Sending log file: {log_path}")
                send_message(chat_id, f"Fetching log for {year}/{month.replace('_', ' ')}/{day_number} ({weekday})...")
                send_document(chat_id, log_path)

                # Reset to initial state after sending the log
                user_data["stage"] = "year"
                user_data["page"] = 0
                years = get_available_years()
                keyboard = create_keyboard(years, 0, "year")
                send_message(chat_id, "Select another year or use /getlog to start over:", keyboard)
            else:
                logger.warning(f"Log file not found: {log_path}")
                send_message(chat_id, f"Log not found for the specified date; "
                                      f"{year}, {month}, {day_number}/{weekday}")


def edit_message(chat_id, message_id, text, reply_markup=None):
    logger.info(f"Editing message {message_id} in chat {chat_id}")
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(f"{TELEGRAM_API_URL}/editMessageText", json=payload)
        if response.status_code != 200:
            logger.error(f"Failed to edit message: {response.text}")
    except Exception as e:
        logger.error(f"Error editing message: {e}")


def handle_user_input(chat_id, user_id, text, user_sessions):
    logger.info(f"Text input from user ID {user_id} in chat ID {chat_id}: {text}")

    if not user_is_authorized(user_id):
        debug_log(f"Unauthorized access attempt by user {user_id}")
        send_message(chat_id, "Unauthorized user.")
        return

    # Handle commands
    if text.lower() == "/getlog":
        logger.info(f"GetLog command initiated by user ID {user_id}")
        reset_session(chat_id, user_sessions)
        return
    elif text.lower() == "/help":
        handle_help_command(chat_id)
        return
    elif text.lower() == "/latest":
        handle_latest_command(chat_id, user_id)
        return

    # For text-based navigation (fallback if inline buttons don't work)
    user_data = user_sessions.get(chat_id, {})
    stage = user_data.get("stage", "year")

    # Update last active time
    user_data["last_active"] = datetime.now()

    if text.lower() == "back":
        logger.info(f"Text 'back' command at stage {stage}")
        if stage == "month":
            user_data["stage"] = "year"
            years = get_available_years()
            keyboard = create_keyboard(years, 0, "year")
            send_message(chat_id, "Please select a year:", keyboard)
        elif stage == "day":
            user_data["stage"] = "month"
            months = get_available_months(user_data.get("year"))
            keyboard = create_keyboard(months, 0, "month")
            send_message(chat_id, "Please select a month:", keyboard)
        return

    if stage == "year":
        years = get_available_years()
        if text in years:
            logger.info(f"Year selected via text: {text}")
            user_data["year"] = text
            user_data["stage"] = "month"
            months = get_available_months(text)
            keyboard = create_keyboard(months, 0, "month")
            send_message(chat_id, f"Year: {text}\nPlease select a month:", keyboard)
        else:
            logger.warning(f"Invalid year entered: {text}")
            keyboard = create_keyboard(years, 0, "year")
            send_message(chat_id, f"Invalid year. Please select a year:", keyboard)

    elif stage == "month":
        year = user_data.get("year")
        months = get_available_months(year)
        month_numbers = [m.split("\t")[1] for m in months]
        if text.zfill(2) in month_numbers:
            selected_month = [m for m in months if m.endswith(f"\t{text.zfill(2)}")][0]
            month_name = selected_month.split("\t")[0]
            logger.info(f"Month selected via text: {month_name} ({text.zfill(2)})")

            user_data["month"] = f"{month_name}_{text.zfill(2)}"
            user_data["stage"] = "day"
            days = get_available_days(year, user_data["month"])
            keyboard = create_keyboard(days, 0, "day")
            send_message(chat_id, f"Year: {year}, Month: {month_name}\nPlease select a day:", keyboard)
        else:
            logger.warning(f"Invalid month entered: {text}")
            keyboard = create_keyboard(months, 0, "month")
            send_message(chat_id, f"Invalid month. Please select a month:", keyboard)

    elif stage == "day":
        year, month = user_data.get("year"), user_data.get("month")
        days = get_available_days(year, month)
        day_numbers = [d.split("\t")[1] for d in days]
        if text.zfill(2) in day_numbers:
            day_number = text.zfill(2)
            weekday = [d.split("\t")[0] for d in days if d.endswith(f"\t{day_number}")][0]
            logger.info(f"Day selected via text: {day_number} ({weekday})")

            log_path = os.path.join(LOGS_DIRECTORY, year, month, f"{day_number}_{weekday}.csv")
            if os.path.exists(log_path):
                logger.info(f"Sending log file: {log_path}")
                send_message(chat_id, f"Fetching log for {year}/{month.replace('_', ' ')}/{day_number} ({weekday})...")
                send_document(chat_id, log_path)

                # Reset to initial state after sending the log
                user_data["stage"] = "year"
                years = get_available_years()
                keyboard = create_keyboard(years, 0, "year")
                send_message(chat_id, "Select another year or use /getlog to start over:", keyboard)
            else:
                logger.warning(f"Log file not found: {log_path}")
                send_message(chat_id, "Log not found for the specified date.")
        else:
            logger.warning(f"Invalid day entered: {text}")
            keyboard = create_keyboard(days, 0, "day")
            send_message(chat_id, f"Invalid day. Please select a day:", keyboard)


def poll_updates():
    offset = None
    user_sessions = {}

    logger.info("Starting bot polling loop")

    # Log bot startup with version and configuration
    logger.info(f"===== POWER LOG BOT STARTED =====")
    logger.info(f"Debug mode: {DEBUG_MODE}")
    logger.info(f"Authorized users: {len(AUTHORIZED_USERS)}")
    logger.info(f"Logs directory: {LOGS_DIRECTORY}")
    logger.info(f"Bot logs directory: {BOT_LOGS_DIRECTORY}")

    # Check if logs directories exist
    if not os.path.exists(LOGS_DIRECTORY):
        logger.warning(f"Logs directory does not exist: {LOGS_DIRECTORY}")

    # Log available years at startup
    years = get_available_years()
    logger.info(f"Available years: {', '.join(years) if years else 'None'}")

    while True:
        try:
            response = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params={"offset": offset, "timeout": 30}).json()

            for update in response.get("result", []):
                offset = update["update_id"] + 1

                # Handle callback queries (for inline keyboard buttons)
                if "callback_query" in update:
                    handle_callback_query(update["callback_query"], user_sessions)
                    continue

                # Handle regular messages
                if "message" in update and "text" in update["message"]:
                    chat_id = update["message"]["chat"]["id"]
                    user_id = update["message"]["from"]["id"]
                    text = update["message"]["text"]

                    logger.info(f"Received message from user ID {user_id} in chat ID {chat_id}: {text}")

                    # Reset session after 20 seconds of inactivity
                    if chat_id in user_sessions:
                        last_active = user_sessions[chat_id].get("last_active")
                        if last_active and datetime.now() - last_active > timedelta(seconds=20):
                            logger.info(f"Session timeout for chat ID {chat_id}")
                            reset_session(chat_id, user_sessions)
                            continue

                    if chat_id not in user_sessions:
                        logger.info(f"New session created for chat ID {chat_id}")
                        user_sessions[chat_id] = {"stage": "year", "last_active": datetime.now(), "page": 0}

                    handle_user_input(chat_id, user_id, text, user_sessions)

        except KeyboardInterrupt:
            logger.info("Bot shutting down due to keyboard interrupt")
            print("\nGracefully shutting down...")
            break
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error: {e}")
            # Add a short sleep to prevent tight looping on network errors
            time.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            # Add a short sleep to prevent tight looping on errors
            time.sleep(1)


if __name__ == "__main__":
    print("Bot is starting...")
    logger.info("===== INITIALIZING POWER LOG BOT =====")
    poll_updates()