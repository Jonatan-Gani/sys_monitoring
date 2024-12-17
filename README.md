Here’s a clean **README.md** content that you can easily copy and paste.

---

# **System Monitoring Script**

This script monitors system metrics such as CPU load, temperature, RAM usage, disk usage, network activity, and estimates power consumption for a Linux-based system (e.g., Raspberry Pi). It logs the data in a CSV file, archives logs daily, and sends Telegram alerts when thresholds are exceeded.

---

## **Features**

- **Metrics Monitored**:
  - CPU Load
  - System Temperature
  - RAM and Disk Usage
  - Network Sent/Received Data
  - Estimated Power Consumption (Watt-Hours)

- **Log Management**:
  - Logs system data to a CSV file (`power_log.csv`).
  - Archives logs daily into folders with the format `logs/log_archive/YYYY/Mon_MM/`.

- **Alerts**:
  - Sends Telegram notifications when metrics exceed configured thresholds.

- **Power Estimation**:
  - Calculates power consumed since the last execution and logs it as Watt-Hours (Wh).

---

## **Installation**

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/YOUR_GITHUB_USERNAME/sys_monitoring.git
   cd sys_monitoring
   ```

2. **Install Dependencies**:
   This script uses `psutil` and `requests`:
   ```bash
   sudo apt update
   sudo apt install python3-psutil python3-requests
   ```

3. **Create a `.env` File**:
   Add your Telegram Bot Token and Chat ID to a `.env` file:
   ```bash
   nano .env
   ```
   Add the following content:
   ```
   BOT_TOKEN=your_telegram_bot_token
   CHAT_ID=your_telegram_chat_id
   ```

4. **Configure Thresholds**:
   Edit the `config.json` file to set threshold values:
   ```json
   {
     "bot_token": "${BOT_TOKEN}",
     "chat_id": "${CHAT_ID}",
     "thresholds": {
       "cpu_load": 90.0,
       "temperature": 70.0,
       "power": 10.0,
       "ram_usage": 85.0,
       "disk_usage": 90.0
     }
   }
   ```

5. **Schedule the Script**:
   Use `cron` to run the script every minute:
   ```bash
   crontab -e
   ```
   Add the following line:
   ```
   * * * * * /usr/bin/python3 /path/to/log_pi_status.py
   ```

---

## **Logs**

- **Current Logs**:
  - `logs/power_log.csv`: Stores current system metrics.
- **Archived Logs**:
  - Logs are archived daily into the `logs/log_archive/YYYY/Mon_MM/` directory.

---

## **Usage**

Run the script manually:
```bash
python3 log_pi_status.py
```

To automate, ensure `cron` is running and scheduled as mentioned above.

---

## **Example Output (CSV)**

```
Timestamp,CPU Load (%),Temperature (°C),RAM Usage (%),Disk Usage (%),Network Sent (MB),Network Received (MB),Estimated Power (W),Interval Wh
2024-12-17 20:07:41,34.50,47.40,8.10,83.20,26425.18,17263.80,4.50,0.0750
2024-12-17 20:08:41,38.10,48.20,9.30,84.10,26427.18,17265.80,5.20,0.0800
```

---

## **Alerts**

The script sends alerts to the specified Telegram chat when thresholds are exceeded:
- **High CPU Load**: ⚠️ High CPU Load: 95.50%
- **High Temperature**: 🔥 High Temperature: 72.0°C
- **High Power Consumption**: ⚡ High Power Consumption: 15.0 W
- **High RAM or Disk Usage**: ⚠️ High RAM Usage: 87.0%

---

## **Directory Structure**

```
sys_monitoring/
│
├── log_pi_status.py         # Main script
├── config.json              # Configuration file
├── .env                     # Environment variables (Telegram API)
├── logs/
│   ├── power_log.csv        # Current CSV log
│   └── log_archive/         # Archived logs
│       ├── YYYY/
│       │   ├── Mon_MM/
│       │   │   ├── 17_Tuesday.csv
└── README.md                # This file
```

---

## **License**

This project is licensed under the MIT License. Feel free to use and modify it for your own purposes.

---

### **Contributing**

If you’d like to improve the script, submit a pull request or open an issue. Suggestions and bug reports are always welcome!

---

Let me know if you need further refinements! 🚀
