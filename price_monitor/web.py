from __future__ import annotations

import sqlite3

from flask import Flask, abort, g, render_template, request

from .storage import get_daily_summary, init_db


def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = db_path
    init_db(db_path).close()

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
        return render_template(
            "index.html",
            products=rows,
            stores=stores,
            selected_store=store,
            query=query,
            recent_observations=sum(item["observations"] for item in recent),
            recent_products=len(recent),
        )

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
