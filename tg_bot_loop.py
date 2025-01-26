import asyncio
import json
import os
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USERS = os.getenv("AUTHORIZED_USERS", "").split(",")  # Comma-separated list of authorized users
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

async def send_delayed_message(context, chat_id, message):
    await asyncio.sleep(1)
    await context.bot.send_message(chat_id=chat_id, text=message)

# Handlers
async def start(update, context):
    if not user_is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized user.")
        return
    print(f"Authorized user {update.effective_user.id} started interaction.")
    await send_delayed_message(context, update.effective_chat.id, "Welcome! Use /getlog to request logs.")

async def get_log(update, context):
    if not user_is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized user.")
        return

    print(f"User {update.effective_user.id} initiated log retrieval.")
    context.user_data.clear()
    context.user_data['stage'] = 'year'

    years = get_available_years()
    if years:
        await send_delayed_message(context, update.effective_chat.id, f"Available years: {', '.join(years)}\nEnter the year:")
    else:
        await send_delayed_message(context, update.effective_chat.id, "No logs available.")

async def message_handler(update, context):
    if not user_is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized user.")
        return

    user_input = update.message.text.strip()
    stage = context.user_data.get('stage')

    if stage == 'year':
        years = get_available_years()
        if user_input in years:
            context.user_data['year'] = user_input
            context.user_data['stage'] = 'month'

            months = get_available_months(user_input)
            await send_delayed_message(context, update.effective_chat.id, f"Available months: {', '.join(months)}\nEnter the month (e.g., Mon_12):")
        else:
            await send_delayed_message(context, update.effective_chat.id, f"Invalid year. Available years: {', '.join(years)}")

    elif stage == 'month':
        year = context.user_data.get('year')
        months = get_available_months(year)
        if user_input in months:
            context.user_data['month'] = user_input
            context.user_data['stage'] = 'day'

            days = get_available_days(year, user_input)
            await send_delayed_message(context, update.effective_chat.id, f"Available days: {', '.join(days)}\nEnter the day:")
        else:
            await send_delayed_message(context, update.effective_chat.id, f"Invalid month. Available months: {', '.join(months)}")

    elif stage == 'day':
        year, month = context.user_data.get('year'), context.user_data.get('month')
        days = get_available_days(year, month)
        if user_input in days:
            log_path = os.path.join(LOGS_DIRECTORY, year, month, f"{user_input}_{datetime.strptime(user_input, '%d').strftime('%A')}.csv")
            if os.path.exists(log_path):
                await send_delayed_message(context, update.effective_chat.id, "Fetching the log...")
                await context.bot.send_document(chat_id=update.effective_chat.id, document=open(log_path, "rb"))
                print(f"Log for {year}-{month}-{user_input} sent to user {update.effective_user.id}.")
            else:
                await send_delayed_message(context, update.effective_chat.id, "Log not found for the specified date.")
        else:
            await send_delayed_message(context, update.effective_chat.id, f"Invalid day. Available days: {', '.join(days)}")

# Main function
async def main():
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getlog", get_log))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("Bot is starting...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
