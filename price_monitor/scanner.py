from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .extraction import scrape_product
from .models import load_stores, log
from .query import parse_query
from .search import search_store
from .storage import init_db, save_price


def run_hourly_scan(db_path: str, stores_path: str) -> str:
    conn = init_db(db_path)
    started_at = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE scan_status
           SET status = 'running', started_at = ?, completed_at = NULL,
               summary = NULL, error = NULL
         WHERE id = 1
        """,
        (started_at,),
    )
    conn.commit()

    checked = 0
    searches = 0
    results = 0
    failures = 0
    try:
        tracked = conn.execute(
            "SELECT product_id, url, store FROM tracked_products ORDER BY product_id"
        ).fetchall()
        for product_id, url, store_name in tracked:
            try:
                product = scrape_product(url, store_name or "")
                product.product_id = product_id
                save_price(conn, product, None)
                checked += 1
            except Exception as exc:
                failures += 1
                log.warning("[%s] Recheck failed for %s: %s", store_name, url, exc)

        tracked_queries = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(search_query), ''), NULLIF(TRIM(name), ''))
              FROM tracked_products
             WHERE COALESCE(NULLIF(TRIM(search_query), ''), NULLIF(TRIM(name), '')) IS NOT NULL
             ORDER BY 1 COLLATE NOCASE
            """
        ).fetchall()
        queries: dict[str, str] = {}
        for (text,) in tracked_queries:
            queries.setdefault(text.casefold(), text)

        stores = load_stores(Path(stores_path))
        for query_text in queries.values():
            try:
                query = parse_query(query_text)
            except ValueError as exc:
                failures += 1
                log.warning("Skipping saved search %r: %s", query_text, exc)
                continue

            for store in stores:
                searches += 1
                try:
                    products = search_store(store, query, max_results=3)
                except Exception as exc:
                    failures += 1
                    log.warning("[%s] Saved search %r failed: %s", store.name, query_text, exc)
                    continue

                for product in products:
                    save_price(conn, product, query.raw)
                    results += 1

        summary = (
            f"Checked {checked} tracked products; searched {len(queries)} saved terms "
            f"across {searches} store searches; found {results} results; "
            f"{failures} failures."
        )
        conn.execute(
            """
            UPDATE scan_status
               SET status = 'complete', completed_at = ?, summary = ?, error = NULL
             WHERE id = 1
            """,
            (datetime.now().isoformat(timespec="seconds"), summary),
        )
        conn.commit()
        log.info("Hourly scan complete: %s", summary)
        return summary
    except Exception as exc:
        conn.execute(
            """
            UPDATE scan_status
               SET status = 'failed', completed_at = ?, error = ?
             WHERE id = 1
            """,
            (datetime.now().isoformat(timespec="seconds"), str(exc)),
        )
        conn.commit()
        log.exception("Hourly scan failed")
        raise
    finally:
        conn.close()


def reset_interrupted_scan(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE scan_status
           SET status = 'idle', summary = NULL,
               error = 'Previous scan was interrupted by a service restart'
         WHERE id = 1 AND status = 'running'
        """
    )
    conn.commit()
