from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from .extraction import scrape_product
from .fetch import _ROBOTS, allowed_by_robots
from .models import Product, STORES, log
from .query import fmt_sek


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            product_name TEXT,
            price REAL NOT NULL,
            scanned_at TEXT NOT NULL,
            product_id TEXT,
            store TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_products (
            product_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            store TEXT,
            name TEXT,
            search_query TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            product_name TEXT,
            store TEXT,
            url TEXT NOT NULL,
            previous_price REAL NOT NULL,
            new_price REAL NOT NULL,
            changed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            summary TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scan_status (id, status)
        VALUES (1, 'idle')
        """
    )
    conn.commit()
    return conn


def get_price_notifications(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, product_id, product_name, store, url,
               previous_price, new_price, changed_at
          FROM price_notifications
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "product_id": row[1],
            "product_name": row[2],
            "store": row[3],
            "url": row[4],
            "previous_price": row[5],
            "new_price": row[6],
            "changed_at": row[7],
        }
        for row in rows
    ]


def get_daily_summary(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[dict]:
    current_time = now or datetime.now()
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone().replace(tzinfo=None)
    cutoff = (current_time - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT product_id, url, product_name, store, price, scanned_at
          FROM price_history
         WHERE scanned_at >= ?
         ORDER BY scanned_at, id
        """,
        (cutoff,),
    ).fetchall()

    summary: dict[str, dict] = {}
    for product_id, url, name, store, price, scanned_at in rows:
        key = product_id or url
        product = summary.get(key)
        if product is None:
            product = {
                "product_id": product_id,
                "url": url,
                "name": name or url,
                "store": store or "Unknown store",
                "observations": 0,
                "first_price": price,
                "latest_price": price,
                "lowest_price": price,
                "highest_price": price,
                "first_seen": scanned_at,
                "latest_seen": scanned_at,
            }
            summary[key] = product

        product["observations"] += 1
        product["latest_price"] = price
        product["latest_seen"] = scanned_at
        product["lowest_price"] = min(product["lowest_price"], price)
        product["highest_price"] = max(product["highest_price"], price)

    return sorted(summary.values(), key=lambda item: (item["store"].casefold(), item["name"].casefold()))


def format_daily_report(products: list[dict]) -> str:
    observation_count = sum(product["observations"] for product in products)
    lines = [
        "Price monitor — last 24 hours",
        f"{observation_count} price observations across {len(products)} products.",
    ]
    if not products:
        lines.append("No price data was collected in this period.")
        return "\n".join(lines)

    for product in products:
        movement = ""
        if product["latest_price"] < product["first_price"]:
            movement = " ↓"
        elif product["latest_price"] > product["first_price"]:
            movement = " ↑"
        lines.extend(
            [
                "",
                f"{product['store']} — {product['name']}",
                (
                    f"{fmt_sek(product['first_price'])} → {fmt_sek(product['latest_price'])}"
                    f"{movement} | low {fmt_sek(product['lowest_price'])}"
                    f" | {product['observations']} check(s)"
                ),
                product["url"],
            ]
        )
    return "\n".join(lines)


def get_previous_and_lowest(conn: sqlite3.Connection, product: Product):
    where = "product_id = ? OR url = ?"
    args = (product.product_id, product.url)
    prev = conn.execute(f"SELECT price FROM price_history WHERE {where} ORDER BY id DESC LIMIT 1", args).fetchone()
    low = conn.execute(f"SELECT MIN(price) FROM price_history WHERE {where}", args).fetchone()
    return (prev[0] if prev else None), (low[0] if low else None)


def save_price(conn: sqlite3.Connection, product: Product, query_text: Optional[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    previous = conn.execute(
        """
        SELECT price FROM price_history
         WHERE product_id = ? OR url = ?
         ORDER BY id DESC LIMIT 1
        """,
        (product.product_id, product.url),
    ).fetchone()
    conn.execute(
        "INSERT INTO price_history (url, product_name, price, scanned_at, product_id, store) VALUES (?, ?, ?, ?, ?, ?)",
        (product.url, product.name, product.price, now, product.product_id, product.store),
    )
    if previous is not None and previous[0] != product.price:
        conn.execute(
            """
            INSERT INTO price_notifications
                (product_id, product_name, store, url, previous_price, new_price, changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product.product_id,
                product.name,
                product.store,
                product.url,
                previous[0],
                product.price,
                now,
            ),
        )
    conn.execute(
        """
        INSERT INTO tracked_products (product_id, url, store, name, search_query, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            url = excluded.url, name = excluded.name, last_seen = excluded.last_seen
        """,
        (product.product_id, product.url, product.store, product.name, query_text, now, now),
    )
    conn.commit()


def report_price_change(p: Product, previous: Optional[float], lowest: Optional[float]) -> None:
    tag = f"[{p.store}] {p.name}"
    if previous is None:
        print(f"ℹ️  New product tracked: {tag}\n   {fmt_sek(p.price)} (baseline saved)\n   {p.url}")
    elif p.price < previous:
        diff = previous - p.price
        extra = "\n   ⭐ Lowest price recorded so far!" if lowest is not None and p.price <= lowest else ""
        print(f"🔻 PRICE DROP! {tag}\n   {fmt_sek(previous)} -> {fmt_sek(p.price)} (-{fmt_sek(diff)}){extra}\n   {p.url}")
    elif p.price > previous:
        diff = p.price - previous
        print(f"🔺 Price increase: {tag}\n   {fmt_sek(previous)} -> {fmt_sek(p.price)} (+{fmt_sek(diff)})\n   {p.url}")
    else:
        print(f"➖ No change: {tag} - {fmt_sek(p.price)}\n   {p.url}")


def handle_product(conn: sqlite3.Connection, product: Product, query_text: Optional[str]) -> None:
    previous, lowest = get_previous_and_lowest(conn, product)
    report_price_change(product, previous, lowest)
    save_price(conn, product, query_text)


def list_tracked(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT t.store, t.name, t.url,
               (SELECT price FROM price_history h WHERE h.product_id = t.product_id ORDER BY h.id DESC LIMIT 1),
               (SELECT MIN(price) FROM price_history h WHERE h.product_id = t.product_id)
          FROM tracked_products t ORDER BY t.store, t.name
        """
    ).fetchall()
    if not rows:
        print("Nothing tracked yet.")
        return
    for store, name, url, last, low in rows:
        print(f"[{store}] {name}\n   now {fmt_sek(last)} | lowest {fmt_sek(low)}\n   {url}")


def run_recheck(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT product_id, url, store, search_query FROM tracked_products").fetchall()
    if not rows:
        print("Nothing tracked yet to recheck.")
        return
    for pid, url, store_name, query_text in rows:
        try:
            product = scrape_product(url, store_name)
            product.product_id = pid
            handle_product(conn, product, query_text)
        except Exception as exc:
            log.error("[%s] %s: %s", store_name, url, exc)


def check_stores() -> None:
    for store in STORES:
        base = f"https://{store.domain}"
        allowed_by_robots(base + "/")
        parser = _ROBOTS.get(base)
        maps = parser.site_maps() if parser else None
        print(f"  {store.name:<18} sitemaps: {len(maps) if maps else 0}")
