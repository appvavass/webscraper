from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from .extraction import scrape_product
from .models import load_stores, log, store_for_url
from .query import parse_query
from .scanner import reset_interrupted_scan, run_hourly_scan
from .search import search_store
from .storage import get_daily_summary, get_price_notifications, init_db, save_price


def create_app(
    db_path: str,
    start_scheduler: bool = True,
    stores_path: str | Path | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("PRICE_MONITOR_SECRET_KEY") or secrets.token_hex(32)
    app.config["DATABASE"] = db_path
    app.config["STORES_FILE"] = (
        Path(stores_path)
        if stores_path is not None
        else Path(__file__).resolve().parent.parent / "stores.txt"
    )
    init_db(db_path).close()
    status_connection = sqlite3.connect(db_path)
    try:
        reset_interrupted_scan(status_connection)
    finally:
        status_connection.close()

    scheduler = None
    if start_scheduler:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            run_hourly_scan,
            "interval",
            hours=1,
            args=[db_path, str(app.config["STORES_FILE"])],
            id="hourly-price-scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        scheduler.start()
    app.extensions["price_monitor_scheduler"] = scheduler

    def database() -> sqlite3.Connection:
        if "database" not in g:
            g.database = sqlite3.connect(app.config["DATABASE"])
            g.database.row_factory = sqlite3.Row
        return g.database

    @app.teardown_appcontext
    def close_database(_error: BaseException | None) -> None:
        connection = g.pop("database", None)
        if connection is not None:
            connection.close()

    @app.get("/")
    def index():
        query = request.args.get("q", "").strip()
        store = request.args.get("store", "").strip()
        selected_categories = request.args.getlist("category")
        clauses = []
        values: list[str] = []
        if query:
            clauses.append("(COALESCE(t.name, '') LIKE ? OR COALESCE(t.url, '') LIKE ?)")
            values.extend((f"%{query}%", f"%{query}%"))
        if store:
            clauses.append("COALESCE(t.store, '') = ?")
            values.append(store)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = database().execute(
            f"""
            SELECT t.product_id, t.store, t.name, t.url,
                   (SELECT h.price FROM price_history h
                     WHERE h.product_id = t.product_id
                     ORDER BY h.id DESC LIMIT 1) AS latest_price,
                   (SELECT MIN(h.price) FROM price_history h
                     WHERE h.product_id = t.product_id) AS lowest_price,
                   COUNT(history.id) AS observation_count,
                   MAX(history.scanned_at) AS latest_seen
              FROM tracked_products t
              LEFT JOIN price_history history ON history.product_id = t.product_id
              {where}
             GROUP BY t.product_id
             ORDER BY t.store COLLATE NOCASE, t.name COLLATE NOCASE
            """,
            values,
        ).fetchall()
        stores = [
            row["store"]
            for row in database().execute(
                "SELECT DISTINCT store FROM tracked_products WHERE store IS NOT NULL ORDER BY store COLLATE NOCASE"
            ).fetchall()
        ]
        recent = get_daily_summary(database())
        notifications = get_price_notifications(database())
        scan_status = database().execute(
            "SELECT status, started_at, completed_at, summary, error FROM scan_status WHERE id = 1"
        ).fetchone()
        scan_job = scheduler.get_job("hourly-price-scan") if scheduler else None
        configured_stores = load_stores(Path(app.config["STORES_FILE"]))
        categories = sorted(
            {category for configured_store in configured_stores for category in configured_store.categories}
        )
        return render_template(
            "index.html",
            products=rows,
            stores=stores,
            categories=categories,
            selected_categories=selected_categories,
            selected_store=store,
            query=query,
            recent_observations=sum(item["observations"] for item in recent),
            recent_products=len(recent),
            notifications=notifications,
            scan_status=scan_status,
            next_scan=scan_job.next_run_time if scan_job else None,
        )

    @app.post("/scan")
    def trigger_scan():
        if scheduler is None:
            flash("Automatic scanning is not enabled for this server.", "error")
            return redirect(url_for("index"))

        current_status = database().execute(
            "SELECT status FROM scan_status WHERE id = 1"
        ).fetchone()
        if current_status and current_status["status"] == "running":
            flash("A scan is already running.", "error")
            return redirect(url_for("index"))

        scheduler.modify_job("hourly-price-scan", next_run_time=datetime.now())
        flash("A full scan has been queued.", "success")
        return redirect(url_for("index"))

    @app.post("/search")
    def search_products():
        query_text = request.form.get("q", "").strip()
        if not query_text or len(query_text) > 200:
            flash("Enter search terms (up to 200 characters).", "error")
            return redirect(url_for("index"))

        configured_stores = load_stores(Path(app.config["STORES_FILE"]))
        categories = {category for store in configured_stores for category in store.categories}
        selected_categories = set(request.form.getlist("category"))
        if selected_categories - categories:
            flash("Choose categories from the list and try again.", "error")
            return redirect(url_for("index"))

        try:
            query = parse_query(query_text)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        active_stores = [
            store for store in configured_stores
            if not selected_categories or selected_categories.intersection(store.categories)
        ]
        if not active_stores:
            flash("No stores are configured for the selected categories.", "error")
            return redirect(url_for("index"))

        found = 0
        stores_with_results = 0
        failed_stores: list[str] = []
        for store in active_stores:
            try:
                products = search_store(store, query, max_results=5)
            except Exception as exc:
                log.warning("[%s] Web search failed: %s", store.name, exc)
                failed_stores.append(store.name)
                continue

            stores_with_results += 1
            for product in products:
                save_price(database(), product, query.raw)
                found += 1

        if found:
            message = f"Search complete: found {found} products at {stores_with_results} stores."
            if failed_stores:
                message += f" Could not search: {', '.join(failed_stores)}."
            flash(message, "success")
        elif failed_stores:
            flash(f"No products found. Could not search: {', '.join(failed_stores)}.", "error")
        else:
            flash("No matching products found.", "error")

        return redirect(
            url_for("index", q=query.raw, category=sorted(selected_categories), searched=1)
        )

    @app.post("/track")
    def track_product():
        url = request.form.get("url", "").strip()
        if len(url) > 2048:
            flash("Enter an HTTPS product-page URL from a supported store.", "error")
            return redirect(url_for("index"))

        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
            port = parts.port
        except ValueError:
            flash("Enter a valid HTTPS product-page URL from a supported store.", "error")
            return redirect(url_for("index"))

        store = store_for_url(url)
        if (
            parts.scheme.lower() != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
            or store is None
            or not (host == store.domain or host.endswith("." + store.domain))
        ):
            flash("Enter an HTTPS product-page URL from a supported store.", "error")
            return redirect(url_for("index"))

        try:
            product = scrape_product(url, store.name)
            save_price(database(), product, None)
        except Exception as exc:
            app.logger.exception("Could not start tracking %s", url)
            flash(f"Could not track that product: {exc}", "error")
        else:
            flash(f"Now tracking {product.name} at {product.store}.", "success")
        return redirect(url_for("index"))

    @app.get("/product/<product_id>")
    def product_detail(product_id: str):
        connection = database()
        product = connection.execute(
            "SELECT product_id, store, name, url FROM tracked_products WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            abort(404)
        history = connection.execute(
            """
            SELECT price, scanned_at
              FROM price_history
             WHERE product_id = ? OR url = ?
             ORDER BY scanned_at DESC, id DESC
            """,
            (product_id, product["url"]),
        ).fetchall()
        return render_template("product.html", product=product, history=history)

    return app
