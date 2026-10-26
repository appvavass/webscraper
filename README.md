# Price Monitor

## Web dashboard

Install the dependencies, then start the dashboard locally:

```powershell
python -m pip install -r requirements.txt
python pricemonitor.py --web
```

Open <http://127.0.0.1:5000>. The dashboard reads the same SQLite database used by the command-line scraper. Use `--db PATH` to select a different database, or `--host` and `--port` to change the local address. Search for products directly from the page by keyword and optionally select one or more store categories; leaving categories unchecked searches all configured stores. Matching products are added to tracking. You can also paste a supported HTTPS product URL to track it directly.

Use **Scan now** on the dashboard to manually run a full scan. The scan rechecks saved product links and then searches every configured store using each distinct saved search term; tracked products added by URL use their product name as a search term. The same scan runs automatically once an hour while the web service is running. Price changes appear in the dashboard's Notifications section.

You can also trigger a full scan from a terminal:

```powershell
python pricemonitor.py --scan
```

The existing `--recheck` option only refreshes saved product URLs; it does not repeat broad store searches.

### Run on a cloud machine

Run the dashboard as a long-lived web service with a production WSGI server. On a Linux cloud machine, install the project and dependencies, then provide a persistent disk path for SQLite and start Gunicorn:

```sh
python -m pip install -r requirements.txt
export PRICE_MONITOR_DB=/path/on/persistent-disk/price_history.db
export PRICE_MONITOR_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
gunicorn --workers 1 --bind 0.0.0.0:${PORT:-8000} wsgi:app
```

Set `PRICE_MONITOR_SECRET_KEY` to a generated secret and keep it unchanged across restarts. Configure the cloud platform to restart the web process if it exits, route HTTPS traffic to its assigned port, and keep the directory containing `PRICE_MONITOR_DB` on persistent storage. The dashboard does not include user authentication; do not expose it publicly without putting it behind your cloud provider's access controls or another authentication layer. Tracking requests are limited to HTTPS URLs on recognized stores.

Keep Gunicorn at one worker: the hourly scheduler runs inside the web process and a multi-worker setup would start duplicate schedulers. The web service keeps the dashboard available, runs full scans every hour, and records a baseline when a product is submitted.

The dashboard's **Scan now** button queues a full scan immediately. For a manual terminal scan on the cloud machine, run `python /path/to/webscraper_python/pricemonitor.py --scan --db "$PRICE_MONITOR_DB"`. If using Telegram daily reports, `--daily` continues to recheck products and send the report.

## Daily Telegram price summary

1. Create a Telegram bot with BotFather and get its bot token.
2. Start a chat with the bot and obtain your chat ID.
3. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The `.env` file is excluded from Git; never put actual credentials in source files or commit them.
4. Test delivery with:

   ```powershell
   python pricemonitor.py --send-daily-report
   ```

The report aggregates observations recorded in the previous 24 hours per product, including the first and latest prices, the observed low, and the number of checks. The notification command reads `.env` from the project directory; variables already set in the process environment take precedence.

For a daily run that first fetches fresh prices for every tracked product, use:

```powershell
python pricemonitor.py --daily
```

To send this automatically each day on Windows, create a daily task in Task Scheduler:

- **Program/script:** the full path to your Python executable
- **Arguments:** `"C:\path\to\webscraper_python\pricemonitor.py" --daily`
- **Start in:** `C:\path\to\webscraper_python`

Set the task to run whether or not a user is logged on if appropriate, and choose a daily trigger time. The task's working directory should be the project directory so the existing database is selected. For another database, append `--db "C:\path\to\price_history.db"` to the arguments.
