Sys Monitoring

Sys Monitoring is a lightweight system monitoring tool designed for Raspberry Pi devices. It tracks and logs key performance metrics and sends alerts via Telegram when thresholds are exceeded. This tool is ideal for monitoring resource usage and ensuring system stability.
Features

    Comprehensive system monitoring:
        Tracks CPU load, temperature, RAM usage, disk usage, network activity, and estimated power consumption.
        Logs metrics in text and CSV formats for easy analysis.
    Alert system:
        Sends Telegram alerts for critical thresholds:
            CPU Load
            System Temperature
            RAM and Disk Usage
            Power Consumption
    Daily log management:
        Automatically archives logs by year and month.
        Clears daily logs to maintain fresh monitoring each day.
    Fully configurable:
        Thresholds and Telegram credentials are stored in an easily editable config.json.

Installation

    Clone the repository: git clone git@github.com:Jonatan-Gani/sys_monitoring.git cd sys_monitoring

    Install required dependencies: pip install psutil requests

    Create a config.json file in the root directory with the following structure: { "bot_token": "your_telegram_bot_token", "chat_id": "your_chat_id", "thresholds": { "cpu_load": 90.0, "temperature": 80.0, "power": 10.0, "ram_usage": 85.0, "disk_usage": 90.0 } }

    Test the script: python3 log_pi_status.py

    Schedule the script using cron for automated execution.

Usage

    Run manually: python3 log_pi_status.py

    Automate with cron: Add the script to your cron schedule for periodic execution (e.g., every 10 minutes). Open your crontab file: crontab -e Add the following line: */10 * * * * /usr/bin/python3 /path/to/log_pi_status.py

Logs

    Current logs:
        Text log: power_log.txt
        CSV log: power_log.csv
    Archived logs:
        Automatically stored in log_archive/year/month.

License

This project is licensed under the MIT License. See LICENSE for more details.
