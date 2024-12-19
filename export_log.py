import os
import requests
import datetime
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Telegram bot credentials
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("BOT_TOKEN or CHAT_ID not found in .env file!")

# Paths
BASE_DIR = os.path.expanduser("~/sys_monitoring")
LOG_DIR = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOG_DIR, "log_archive")
CURRENT_FILE = os.path.join(LOG_DIR, "power_log.csv")


def send_file_to_telegram(file_path):
    """Send the selected file to the Telegram bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(file_path, "rb") as file:
        response = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID},
            files={"document": (os.path.basename(file_path), file)},
        )
    if response.status_code == 200:
        print(f"File '{file_path}' sent successfully!")
    else:
        print(f"Failed to send file. Error: {response.text}")


def select_archived_file(year):
    """Select an archived file based on year, month, and day."""
    year_path = os.path.join(ARCHIVE_DIR, year)
    if not os.path.exists(year_path):
        print(f"No archive found for year {year}. Exiting.")
        return None

    # Ask for month
    month = input("Enter the month number (e.g., 12 for December): ").strip().zfill(2)
    month_abbr = datetime.datetime.strptime(month, "%m").strftime("%b")
    month_folder = f"{month_abbr}_{month}"
    month_path = os.path.join(year_path, month_folder)
    if not os.path.exists(month_path):
        print(f"No archive found for month {month} in year {year}. Exiting.")
        return None

    # Ask for day
    day = input("Enter the day number (e.g., 15): ").strip().zfill(2)
    day_file_prefix = f"{int(day)}_"
    available_files = [
        f for f in os.listdir(month_path) if f.startswith(day_file_prefix) and f.endswith(".csv")
    ]
    if not available_files:
        print(f"No file found for day {day} in month {month}, year {year}. Exiting.")
        return None

    # If multiple files match, select the first one
    selected_file = os.path.join(month_path, available_files[0])
    return selected_file


def main():
    print("Enter 'C' to send the current log file or the year to browse archives.")
    choice = input("Enter your choice (C or year): ").strip().upper()

    if choice == "C":
        if os.path.exists(CURRENT_FILE):
            send_file_to_telegram(CURRENT_FILE)
        else:
            print(f"Current file '{CURRENT_FILE}' does not exist.")
    else:
        # Assume the user entered a year
        if not choice.isdigit() or len(choice) != 4:
            print("Invalid input. Please enter 'C' or a valid 4-digit year.")
            return

        selected_file = select_archived_file(choice)
        if selected_file:
            send_file_to_telegram(selected_file)


if __name__ == "__main__":
    main()
