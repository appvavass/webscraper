import unittest
from pathlib import Path
from unittest.mock import patch

from price_monitor import fetch
from price_monitor.models import (
    CFG,
    ExtractionError,
    FetchBlocked,
    FetchError,
    RobotsDisallowed,
    load_stores,
    make_store,
)
from price_monitor.query import parse_query
from price_monitor.search import find_ranked_links, search_store


class FetchFallbackTests(unittest.TestCase):
    def test_auto_uses_playwright_after_recoverable_http_failure(self):
        with patch.object(CFG, "js_mode", "auto"), \
                patch.object(CFG, "delay", 0), \
                patch.object(fetch, "allowed_by_robots", return_value=True), \
                patch.object(fetch, "playwright_available", return_value=True), \
                patch.object(fetch, "_polite_delay"), \
                patch.object(fetch, "fetch_requests", side_effect=FetchError("network error")), \
                patch.object(fetch, "fetch_playwright", return_value="<html>rendered</html>") as browser, \
                patch.object(fetch, "_dump_html"):
            result = fetch.fetch_and_extract(
                "https://example.com/search",
                lambda html: html if "rendered" in html else None,
            )

        self.assertEqual(result, "<html>rendered</html>")
        browser.assert_called_once_with("https://example.com/search")

    def test_robots_denial_does_not_try_playwright(self):
        with patch.object(CFG, "js_mode", "auto"), \
                patch.object(fetch, "allowed_by_robots", return_value=False), \
                patch.object(fetch, "playwright_available", return_value=True), \
                patch.object(fetch, "fetch_playwright") as browser:
            with self.assertRaises(RobotsDisallowed):
                fetch.fetch_and_extract("https://example.com/search", lambda _: "result")

        browser.assert_not_called()

    def test_explicit_site_block_does_not_try_playwright(self):
        with patch.object(CFG, "js_mode", "auto"), \
                patch.object(CFG, "delay", 0), \
                patch.object(fetch, "allowed_by_robots", return_value=True), \
                patch.object(fetch, "playwright_available", return_value=True), \
                patch.object(fetch, "_polite_delay"), \
                patch.object(fetch, "fetch_requests", side_effect=FetchBlocked("HTTP 403")), \
                patch.object(fetch, "fetch_playwright") as browser:
            with self.assertRaises(FetchBlocked):
                fetch.fetch_and_extract("https://example.com/search", lambda _: "result")

        browser.assert_not_called()

    def test_search_does_not_crawl_sitemap_after_robots_denial(self):
        store = make_store("Example", "https://example.com")
        query = parse_query("phone")
        with patch("price_monitor.search.fetch_and_extract", side_effect=RobotsDisallowed("denied")), \
                patch("price_monitor.search.sitemap_links") as sitemap:
            with self.assertRaisesRegex(ExtractionError, "robots.txt"):
                search_store(store, query, max_results=1)

        sitemap.assert_not_called()

    def test_search_does_not_crawl_sitemap_after_site_rejection(self):
        store = make_store("Example", "https://example.com")
        query = parse_query("phone")
        with patch("price_monitor.search.fetch_and_extract", side_effect=FetchBlocked("HTTP 403")), \
                patch("price_monitor.search.sitemap_links") as sitemap:
            with self.assertRaisesRegex(ExtractionError, "not retrying"):
                search_store(store, query, max_results=1)

        sitemap.assert_not_called()

    def test_blocket_ranks_the_listing_card_title(self):
        blocket = next(
            store for store in load_stores(Path("stores.txt"))
            if store.domain == "blocket.se"
        )
        html = (
            '<article><a href="/recommerce/forsale/item/123"></a>'
            "<h2>iPhone 15 Pro</h2></article>"
        )

        links = find_ranked_links(
            html,
            blocket,
            "https://www.blocket.se/recommerce/forsale/search?q=iphone+15",
            parse_query("iphone 15"),
        )

        self.assertEqual(links, ["https://www.blocket.se/recommerce/forsale/item/123"])


if __name__ == "__main__":
    unittest.main()
