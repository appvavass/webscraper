from __future__ import annotations

import argparse
import logging
import shlex
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .extraction import scrape_product
from .fetch import CFG
from .models import DEFAULT_DB, STORES, Store, load_stores, log, store_for_url
from .query import parse_query
from .search import CACHE_DIR, search_store
from .storage import check_stores, handle_product, init_db, list_tracked, run_recheck


def print_flag_reminder():
    print("\n" + "=" * 65)
    print("💡 HELPER FLAGS REMINDER (Pass these via command-line or interactive prompt):")
    print("  --category CAT [CAT...] : Filter shops (electronics, design, furniture, auction, secondhand, general)")
    print("  --exclude WORD [WORD...] : Exclude items with specific words (e.g. --exclude skal fodral)")
    print("  --min-price SEK          : Ignore items cheaper than SEK limit")
    print("  --max-price SEK          : Ignore items pricier than SEK limit")
    print("  --max-per-store N        : Max matching items per store (default: 10)")
    print("  --js auto|always|never   : JavaScript engine mode")
    print("  --delay SECONDS          : Pause between requests (default: 1.5s)")
    print("  --ignore-robots          : Ignore robots.txt rules")
    print("  --retry-blocked          : Retry shops that failed recently")
    print("  --dump-html DIR          : Dump raw HTML for debugging")
    print("  -v, --verbose            : Turn on verbose log messages")
    print("=" * 65 + "\n")


def prompt_user_for_input(all_stores: list[Store], base_args: argparse.Namespace) -> tuple[str, list[str]]:
    print_flag_reminder()
    raw_input = input("Enter product to search for (you can include flags like --min-price 1200): ").strip()
    if not raw_input:
        print("Search text cannot be empty.")
        sys.exit(1)

    # Re-parse input string to extract embedded flags (--min-price, --exclude, etc.)
    tokens = shlex.split(raw_input)
    sub_parser = argparse.ArgumentParser(add_help=False)
    sub_parser.add_argument("--exclude", nargs="+", default=[])
    sub_parser.add_argument("--min-price", type=float)
    sub_parser.add_argument("--max-price", type=float)
    sub_parser.add_argument("--max-per-store", type=int)

    extra_args, remaining_tokens = sub_parser.parse_known_args(tokens)

    if extra_args.min_price is not None:
        base_args.min_price = extra_args.min_price
    if extra_args.max_price is not None:
        base_args.max_price = extra_args.max_price
    if extra_args.exclude:
        base_args.exclude.extend(extra_args.exclude)
    if extra_args.max_per_store is not None:
        base_args.max_per_store = extra_args.max_per_store

    search_text = " ".join(remaining_tokens)

    categories_set = set()
    for store in all_stores:
        categories_set.update(store.categories)
    sorted_categories = sorted(categories_set)

    print("\nAvailable Categories:")
    print("  [0] ALL Stores")
    for idx, cat in enumerate(sorted_categories, 1):
        print(f"  [{idx}] {cat.capitalize()}")

    choice = input("\nSelect categories (e.g. 1, 3 or multiple separated by comma) [0]: ").strip()
    selected_cats = []

    if choice and choice != "0":
        indices = [c.strip() for c in choice.split(",") if c.strip().isdigit()]
        for idx_str in indices:
            idx = int(idx_str)
            if 1 <= idx <= len(sorted_categories):
                selected_cats.append(sorted_categories[idx - 1])

    return search_text, selected_cats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Search Swedish web shops, track prices, and view or report on the database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_argument_group("what to do")
    mode.add_argument("--search", help='Product to look for, e.g. "Sony WH-1000XM5"')
    mode.add_argument("--category", "--categories", nargs="+", help="Filter stores by category (e.g. design furniture)")
    mode.add_argument("--recheck", action="store_true", help="Re-scan all tracked products")
    mode.add_argument("--list", action="store_true", help="List tracked products")
    mode.add_argument("--check-stores", action="store_true", help="Check robots.txt status")
    mode.add_argument("--url", nargs="+", help="Track specific product page URLs")
    mode.add_argument("--web", action="store_true", help="Start the local database dashboard")
    mode.add_argument("--send-daily-report", action="store_true", help="Send the last 24 hours of prices to Telegram")
    mode.add_argument("--daily", action="store_true", help="Recheck tracked prices, then send a Telegram report")

    opt = p.add_argument_group("options")
    default_stores = Path(__file__).resolve().parent.parent / "stores.txt"
    opt.add_argument("--stores-file", default=str(default_stores), help="Stores file")
    opt.add_argument("--stores", nargs="+", type=str.lower, help="Only search specific stores by name")
    opt.add_argument("--max-per-store", type=int, default=10, help="Products per store (default: 10)")
    opt.add_argument("--refresh-cache", action="store_true", help="Clear sitemap/URL cache")
    opt.add_argument("--exclude", nargs="+", default=[], help="Disqualifying words (e.g. --exclude skal fodral)")
    opt.add_argument("--min-price", type=float, help="Minimum price limit in SEK")
    opt.add_argument("--max-price", type=float, help="Maximum price limit in SEK")
    opt.add_argument("--retry-blocked", action="store_true", help="Retry blocked stores")
    opt.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file path (default: {DEFAULT_DB})")
    opt.add_argument("--host", default="127.0.0.1", help="Dashboard address (default: 127.0.0.1)")
    opt.add_argument("--port", type=int, default=5000, help="Dashboard port (default: 5000)")
    opt.add_argument("--js", choices=["auto", "always", "never"], default="auto", help="Playwright usage mode")
    opt.add_argument("--delay", type=float, default=1.5, help="Average pause between requests (default: 1.5s)")
    opt.add_argument("--ignore-robots", action="store_true", help="Bypass robots.txt")
    opt.add_argument("--dump-html", metavar="DIR", help="Save fetched HTML for debugging")
    opt.add_argument("-v", "--verbose", action="store_true", help="Verbose log output")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    CFG.js_mode = args.js
    CFG.delay = max(0.0, args.delay)
    CFG.respect_robots = not args.ignore_robots
    if args.dump_html:
        CFG.dump_dir = Path(args.dump_html)
        CFG.dump_dir.mkdir(parents=True, exist_ok=True)

    if args.refresh_cache and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        log.info("Cache cleared.")

    if args.web:
        from .web import create_app

        create_app(args.db).run(host=args.host, port=args.port, debug=False)
        return 0

    if args.send_daily_report or args.daily:
        from .notifications import NotificationError, load_dotenv, send_daily_report

        env_path = Path(__file__).resolve().parent.parent / ".env"
        try:
            load_dotenv(env_path)
            conn = init_db(args.db)
            try:
                if args.daily:
                    run_recheck(conn)
                character_count = send_daily_report(conn)
            finally:
                conn.close()
        except (NotificationError, ValueError, OSError) as exc:
            log.error("Daily report failed: %s", exc)
            return 1
        log.info("Daily report sent to Telegram (%d characters).", character_count)
        return 0

    STORES[:] = load_stores(Path(args.stores_file))
    if not STORES:
        log.error("No stores loaded from %s", args.stores_file)
        return 1

    if args.check_stores:
        check_stores()
        return 0

    conn = init_db(args.db)

    if args.list:
        list_tracked(conn)
        return 0

    if args.recheck:
        run_recheck(conn)
        return 0

    if args.url:
        for u in args.url:
            st = store_for_url(u)
            name = st.name if st else urlsplit(u).netloc
            try:
                prod = scrape_product(u, name)
                handle_product(conn, prod, None)
            except Exception as exc:
                log.error("[%s] Failed to fetch %s: %s", name, u, exc)
        return 0

    search_text = args.search
    selected_categories = [c.lower() for c in (args.category or [])]

    if not search_text and sys.stdin.isatty():
        search_text, selected_categories = prompt_user_for_input(STORES, args)

    if not search_text:
        log.error("Please provide a search query or use interactive mode.")
        return 1

    try:
        query = parse_query(search_text, args.exclude, args.min_price, args.max_price)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    active_stores = STORES
    if args.stores:
        active_stores = [s for s in STORES if s.name.lower() in args.stores]
    elif selected_categories:
        active_stores = [
            s for s in STORES
            if any(cat in s.categories for cat in selected_categories)
        ]
        log.info("Filtering stores by categories %s: selected %d stores.", selected_categories, len(active_stores))

    for store in active_stores:
        try:
            products = search_store(store, query, max_results=args.max_per_store)
            for product in products:
                handle_product(conn, product, query.raw)
        except Exception as exc:
            log.error("[%s] Search failed: %s", store.name, exc)

    return 0
