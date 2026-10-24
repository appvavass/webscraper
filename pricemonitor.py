#!/usr/bin/env python3
"""
price_monitor.py - Search Swedish web shops for a product and track its price.

Usage
-----
    # Search several stores, pick the best matches, record prices, report drops
    python price_monitor.py --search "Sony WH-1000XM5"

    # Re-check everything you are already tracking (cheap: no searching) - good for cron
    python price_monitor.py --recheck

    # List what is tracked, with the latest price
    python price_monitor.py --list

    # Which stores permit automated search pages? (reads each store's robots.txt)
    python price_monitor.py --check-stores

    # Track specific product pages (selectors are auto-detected) - then use --recheck
    python price_monitor.py --url "https://www.example.se/produkt/123" "https://www.example.se/produkt/456"

    # Useful options
    python price_monitor.py --search "iPhone 15 128GB" --exclude fodral skal \
        --stores elgiganten webhallen --max-per-store 2 --js never -v

How it works
------------
1. The query is cleaned and split into tokens.  Tokens containing digits
   (model numbers such as "wh-1000xm5") MUST appear in a result to count as a match.
2. Each store's search page is fetched and product links are collected from
   <a> tags AND from JSON embedded in the page (works for Next.js-style sites).
   If a store's robots.txt forbids its search page, the store's own published
   sitemap is used to find matching product pages instead (cached for 7 days).
3. The best-matching product pages are fetched and parsed WITHOUT per-store CSS
   selectors, using, in order: JSON-LD (schema.org Product), microdata
   (itemprop="price"), OpenGraph/product meta tags, and common price CSS patterns.
4. Every product gets a stable ID, is stored in SQLite with a timestamp, and is
   compared with its previous price.
5. Any store that fails or blocks us is logged and skipped - the others continue.

Install
-------
    pip install requests beautifulsoup4
    pip install playwright && playwright install chromium     # optional, for JS-only pages
"""

from __future__ import annotations

import argparse
import atexit
import gzip
import hashlib
import html as htmllib
import json
import logging
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote_plus, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_DB = "price_history.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}


@dataclass
class Settings:
    """Runtime settings, filled in from the command line in main()."""
    timeout: int = 20
    delay: float = 1.5                 # average pause between requests (seconds)
    js_mode: str = "auto"              # auto | always | never
    respect_robots: bool = True
    dump_dir: Optional[Path] = None    # save fetched HTML here for debugging


CFG = Settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("price_monitor")


# --------------------------------------------------------------------------- #
# Store definitions
# --------------------------------------------------------------------------- #
# Adding a store = adding one entry here. No per-store CSS selectors are needed;
# product pages are parsed generically (see extract_product_data).
#
#   search_url  - search page; {q} is replaced by the URL-encoded keywords
#   product_re  - regex matching the URL *path* of a product page
#   id_re       - optional regex whose group(1) is the shop's own product ID
#                 (makes the tracking ID stable even if the URL slug changes)
#
# NOTE: shops change their URLs now and then. If a store stops returning results,
# open its search page in a browser and adjust search_url / product_re here.

_LINK = r"""[^\s"'<>\\?#]+"""


@dataclass(frozen=True)
class Store:
    name: str
    domain: str
    search_url: str
    product_re: "re.Pattern[str]"
    id_re: Optional["re.Pattern[str]"] = None


STORES = [
    Store("Elgiganten", "elgiganten.se",
          "https://www.elgiganten.se/search?q={q}&view=products",
          re.compile(r"/product/" + _LINK)),
    Store("NetOnNet", "netonnet.se",
          "https://www.netonnet.se/search?query={q}",
          re.compile(r"/art/" + _LINK)),
    Store("Webhallen", "webhallen.com",
          "https://www.webhallen.com/se/search?searchString={q}",
          re.compile(r"/se/product/\d+" + r"[^\s\"'<>\\?#]*"),
          re.compile(r"/product/(\d+)")),
    Store("Komplett", "komplett.se",
          "https://www.komplett.se/search?q={q}",
          re.compile(r"/product/\d+" + r"[^\s\"'<>\\?#]*"),
          re.compile(r"/product/(\d+)")),
    Store("Inet", "inet.se",
          "https://www.inet.se/hitta?q={q}",
          re.compile(r"/produkt/\d+" + r"[^\s\"'<>\\?#]*"),
          re.compile(r"/produkt/(\d+)")),
    Store("MediaMarkt", "mediamarkt.se",
          "https://www.mediamarkt.se/sv/search.html?query={q}",
          re.compile(r"/sv/product/" + _LINK),
          re.compile(r"-(\d{5,})\.html$")),
    Store("Kjell", "kjell.com",
          "https://www.kjell.com/se/sok?query={q}",
          re.compile(r"/se/[^\s\"'<>\\?#]*/p/\d+"),
          re.compile(r"/p/(\d+)")),
]


def store_for_url(url: str) -> Optional[Store]:
    host = urlsplit(url).netloc.lower()
    return next((s for s in STORES if host.endswith(s.domain)), None)


def store_by_name(name: str) -> Optional[Store]:
    return next((s for s in STORES if s.name.lower() == name.lower()), None)


# --------------------------------------------------------------------------- #
# Exceptions & data classes
# --------------------------------------------------------------------------- #

class FetchError(Exception):
    """A page could not be downloaded (network error, block, timeout ...)."""


class RobotsDisallowed(FetchError):
    """robots.txt forbids fetching this URL."""


class ExtractionError(Exception):
    """A page was loaded but the wanted data could not be found in it."""


class PriceParseError(Exception):
    """A price string could not be converted to a number."""


@dataclass
class Product:
    product_id: str
    store: str
    name: str
    price: float
    url: str
    currency: str = "SEK"


# --------------------------------------------------------------------------- #
# Query cleaning
# --------------------------------------------------------------------------- #

_ALNUM = "0-9a-zåäöé"
_STOPWORDS = {"the", "a", "an", "and", "of", "för", "och", "med", "till", "en", "ett"}
_TOKEN_RE = re.compile(rf"[{_ALNUM}]+(?:[-.][{_ALNUM}]+)*")


def normalize(text: str) -> str:
    """Lowercase and strip everything except letters/digits: 'WH-1000XM5' -> 'wh1000xm5'."""
    return re.sub(rf"[^{_ALNUM}]", "", text.lower())


@dataclass
class Query:
    raw: str
    keywords: str          # cleaned text sent to the stores' search boxes
    tokens: list           # normalised tokens used for matching
    must: list             # tokens that contain digits (model numbers) - mandatory
    excludes: list         # normalised words that disqualify a result
    min_price: Optional[float] = None
    max_price: Optional[float] = None


def parse_query(raw: str, excludes: Optional[list] = None,
                min_price: Optional[float] = None,
                max_price: Optional[float] = None) -> Query:
    """Clean a free-text query into search keywords + matching rules."""
    cleaned = " ".join(raw.replace("\u00a0", " ").split())
    raw_tokens = list(dict.fromkeys(_TOKEN_RE.findall(cleaned.lower())))  # de-duplicated
    raw_tokens = [t for t in raw_tokens if t not in _STOPWORDS] or raw_tokens
    tokens = [t for t in (normalize(t) for t in raw_tokens) if t]
    if not tokens:
        raise ValueError("The search query contains no usable words.")
    return Query(
        raw=cleaned,
        keywords=" ".join(raw_tokens),
        tokens=tokens,
        must=[t for t in tokens if any(c.isdigit() for c in t)],
        excludes=[normalize(e) for e in (excludes or []) if normalize(e)],
        min_price=min_price,
        max_price=max_price,
    )


def relevance(query: Query, *texts: str) -> Optional[float]:
    """
    Score 0..1 for how well the given texts (link text, URL path, product name)
    match the query, or None if the result must be rejected.
    """
    joined = " ".join(texts)
    # Excluded words match whole words only ("ram" must not reject "fRAMe").
    words = re.findall(rf"[{_ALNUM}]+", joined.lower())
    if any(w.startswith(e) for e in query.excludes for w in words):
        return None
    haystack = normalize(joined)
    if any(token not in haystack for token in query.must):
        return None
    score = sum(token in haystack for token in query.tokens) / len(query.tokens)
    return score if score >= 0.6 else None


# --------------------------------------------------------------------------- #
# Price cleaning
# --------------------------------------------------------------------------- #

_PRICE_RE = re.compile(
    r"\d{1,3}(?:[ .]\d{3})+(?:[,.]\d{1,2})?"   # with thousands separators
    r"|\d+(?:[,.]\d{1,2})?"                    # plain number
)


def clean_swedish_price(raw: str) -> float:
    """
    Convert a Swedish price string to a float.

      '1 299 kr' -> 1299.0     '1\u00a0299:-' -> 1299.0     '1 299,50 kr' -> 1299.5
      '1.299,00 SEK' -> 1299.0     'Fr. 499 kr' -> 499.0

    If several numbers appear, the FIRST is used.
    """
    text = (
        raw.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").strip()
    )
    match = _PRICE_RE.search(text)
    if not match:
        raise PriceParseError(f"No number found in price text: {raw!r}")

    number = match.group(0).replace(" ", "")
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", number):
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError as exc:
        raise PriceParseError(f"Could not convert {number!r} to a number") from exc


def _to_number(value) -> Optional[float]:
    """Turn a structured-data value (number or string) into a positive float, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", str(value)):
            number = float(value)               # machine format: "1299.00"
        else:
            number = clean_swedish_price(str(value))   # human format: "1 299 kr"
    except (PriceParseError, ValueError):
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------- #
# Fetching: requests first, Playwright as a fallback
# --------------------------------------------------------------------------- #

def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=1, connect=0, read=0, backoff_factor=1.0,
                  status_forcelist=(500, 502, 504), allowed_methods=("GET",))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


SESSION = _build_session()
_ROBOTS: dict = {}
_BLOCK_MARKERS = ("captcha", "access denied", "attention required",
                  "are you a robot", "verifying you are human")


def allowed_by_robots(url: str) -> bool:
    """Check robots.txt (cached per host). If it cannot be read, we allow the fetch."""
    if not CFG.respect_robots:
        return True
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _ROBOTS:
        parser: Optional[RobotFileParser] = None
        try:
            resp = SESSION.get(base + "/robots.txt", timeout=8)
            if resp.status_code == 200:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
        except requests.RequestException:
            parser = None
        _ROBOTS[base] = parser
    parser = _ROBOTS[base]
    return True if parser is None else parser.can_fetch(HEADERS["User-Agent"], url)


def fetch_requests(url: str) -> str:
    """Download a page with plain `requests`."""
    if not allowed_by_robots(url):
        raise RobotsDisallowed("disallowed by robots.txt - add product pages directly with --url")
    try:
        resp = SESSION.get(url, timeout=(8, CFG.timeout))
    except requests.exceptions.Timeout as exc:
        raise FetchError(f"timed out after {CFG.timeout}s") from exc
    except requests.exceptions.RequestException as exc:
        raise FetchError(f"network error: {exc}") from exc

    if resp.status_code in (403, 429, 503):
        raise FetchError(f"blocked or rate-limited (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code}")

    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "utf-8"          # avoid mangled å/ä/ö
    html = resp.text
    if len(html) < 30000 and any(m in html.lower() for m in _BLOCK_MARKERS):
        raise FetchError("bot-protection page returned")
    return html


_PW: dict = {"pw": None, "browser": None, "context": None}


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _playwright_context():
    """Start one shared headless browser lazily (reused for every page)."""
    if _PW["context"] is None:
        from playwright.sync_api import sync_playwright
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch(headless=True)
        _PW["context"] = _PW["browser"].new_context(
            user_agent=HEADERS["User-Agent"], locale="sv-SE")
    return _PW["context"]


def close_browser() -> None:
    for key in ("context", "browser"):
        try:
            if _PW[key] is not None:
                _PW[key].close()
        except Exception:
            pass
    try:
        if _PW["pw"] is not None:
            _PW["pw"].stop()
    except Exception:
        pass
    _PW.update(pw=None, browser=None, context=None)


atexit.register(close_browser)


def fetch_playwright(url: str) -> str:
    """Download a page with a headless browser (renders JavaScript)."""
    if not allowed_by_robots(url):
        raise RobotsDisallowed("disallowed by robots.txt - add product pages directly with --url")
    try:
        page = _playwright_context().new_page()
        try:
            page.goto(url, timeout=CFG.timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass                      # some sites never go idle; use what we have
            return page.content()
        finally:
            page.close()
    except Exception as exc:
        raise FetchError(f"Playwright error: {exc}") from exc


def _polite_delay() -> None:
    time.sleep(random.uniform(CFG.delay * 0.7, CFG.delay * 1.3))


def _dump_html(url: str, html: str, used_js: bool) -> None:
    if CFG.dump_dir:
        name = f"{hashlib.sha1(url.encode()).hexdigest()[:8]}_{'js' if used_js else 'raw'}.html"
        (CFG.dump_dir / name).write_text(html, encoding="utf-8")
        log.info("Saved HTML for %s -> %s", url, CFG.dump_dir / name)


def fetch_and_extract(url: str, extractor: Callable[[str], object]):
    """
    Fetch `url` and run `extractor(html)` on it.

    In 'auto' mode we try plain requests first. If the page LOADED but the
    extractor found nothing (typical for JavaScript-rendered pages), we render it
    with Playwright when it is installed. If the shop refused or reset the
    connection we stop: a browser would not change that, and this tool does not
    try to get around blocks.
    """
    if CFG.js_mode == "never":
        modes = [False]
    elif CFG.js_mode == "always":
        modes = [True]
    else:
        modes = [False] + ([True] if playwright_available() else [])

    last_error: Exception = ExtractionError("nothing usable found")
    for use_js in modes:
        try:
            _polite_delay()
            html = fetch_playwright(url) if use_js else fetch_requests(url)
            _dump_html(url, html, use_js)
            result = extractor(html)
            if result:
                return result
            last_error = ExtractionError("nothing usable found")
        except RobotsDisallowed:
            raise                                     # retrying with JS would not help
        except FetchError as exc:
            last_error = exc
            if not use_js and CFG.js_mode == "auto":
                break                                 # blocked/unreachable: give up on this page
        except ExtractionError as exc:
            last_error = exc
            log.debug("Attempt (js=%s) found nothing at %s: %s", use_js, url, exc)
    if (isinstance(last_error, ExtractionError) and CFG.js_mode != "always"
            and not playwright_available()):
        last_error = ExtractionError(
            f"{last_error} - the page may be JavaScript-rendered; install Playwright: "
            "pip install playwright && playwright install chromium")
    raise last_error


# --------------------------------------------------------------------------- #
# Product IDs
# --------------------------------------------------------------------------- #

def canonicalize(url: str) -> str:
    """https://Host/path/?utm=1#x -> https://host/path (query and fragment dropped)."""
    parts = urlsplit(url)
    return f"https://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def product_id_for(url: str, store: Optional[Store]) -> str:
    """
    Stable 12-char ID. Uses the shop's own product number when we know how to
    read it (survives slug changes); otherwise a hash of the canonical URL.
    """
    path = urlsplit(url).path
    if store and store.id_re:
        match = store.id_re.search(path)
        if match:
            return hashlib.sha1(f"{store.name}:{match.group(1)}".encode()).hexdigest()[:12]
    return hashlib.sha1(canonicalize(url).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Generic product-page parsing (no per-store selectors)
# --------------------------------------------------------------------------- #

# Last-resort CSS patterns, tried in order. A match only counts if its text
# looks like money ("kr", ":-", "SEK") so we don't pick up random numbers.
PRICE_CSS_PATTERNS = [
    '[data-testid*="price" i]',
    '[class*="product-price" i]',
    '[class*="productPrice" i]',
    '[class*="current-price" i]',
    '[class*="price" i]',
    ".price",
]


def _flatten_ld(node):
    """Yield every dict inside a JSON-LD structure (lists, @graph, nested items)."""
    if isinstance(node, list):
        for item in node:
            yield from _flatten_ld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                yield from _flatten_ld(node[key])


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            data = json.loads(tag.string or tag.get_text(), strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        yield from _flatten_ld(data)


def _has_type(node: dict, wanted: str) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.lower() == wanted.lower() for k in kinds)


def _offer_price(offer):
    """Return (price, currency) from a schema.org Offer / AggregateOffer, or (None, None)."""
    if isinstance(offer, list):
        found = [r for r in (_offer_price(o) for o in offer) if r[0] is not None]
        return min(found, key=lambda r: r[0]) if found else (None, None)
    if not isinstance(offer, dict):
        return None, None
    if "offers" in offer:                          # AggregateOffer with nested offers
        nested = _offer_price(offer["offers"])
        if nested[0] is not None:
            return nested
    for key in ("price", "lowPrice"):
        price = _to_number(offer.get(key))
        if price is not None:
            return price, offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    if isinstance(spec, dict):
        price = _to_number(spec.get("price"))
        if price is not None:
            return price, spec.get("priceCurrency") or offer.get("priceCurrency")
    return None, None


def _meta(soup: BeautifulSoup, *names: str) -> Optional[str]:
    for name in names:
        tag = (soup.find("meta", attrs={"property": name})
               or soup.find("meta", attrs={"name": name}))
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _element_price(element) -> Optional[float]:
    value = element.get("content") or element.get("data-price") \
        or element.get_text(" ", strip=True)
    return _to_number(value)


def _tidy_name(text: str) -> str:
    return " ".join(text.split())[:200]


def extract_product_data(html: str, name_selector: Optional[str] = None,
                         price_selector: Optional[str] = None):
    """
    Return (name, price, currency) from a product page, trying in order:
      0. selectors given by the user (optional override)
      1. JSON-LD  (<script type="application/ld+json">, schema.org Product)
      2. microdata (itemprop="price")
      3. meta tags (product:price:amount, og:price:amount)
      4. common price CSS patterns
    Raises ExtractionError if no price or no name can be found.
    """
    soup = BeautifulSoup(html, "html.parser")
    name = price = currency = None

    # 0. explicit overrides
    if price_selector:
        el = soup.select_one(price_selector)
        price = _element_price(el) if el else None
    if name_selector:
        el = soup.select_one(name_selector)
        name = _tidy_name(el.get_text(" ", strip=True)) if el else None

    # 1. JSON-LD
    if price is None or name is None:
        for node in _iter_jsonld(soup):
            if not _has_type(node, "Product"):
                continue
            if name is None and isinstance(node.get("name"), str):
                name = _tidy_name(node["name"])
            if price is None:
                price, currency = _offer_price(node.get("offers"))
            if name and price is not None:
                break

    # 2. microdata
    if price is None:
        el = soup.select_one('[itemprop="price"]')
        if el:
            price = _element_price(el)
            cur_el = soup.select_one('[itemprop="priceCurrency"]')
            currency = currency or (cur_el.get("content") or cur_el.get_text(strip=True)
                                    if cur_el else None)

    # 3. meta tags
    if price is None:
        price = _to_number(_meta(soup, "product:price:amount", "og:price:amount",
                                 "product:sale_price:amount"))
        currency = currency or _meta(soup, "product:price:currency", "og:price:currency")

    # 4. CSS patterns (must look like money)
    if price is None:
        for selector in PRICE_CSS_PATTERNS:
            for el in soup.select(selector)[:6]:
                text = el.get_text(" ", strip=True)
                if len(text) <= 40 and re.search(r"kr|:-|sek", text, re.I):
                    price = _to_number(text)
                    if price is not None:
                        break
            if price is not None:
                break

    # Name fallbacks
    if not name:
        og = _meta(soup, "og:title")
        h1 = soup.find("h1")
        title = soup.find("title")
        for candidate in (og, h1.get_text(" ", strip=True) if h1 else None,
                          title.get_text(" ", strip=True) if title else None):
            if candidate:
                name = _tidy_name(candidate)
                break

    if price is None:
        raise ExtractionError("no price found on page")
    if not name:
        raise ExtractionError("no product name found on page")
    return name, price, (currency or "SEK").upper()


# --------------------------------------------------------------------------- #
# Store search: find candidate product URLs
# --------------------------------------------------------------------------- #

def find_ranked_links(html: str, store: Store, base_url: str, query: Query) -> list:
    """
    Collect product-page URLs from a search results page and rank them by how
    well they match the query. Links come from <a> tags AND from any product
    paths hidden in embedded JSON (__NEXT_DATA__ etc.), so JS-framework sites
    that ship their results as JSON still work.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict = {}                      # product_id -> {"url": ..., "text": ...}

    def add(href: str, text: str = "") -> None:
        url = canonicalize(urljoin(base_url, href))
        parts = urlsplit(url)
        if not parts.netloc.endswith(store.domain) or not store.product_re.search(parts.path):
            return
        entry = found.setdefault(product_id_for(url, store), {"url": url, "text": ""})
        if text and text not in entry["text"]:
            entry["text"] += " " + text

    for a in soup.find_all("a", href=True):
        add(a["href"], a.get_text(" ", strip=True)[:200])
    for match in store.product_re.finditer(html.replace("\\/", "/")):
        add(match.group(0))

    ranked = []
    for order, entry in enumerate(found.values()):
        score = relevance(query, entry["text"], urlsplit(entry["url"]).path)
        if score is not None:
            ranked.append((-score, order, entry["url"]))
    ranked.sort()
    if not ranked:
        raise ExtractionError(
            f"{len(found)} product link(s) seen, none matching '{query.keywords}'")
    return [url for _, _, url in ranked]


def scrape_product(url: str, store_name: str, query: Optional[Query] = None,
                   name_selector: Optional[str] = None,
                   price_selector: Optional[str] = None) -> Product:
    """Fetch one product page and return a Product (raises FetchError/ExtractionError)."""
    def extractor(html: str):
        name, price, currency = extract_product_data(html, name_selector, price_selector)
        if query and relevance(query, name, urlsplit(url).path) is None:
            raise ExtractionError(f"page '{name[:60]}' does not match the search")
        if query and query.min_price is not None and price < query.min_price:
            raise ExtractionError(f"price {price:g} kr is below --min-price")
        if query and query.max_price is not None and price > query.max_price:
            raise ExtractionError(f"price {price:g} kr is above --max-price")
        return name, price, currency

    name, price, currency = fetch_and_extract(url, extractor)
    if currency != "SEK":
        raise ExtractionError(f"unexpected currency {currency}")
    return Product(product_id_for(url, store_by_name(store_name)),
                   store_name, name, price, canonicalize(url), currency)


def search_store(store: Store, query: Query, max_results: int) -> list:
    """Search one store and return up to `max_results` scraped Products."""
    search_url = store.search_url.format(q=quote_plus(query.keywords))
    log.info("[%s] searching: %s", store.name, search_url)
    try:
        links = fetch_and_extract(
            search_url, lambda html: find_ranked_links(html, store, search_url, query))
    except (RobotsDisallowed, ExtractionError) as exc:
        log.info("[%s] search page not usable (%s) - trying the store's sitemap", store.name, exc)
        try:
            links = sitemap_links(store, query)
        except (FetchError, ExtractionError) as exc2:
            raise ExtractionError(f"search: {exc}; sitemap: {exc2}") from exc2
    log.info("[%s] %d matching link(s), checking top %d",
             store.name, len(links), min(len(links), max_results))

    products = []
    for link in links[:max_results]:
        try:
            products.append(scrape_product(link, store.name, query))
        except RobotsDisallowed as exc:
            log.warning("[%s] skipped %s: %s", store.name, link, exc)
        except (FetchError, ExtractionError) as exc:
            log.warning("[%s] skipped %s: %s", store.name, link, exc)
    if not products:
        raise ExtractionError("matching links found but no product page could be parsed")
    return products


# --------------------------------------------------------------------------- #
# Discovery through the store's published sitemaps
# --------------------------------------------------------------------------- #
# Sitemaps are files shops publish *for* automated clients. When a store's
# robots.txt forbids its search page, we can still find matching product pages
# by name in the sitemap (product pages are checked against robots.txt too).

CACHE_DIR = Path(".price_monitor_cache")
SITEMAP_TTL = 7 * 24 * 3600
MAX_SITEMAP_FILES = 40
MAX_SITEMAP_BYTES = 60_000_000


def _site_base(store: Store) -> str:
    parts = urlsplit(store.search_url)
    return f"{parts.scheme}://{parts.netloc}"


def _sitemaps_listed_in_robots(base: str) -> list:
    allowed_by_robots(base + "/")                   # makes sure robots.txt is loaded
    parser = _ROBOTS.get(base)
    maps = parser.site_maps() if parser else None
    return list(maps) if maps else [base + "/sitemap.xml"]


def _fetch_sitemap(url: str) -> str:
    if not allowed_by_robots(url):
        raise RobotsDisallowed("sitemap disallowed by robots.txt")
    _polite_delay()
    try:
        resp = SESSION.get(url, timeout=(8, 60))
    except requests.exceptions.RequestException as exc:
        raise FetchError(f"sitemap network error: {exc}") from exc
    if resp.status_code != 200:
        raise FetchError(f"sitemap HTTP {resp.status_code}")
    data = resp.content
    if data[:2] == b"\x1f\x8b":                      # .xml.gz
        data = gzip.decompress(data)
    if len(data) > MAX_SITEMAP_BYTES:
        raise FetchError("sitemap too large")
    return data.decode("utf-8", errors="replace")


def load_store_urls(store: Store) -> list:
    """Every product URL listed in the store's sitemaps (cached for a week)."""
    cache = CACHE_DIR / f"{store.name.lower()}_urls.txt.gz"
    if cache.exists() and time.time() - cache.stat().st_mtime < SITEMAP_TTL:
        return gzip.decompress(cache.read_bytes()).decode("utf-8").splitlines()

    log.info("[%s] reading the store's sitemaps (first run is slow, then cached 7 days)",
             store.name)
    queue = _sitemaps_listed_in_robots(_site_base(store))
    seen: set = set()
    urls: list = []
    files = 0
    last_error: Optional[Exception] = None
    while queue and files < MAX_SITEMAP_FILES:
        sitemap = queue.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)
        try:
            xml = _fetch_sitemap(sitemap)
        except FetchError as exc:
            last_error = exc
            log.debug("[%s] %s: %s", store.name, sitemap, exc)
            continue
        files += 1
        locs = [htmllib.unescape(m.strip()) for m in re.findall(
            r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", xml, re.S)]
        if "<sitemapindex" in xml[:3000]:
            queue.extend([l for l in locs if "product" in l.lower()] or locs)
        else:
            urls.extend(canonicalize(l) for l in locs
                        if store.product_re.search(urlsplit(l).path))
    if queue and files >= MAX_SITEMAP_FILES:
        log.warning("[%s] stopped after %d sitemap files (list may be incomplete)",
                    store.name, MAX_SITEMAP_FILES)
    if not urls:
        raise ExtractionError(f"no product URLs in the sitemaps ({last_error or 'none listed'})")
    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_bytes(gzip.compress("\n".join(urls).encode("utf-8")))
    return urls


def sitemap_links(store: Store, query: Query) -> list:
    """Rank sitemap URLs by how well their slug matches the query."""
    ranked = []
    for order, url in enumerate(load_store_urls(store)):
        score = relevance(query, urlsplit(url).path)
        if score is not None:
            ranked.append((-score, order, url))
    ranked.sort()
    unique: dict = {}
    for _, _, url in ranked:
        unique.setdefault(product_id_for(url, store), url)
    if not unique:
        raise ExtractionError(f"no URL in the sitemap matches '{query.keywords}'")
    return list(unique.values())


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def init_db(db_path: str) -> sqlite3.Connection:
    """Open/create the DB. Upgrades the old single-URL schema in place (no data loss)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url          TEXT NOT NULL,
            product_name TEXT,
            price        REAL NOT NULL,
            scanned_at   TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(price_history)")}
    for col in ("product_id", "store"):
        if col not in columns:
            conn.execute(f"ALTER TABLE price_history ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_url_time ON price_history (url, scanned_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pid ON price_history (product_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_products (
            product_id   TEXT PRIMARY KEY,
            url          TEXT NOT NULL,
            store        TEXT,
            name         TEXT,
            search_query TEXT,
            first_seen   TEXT NOT NULL,
            last_seen    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS store_health "
        "(store TEXT PRIMARY KEY, failed_at TEXT NOT NULL, reason TEXT)"
    )
    conn.commit()
    return conn


def get_previous_and_lowest(conn: sqlite3.Connection, product: Product):
    """Return (previous_price, lowest_price) recorded for this product (None, None if new)."""
    where = "product_id = ? OR url = ?"
    args = (product.product_id, product.url)
    prev = conn.execute(
        f"SELECT price FROM price_history WHERE {where} ORDER BY id DESC LIMIT 1", args
    ).fetchone()
    low = conn.execute(
        f"SELECT MIN(price) FROM price_history WHERE {where}", args
    ).fetchone()
    return (prev[0] if prev else None), (low[0] if low else None)


def save_price(conn: sqlite3.Connection, product: Product, query_text: Optional[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO price_history (url, product_name, price, scanned_at, product_id, store) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (product.url, product.name, product.price, now, product.product_id, product.store),
    )
    conn.execute(
        """
        INSERT INTO tracked_products
            (product_id, url, store, name, search_query, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            url = excluded.url,
            name = excluded.name,
            last_seen = excluded.last_seen,
            search_query = COALESCE(excluded.search_query, tracked_products.search_query)
        """,
        (product.product_id, product.url, product.store, product.name,
         query_text, now, now),
    )
    conn.commit()


HEALTH_TTL = 24 * 3600


def record_failure(conn: sqlite3.Connection, store: str, reason: str) -> None:
    conn.execute("INSERT OR REPLACE INTO store_health VALUES (?, ?, ?)",
                 (store, datetime.now().isoformat(timespec="seconds"), reason[:150]))
    conn.commit()


def clear_failure(conn: sqlite3.Connection, store: str) -> None:
    conn.execute("DELETE FROM store_health WHERE store = ?", (store,))
    conn.commit()


def recent_failure(conn: sqlite3.Connection, store: str) -> Optional[str]:
    """Reason if the store blocked us within the last 24h, else None."""
    row = conn.execute("SELECT failed_at, reason FROM store_health WHERE store = ?",
                       (store,)).fetchone()
    if row and (datetime.now() - datetime.fromisoformat(row[0])).total_seconds() < HEALTH_TTL:
        return row[1]
    return None


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def fmt_sek(value: float) -> str:
    """1299.5 -> '1 299,50 kr'; 1299 -> '1 299 kr'."""
    if float(value).is_integer():
        formatted = f"{int(value):,}".replace(",", " ")
    else:
        formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} kr"


def report_price_change(p: Product, previous: Optional[float], lowest: Optional[float]) -> None:
    """Print a clear message comparing the current price with the previous scan."""
    tag = f"[{p.store}] {p.name}"
    if previous is None:
        print(f"ℹ️  New product tracked: {tag}\n"
              f"   {fmt_sek(p.price)} (baseline saved)\n   {p.url}")
    elif p.price < previous:
        diff = previous - p.price
        extra = "\n   ⭐ Lowest price recorded so far!" if lowest is not None and p.price <= lowest else ""
        print(f"🔻 PRICE DROP! {tag}\n"
              f"   {fmt_sek(previous)}  ->  {fmt_sek(p.price)}  "
              f"(-{fmt_sek(diff)}, -{diff / previous * 100:.1f}%){extra}\n   {p.url}")
    elif p.price > previous:
        diff = p.price - previous
        print(f"🔺 Price increase: {tag}\n"
              f"   {fmt_sek(previous)}  ->  {fmt_sek(p.price)}  "
              f"(+{fmt_sek(diff)}, +{diff / previous * 100:.1f}%)\n   {p.url}")
    else:
        print(f"➖ No change: {tag} - still {fmt_sek(p.price)}\n   {p.url}")


def handle_product(conn: sqlite3.Connection, product: Product, query_text: Optional[str]) -> None:
    """Compare with history, report, then store the new price."""
    previous, lowest = get_previous_and_lowest(conn, product)
    report_price_change(product, previous, lowest)
    save_price(conn, product, query_text)


# --------------------------------------------------------------------------- #
# Modes of operation
# --------------------------------------------------------------------------- #

def run_search(conn: sqlite3.Connection, query: Query, args) -> dict:
    """Search all selected stores. A failing store is logged and skipped."""
    selected = [s for s in STORES if not args.stores or s.name.lower() in args.stores]
    status: dict = {}
    seen: set = set()
    for store in selected:
        if not args.retry_blocked:
            reason = recent_failure(conn, store.name)
            if reason:
                log.info("[%s] skipped: refused us within the last 24h. "
                         "Use --retry-blocked to try again.", store.name)
                status[store.name] = f"SKIPPED  blocked recently ({reason})"
                continue
        try:
            products = search_store(store, query, args.max_per_store)
        except FetchError as exc:       # blocked / unreachable: remember it, don't hammer the site
            log.error("[%s] failed: %s", store.name, exc)
            status[store.name] = f"FAILED   {exc}"
            if "timed out" not in str(exc):
                record_failure(conn, store.name, str(exc))
            continue
        except Exception as exc:        # one broken retailer must not stop the rest
            log.error("[%s] failed: %s", store.name, exc)
            log.debug("Details", exc_info=True)
            status[store.name] = f"FAILED   {exc}"
            continue
        clear_failure(conn, store.name)
        count = 0
        for product in products:
            if product.product_id in seen:
                continue
            seen.add(product.product_id)
            handle_product(conn, product, query.raw)
            count += 1
        status[store.name] = f"OK       {count} product(s)"
    return status


def check_stores() -> None:
    """Report which stores permit automated search pages and publish sitemaps."""
    CFG.respect_robots = True
    for store in STORES:
        base = _site_base(store)
        search_ok = allowed_by_robots(store.search_url.format(q="test"))
        parser = _ROBOTS.get(base)
        if parser is None:
            note = "robots.txt unreadable"
        else:
            maps = parser.site_maps()
            note = f"{len(maps) if maps else 0} sitemap(s) listed"
        print(f"  {store.name:<12} search pages: {'allowed' if search_ok else 'DISALLOWED':<10} {note}")


def run_recheck(conn: sqlite3.Connection) -> dict:
    """Re-scan every tracked product directly by URL (no search step)."""
    rows = conn.execute(
        "SELECT product_id, url, store, search_query FROM tracked_products ORDER BY store, name"
    ).fetchall()
    if not rows:
        print("Nothing tracked yet. Start with:  python price_monitor.py --search \"<product>\"")
        return {}
    status: dict = {}
    for pid, url, store_name, query_text in rows:
        store_name = store_name or urlsplit(url).netloc
        counts = status.setdefault(store_name, {"ok": 0, "failed": []})
        try:
            product = scrape_product(url, store_name)
            product.product_id = pid          # keep the ID the product was first stored with
            handle_product(conn, product, query_text)
            counts["ok"] += 1
        except Exception as exc:        # keep going if a single page fails
            log.error("[%s] %s: %s", store_name, url, exc)
            counts["failed"].append(str(exc))
    return _format_status(status)


def _format_status(status: dict) -> dict:
    out = {}
    for store, v in status.items():
        if not v["failed"]:
            out[store] = f"OK       {v['ok']} product(s)"
        elif v["ok"] == 0:
            out[store] = f"FAILED   {v['failed'][0]}"
        else:
            out[store] = f"PARTIAL  {v['ok']} ok, {len(v['failed'])} failed: {v['failed'][0]}"
    return out


def run_single_url(conn: sqlite3.Connection, args) -> dict:
    """Track one or more explicit product-page URLs (selectors optional)."""
    status: dict = {}
    for url in args.url:
        store = store_for_url(url)
        store_name = store.name if store else urlsplit(url).netloc
        counts = status.setdefault(store_name, {"ok": 0, "failed": []})
        try:
            product = scrape_product(url, store_name,
                                     name_selector=args.name_selector,
                                     price_selector=args.price_selector)
            handle_product(conn, product, None)
            counts["ok"] += 1
        except Exception as exc:
            log.error("[%s] %s: %s", store_name, url, exc)
            counts["failed"].append(str(exc))
    return _format_status(status)


def list_tracked(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT t.store, t.name, t.url,
               (SELECT price FROM price_history h
                 WHERE h.product_id = t.product_id ORDER BY h.id DESC LIMIT 1),
               (SELECT MIN(price) FROM price_history h WHERE h.product_id = t.product_id)
          FROM tracked_products t ORDER BY t.store, t.name
        """
    ).fetchall()
    if not rows:
        print("Nothing tracked yet.")
        return
    for store, name, url, last, low in rows:
        print(f"[{store}] {name}\n   now {fmt_sek(last)}   lowest {fmt_sek(low)}\n   {url}")


def print_summary(status: dict) -> bool:
    """Print per-store results. Returns True if at least one store succeeded."""
    if not status:
        return True
    print("\n--- Summary " + "-" * 50)
    for store, text in status.items():
        print(f"  {store:<12} {text}")
    return any(text.startswith(("OK", "PARTIAL")) for text in status.values())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Search Swedish web shops for a product and track its price.")
    mode = p.add_argument_group("what to do")
    mode.add_argument("--search", help='Product to look for, e.g. "Sony WH-1000XM5"')
    mode.add_argument("--recheck", action="store_true", help="Re-scan all tracked products")
    mode.add_argument("--list", action="store_true", help="List tracked products")
    mode.add_argument("--check-stores", action="store_true",
                      help="Show which stores allow automated search (robots.txt)")
    mode.add_argument("--url", nargs="+", help="Track specific product page URL(s)")
    mode.add_argument("--name-selector", help="(with --url) optional CSS selector override")
    mode.add_argument("--price-selector", help="(with --url) optional CSS selector override")

    opt = p.add_argument_group("options")
    opt.add_argument("--stores", nargs="+", type=str.lower,
                     choices=[s.name.lower() for s in STORES], metavar="STORE",
                     help="Only search these stores (default: all). Choices: "
                          + ", ".join(s.name.lower() for s in STORES))
    opt.add_argument("--max-per-store", type=int, default=3,
                     help="Max products to track per store (default 3)")
    opt.add_argument("--exclude", nargs="+", default=[],
                     help="Words that disqualify a result, e.g. --exclude fodral skal")
    opt.add_argument("--min-price", type=float, help="Ignore results cheaper than this (SEK)")
    opt.add_argument("--max-price", type=float, help="Ignore results dearer than this (SEK)")
    opt.add_argument("--retry-blocked", action="store_true",
                     help="Also retry stores that refused us within the last 24h")
    opt.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    opt.add_argument("--js", choices=["auto", "always", "never"], default="auto",
                     help="Playwright usage: auto = only as fallback (default)")
    opt.add_argument("--delay", type=float, default=1.5,
                     help="Average pause between requests in seconds (default 1.5)")
    opt.add_argument("--ignore-robots", action="store_true",
                     help="Do not consult robots.txt (default: respect it)")
    opt.add_argument("--dump-html", metavar="DIR",
                     help="Save every fetched page here (for debugging store settings)")
    opt.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = p.parse_args()

    # Interactive fallback: no mode given and we are in a terminal -> ask.
    if not (args.search or args.recheck or args.list or args.url or args.check_stores):
        if sys.stdin.isatty():
            args.search = input("Search for a product: ").strip()
        if not args.search:
            p.error("give one of --search, --recheck, --list, --url or --check-stores")
    return args


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
    if CFG.js_mode == "always" and not playwright_available():
        log.error("--js always needs Playwright: pip install playwright && playwright install chromium")
        return 1

    if args.check_stores:
        check_stores()
        return 0

    try:
        conn = init_db(args.db)
    except sqlite3.Error as exc:
        log.error("Cannot open database %s: %s", args.db, exc)
        return 1

    try:
        if args.list:
            list_tracked(conn)
            return 0
        if args.recheck:
            ok = print_summary(run_recheck(conn))
        elif args.url:
            ok = print_summary(run_single_url(conn, args))
        else:
            try:
                query = parse_query(args.search, args.exclude, args.min_price, args.max_price)
            except ValueError as exc:
                log.error("%s", exc)
                return 1
            log.info("Searching for: %s  (must contain: %s)",
                     query.keywords, ", ".join(query.must) or "-")
            ok = print_summary(run_search(conn, query, args))
        return 0 if ok else 1
    except sqlite3.Error as exc:
        log.error("Database error: %s", exc)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    except Exception:
        log.exception("Unexpected error")
        return 1
    finally:
        conn.close()
        close_browser()


if __name__ == "__main__":
    sys.exit(main())