from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .storage import format_daily_report, get_daily_summary


class NotificationError(Exception):
    """A report could not be delivered."""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env entry on line {line_number}.")

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def send_telegram_message(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise NotificationError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment or .env file."
        )

    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for character in message:
        units = len(character.encode("utf-16-le")) // 2
        if current_units + units > 3500:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += units
    if current:
        chunks.append("".join(current))
    if not chunks:
        chunks.append("")

    for chunk in chunks:
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": chunk}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise NotificationError(f"Telegram returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise NotificationError("Could not connect to Telegram.") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NotificationError("Telegram returned an unreadable response.") from None

        if not isinstance(result, dict):
            raise NotificationError("Telegram returned an unreadable response.")
        if not result.get("ok"):
            raise NotificationError("Telegram did not accept the report.")


def send_daily_report(conn: sqlite3.Connection) -> int:
    report = format_daily_report(get_daily_summary(conn))
    send_telegram_message(report)
    return len(report)
