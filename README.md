# Price Monitor

## Local web dashboard

Install the dependencies, then start the dashboard from the project directory:

```powershell
python -m pip install -r requirements.txt
python pricemonitor.py --web
```

Open <http://127.0.0.1:5000>. The dashboard reads the same SQLite database used by the command-line scraper. Use `--db PATH` to select a different database, or `--host` and `--port` to change the local address. The dashboard has no login, so keep it bound to `127.0.0.1` and do not expose it to an untrusted network.

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
