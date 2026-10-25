#!/usr/bin/env python3
"""
price_monitor.py - Search Swedish web shops by category and track product prices.

Usage & Flags Overview
----------------------
Search & Filtering:
    --search "Text"         Product to look for (e.g. "Samsung Frame" or "Lilla Åland")
    --category CAT [CAT...] Filter by category (electronics, design, furniture, auction, general)
    --exclude WORD [WORD...] Exclude results containing these words (e.g. --exclude skal fodral)
    --min-price SEK         Ignore items cheaper than this amount
    --max-price SEK         Ignore items more expensive than this amount
    --max-per-store N       Maximum accepted items to collect per store (default: 10)

Operation Modes:
    --recheck               Re-scan all tracked products (fast, directly by URL)
    --list                  List all currently tracked products and lowest recorded prices
    --check-stores          Check which stores permit search pages (via robots.txt)
    --url URL [URL...]      Track specific product page URLs directly

Advanced Options:
    --stores-file FILE      Custom stores file (default: stores.txt)
    --stores STORE [STORE...] Restrict search to specific shop names from stores.txt
    --js auto|always|never  Headless browser mode (Playwright required for 'always' or fallback)
    --delay SECONDS         Average pause between HTTP requests (default: 1.5s)
    --ignore-robots         Bypass robots.txt restrictions
    --retry-blocked         Retry stores that refused connection in the last 24h
    --refresh-cache         Clear cached search URLs and sitemap indexes
    --dump-html DIR         Save fetched raw/JS HTML to directory for debugging
    -v, --verbose           Enable debug logging

Install Dependencies
--------------------
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
import shlex
import shutil
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
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
# Configuration & Settings
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
    """Runtime configuration settings."""
    timeout: int = 20
    delay: float = 1.5
    js_mode: str = "auto"              # auto | always | never
    respect_robots: bool = True
    dump_dir: Optional[Path] = None


CFG = Settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("price_monitor")

_LINK = r"""[^\s"'<>\\?#]+"""


@dataclass(frozen=True)
class Store:
    name: str
    domain: str
    home: str
    categories: list[str] = field(default_factory=list)
    search_url: Optional[str] = None
    product_re: Optional["re.Pattern[str]"] = None
    id_re: Optional["re.Pattern[str]"] = None
    key: str = ""


# Tuned configurations for pre-defined shops
KNOWN = {
    "elgiganten.se": dict(
        key="Elgiganten",
        search_url="https://www.elgiganten.se/search?q={q}&view=products",
        product_re=re.compile(r"/product/" + _LINK)),
    "netonnet.se": dict(
        key="NetOnNet",
        search_url="https://www.netonnet.se/search?query={q}",
        product_re=re.compile(r"/art/" + _LINK)),
    "webhallen.com": dict(
        key="Webhallen",
        search_url="https://www.webhallen.com/se/search?searchString={q}",
        product_re=re.compile(r"/se/product/\d+" + r"[^\s\"'<>\\?#]*"),
        id_re=re.compile(r"/product/(\d+)")),
    "inet.se": dict(
        key="Inet",
        search_url="https://www.inet.se/hitta?q={q}",
        product_re=re.compile(r"/produkt/\d+" + r"[^\s\"'<>\\?#]*"),
        id_re=re.compile(r"/produkt/(\d+)")),
    "kjell.com": dict(
        key="Kjell",
        search_url="https://www.kjell.com/se/sok?query={q}",
        product_re=re.compile(r"/se/[^\s\"'<>\\?#]*/p/\d+"),
        id_re=re.compile(r"/p/(\d+)")),
    "svenssons.se": dict(
        key="Svenssons",
        search_url="https://www.svenssons.se/sok/?q={q}"),
    "nordiskagalleriet.se": dict(
        key="NordiskaGalleriet",
        search_url="https://www.nordiskagalleriet.se/sok?q={q}"),
    "lannamobler.se": dict(
        key="LannaMobler",
        search_url="https://www.lannamobler.se/sok?q={q}"),
    "royaldesign.se": dict(
        key="RoyalDesign",
        search_url="https://royaldesign.se/sok?q={q}"),
    "rum21.se": dict(
        key="Rum21",
        search_url="https://www.rum21.se/sok?q={q}"),
    "valamagasinet.se": dict(
        key="Valamagasinet",
        search_url="https://www.valamagasinet.se/sok?q={q}"),
    "ahlens.se": dict(
        key="Ahlens",
        search_url="https://www.ahlens.se/sok?q={q}"),
    "auctionet.com": dict(
        key="Auctionet",
        search_url="https://auctionet.com/sv/search?is_auction=true&q={q}"),
    "bukowskis.com": dict(
        key="Bukowskis",
        search_url="https://www.bukowskis.com/sv/lots/search?q={q}"),
    "tradera.com": dict(
        key="Tradera",
        search_url="https://www.tradera.com/search?q={q}"),
}

STORES: list[Store] = []

DEFAULT_STORES_TXT = """# Stores list - Name - Home URL [- Search URL] [# category1, category2]
Elgiganten - https://www.elgiganten.se # electronics
NetOnNet - https://www.netonnet.se # electronics
Webhallen - https://www.webhallen.com/se # electronics
Inet - https://www.inet.se # electronics
Kjell & Company - https://www.kjell.com/se # electronics

Svenssons - https://www.svenssons.se # design, furniture
Nordiska Galleriet - https://www.nordiskagalleriet.se # design, furniture
Länna Möbler - https://www.lannamobler.se # design, furniture
Royal Design - https://royaldesign.se # design, furniture
Rum21 - https://www.rum21.se # design, furniture
Vålamagasinet - https://www.valamagasinet.se # design, furniture
Åhléns - https://www.ahlens.se # general, design

Auctionet - https://auctionet.com # auction, design
Bukowskis - https://www.bukowskis.com # auction, design
Tradera - https://www.tradera.com # auction, general
"""

_URL_RE = re.compile(r"https?://\S+")


def _domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def make_store(name: str, home: str, search_template: Optional[str] = None, categories: Optional[list[str]] = None) -> Store:
    domain = _domain_of(home)
    known = None
    for d, cfg in KNOWN.items():
        if domain == d or domain.endswith("." + d):
            known = cfg
            break

    if known:
        search_url = search_template or known.get("search_url")
        product_re = known.get("product_re")
        id_re = known.get("id_re")
        key = known.get("key", domain)
    else:
        search_url = search_template or f"https://{domain}/sok?q={{q}}"
        product_re = None
        id_re = None
        key = domain

    return Store(
        name=name,
        domain=domain,
        home=home,
        categories=[c.lower() for c in (categories or ["general"])],
        search_url=search_url,
        product_re=product_re,
        id_re=id_re,
        key=key,
    )


def parse_store_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    categories = []
    if "#" in line:
        line, comment_part = line.split("#", 1)
        categories = [c.strip().lower() for c in comment_part.split(",") if c.strip()]

    urls = _URL_RE.findall(line)
    if not urls:
        m = re.search(r"(?:^|\s)((?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?)\s*$", line, re.I)
        if not m:
            return None
        urls = ["https://" + m.group(1)]
        name_part = line[:m.start(1)]
    else:
        name_part = line[:line.index(urls[0])]

    home = urls[0].rstrip(",;")
    template = next((u.rstrip(",;") for u in urls[1:] if "{q}" in u), None)
    name = name_part.strip(" \t-\u2013\u2014:,|=").strip() or _domain_of(home).split(".")[0].capitalize()
    return name, home, template, categories


def load_stores(path: Path) -> list[Store]:
    if not path.exists():
        path.write_text(DEFAULT_STORES_TXT, encoding="utf-8")
        log.info("Created %s with default store configurations.", path)
    stores: list[Store] = []
    seen: set = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        parsed = parse_store_line(line)
        if not parsed:
            continue
        name, home, template, categories = parsed
        store = make_store(name, home, template, categories)
        if store.domain in seen:
            continue
        seen.add(store.domain)
        stores.append(store)
    return stores


def store_for_url(url: str) -> Optional[Store]:
    host = urlsplit(url).netloc.lower()
    for store in STORES:
        if host.endswith(store.domain):
            return store
    for domain, cfg in KNOWN.items():
        if host.endswith(domain):
            return make_store(cfg["key"], f"https://{host}")
    return None


# --------------------------------------------------------------------------- #
# Exceptions & Data Models
# --------------------------------------------------------------------------- #

class FetchError(Exception):
    """Page download error."""

class RobotsDisallowed(FetchError):
    """Disallowed by robots.txt."""

class ExtractionError(Exception):
    """Data extraction error."""

class PriceParseError(Exception):
    """Price string parsing error."""

@dataclass
class Product:
    product_id: str
    store: str
    name: str
    price: float
    url: str
    currency: str = "SEK"


# --------------------------------------------------------------------------- #
# Query Engine & Relevance Evaluation (Strict All-Token Match)
# --------------------------------------------------------------------------- #

_ALNUM = "0-9a-zåäöé"
_STOPWORDS = {"the", "a", "an", "and", "of", "för", "och", "med", "till", "en", "ett"}
_TOKEN_RE = re.compile(rf"[{_ALNUM}]+(?:[-.][{_ALNUM}]+)*")


def normalize(text: str) -> str:
    return re.sub(rf"[^{_ALNUM}]", "", text.lower())


@dataclass
class Query:
    raw: str
    keywords: str
    tokens: list
    must: list
    excludes: list
    min_price: Optional[float] = None
    max_price: Optional[float] = None


def parse_query(raw: str, excludes: Optional[list] = None,
                min_price: Optional[float] = None,
                max_price: Optional[float] = None) -> Query:
    cleaned = " ".join(raw.replace("\u00a0", " ").split())
    raw_tokens = list(dict.fromkeys(_TOKEN_RE.findall(cleaned.lower())))
    raw_tokens = [t for t in raw_tokens if t not in _STOPWORDS] or raw_tokens
    tokens = [t for t in (normalize(t) for t in raw_tokens) if t]
    if not tokens:
        raise ValueError("Search query contains no usable words.")
    return Query(
        raw=cleaned,
        keywords=" ".join(raw_tokens),
        tokens=tokens,
        must=tokens,  # ALL query tokens are mandatory for strict matching
        excludes=[normalize(e) for e in (excludes or []) if normalize(e)],
        min_price=min_price,
        max_price=max_price,
    )


def relevance(query: Query, *texts: str, min_score: float = 0.5) -> Optional[float]:
    joined = " ".join(texts)
    words = re.findall(rf"[{_ALNUM}]+", joined.lower())
    if any(w.startswith(e) for e in query.excludes for w in words):
        return None
    haystack = normalize(joined)
    
    # Strict check: EVERY query token must appear in the item text
    if any(token not in haystack for token in query.must):
        return None

    score = sum(token in haystack for token in query.tokens) / len(query.tokens)
    return score if score >= min_score else None


# --------------------------------------------------------------------------- #
# Price Formatting & Conversion
# --------------------------------------------------------------------------- #

_PRICE_RE = re.compile(r"\d{1,3}(?:[ .]\d{3})+(?:[,.]\d{1,2})?|\d+(?:[,.]\d{1,2})?")


def clean_swedish_price(raw: str) -> float:
    text = raw.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").strip()
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
        raise PriceParseError(f"Could not convert {number!r} to numeric float") from exc


def _to_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", str(value)):
            number = float(value)
        else:
            number = clean_swedish_price(str(value))
    except (PriceParseError, ValueError):
        return None
    return number if number > 0 else None


def fmt_sek(value: float) -> str:
    if float(value).is_integer():
        formatted = f"{int(value):,}".replace(",", " ")
    else:
        formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} kr"


# --------------------------------------------------------------------------- #
# Web Fetcher & Browser Engines
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
    if not allowed_by_robots(url):
        raise RobotsDisallowed("disallowed by robots.txt")
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
        resp.encoding = "utf-8"
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
    if not allowed_by_robots(url):
        raise RobotsDisallowed("disallowed by robots.txt")
    try:
        page = _playwright_context().new_page()
        try:
            page.goto(url, timeout=CFG.timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
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


def fetch_and_extract(url: str, extractor: Callable[[str], object]):
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
            raise
        except FetchError as exc:
            last_error = exc
            if not use_js and CFG.js_mode == "auto":
                break
        except ExtractionError as exc:
            last_error = exc
    raise last_error


# --------------------------------------------------------------------------- #
# Product Data Extraction
# --------------------------------------------------------------------------- #

PRICE_CSS_PATTERNS = [
    '[data-testid*="price" i]',
    '[class*="product-price" i]',
    '[class*="productPrice" i]',
    '[class*="current-price" i]',
    '[class*="price" i]',
    ".price",
]


def canonicalize(url: str) -> str:
    parts = urlsplit(url)
    return f"https://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def product_id_for(url: str, store: Optional[Store]) -> str:
    path = urlsplit(url).path
    if store and store.id_re:
        match = store.id_re.search(path)
        if match:
            return hashlib.sha1(f"{store.key}:{match.group(1)}".encode()).hexdigest()[:12]
    return hashlib.sha1(canonicalize(url).encode()).hexdigest()[:12]


def _flatten_ld(node):
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
    if isinstance(offer, list):
        found = [r for r in (_offer_price(o) for o in offer) if r[0] is not None]
        return min(found, key=lambda r: r[0]) if found else (None, None)
    if not isinstance(offer, dict):
        return None, None
    if "offers" in offer:
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
    value = element.get("content") or element.get("data-price") or element.get_text(" ", strip=True)
    return _to_number(value)


def extract_product_data(html: str, name_selector: Optional[str] = None, price_selector: Optional[str] = None):
    soup = BeautifulSoup(html, "html.parser")
    name = price = currency = None

    if price_selector:
        el = soup.select_one(price_selector)
        price = _element_price(el) if el else None
    if name_selector:
        el = soup.select_one(name_selector)
        name = " ".join(el.get_text(" ", strip=True).split()) if el else None

    if price is None or name is None:
        for node in _iter_jsonld(soup):
            if not _has_type(node, "Product"):
                continue
            if name is None and isinstance(node.get("name"), str):
                name = " ".join(node["name"].split())
            if price is None:
                price, currency = _offer_price(node.get("offers"))
            if name and price is not None:
                break

    if price is None:
        el = soup.select_one('[itemprop="price"]')
        if el:
            price = _element_price(el)

    if price is None:
        price = _to_number(_meta(soup, "product:price:amount", "og:price:amount", "product:sale_price:amount"))
        currency = currency or _meta(soup, "product:price:currency", "og:price:currency")

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

    if not name:
        og = _meta(soup, "og:title")
        h1 = soup.find("h1")
        title = soup.find("title")
        for candidate in (og, h1.get_text(" ", strip=True) if h1 else None,
                          title.get_text(" ", strip=True) if title else None):
            if candidate:
                name = " ".join(candidate.split())
                break

    if price is None:
        raise ExtractionError("no price found on page")
    if not name:
        raise ExtractionError("no product name found on page")
    return name, price, (currency or "SEK").upper()


def scrape_product(url: str, store_name: str, query: Optional[Query] = None,
                   name_selector: Optional[str] = None,
                   price_selector: Optional[str] = None) -> Product:
    def extractor(html: str):
        name, price, currency = extract_product_data(html, name_selector, price_selector)
        if query and relevance(query, name, urlsplit(url).path) is None:
            raise ExtractionError(f"page '{name[:60]}' does not match search")
        if query and query.min_price is not None and price < query.min_price:
            raise ExtractionError(f"price {price:g} kr below --min-price")
        if query and query.max_price is not None and price > query.max_price:
            raise ExtractionError(f"price {price:g} kr above --max-price")
        return name, price, currency

    name, price, currency = fetch_and_extract(url, extractor)
    return Product(product_id_for(url, store_for_url(url)), store_name, name, price, canonicalize(url), currency)


# --------------------------------------------------------------------------- #
# Search & Sitemap Discovery Engine
# --------------------------------------------------------------------------- #

_ASSET_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|pdf|xml|css|js|ico)$", re.I)
CACHE_DIR = Path(".price_monitor_cache")
SITEMAP_TTL = 7 * 24 * 3600


def _is_product_like(store: Store, url: str) -> bool:
    path = urlsplit(url).path
    if store.product_re is not None:
        return bool(store.product_re.search(path))
    return path not in ("", "/") and not _ASSET_RE.search(path)


def find_ranked_links(html: str, store: Store, base_url: str, query: Query) -> list:
    soup = BeautifulSoup(html, "html.parser")
    found: dict = {}

    def add(href: str, text: str = "") -> None:
        url = canonicalize(urljoin(base_url, href))
        if not urlsplit(url).netloc.endswith(store.domain) or not _is_product_like(store, url):
            return
        entry = found.setdefault(product_id_for(url, store), {"url": url, "text": ""})
        if text and text not in entry["text"]:
            entry["text"] += " " + text

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        add(href, a.get_text(" ", strip=True)[:200])

    if store.product_re is not None:
        for match in store.product_re.finditer(html.replace("\\/", "/")):
            add(match.group(0))

    ranked = []
    for order, entry in enumerate(found.values()):
        score = relevance(query, entry["text"], urlsplit(entry["url"]).path)
        if score is not None:
            ranked.append((-score, order, entry["url"]))
    ranked.sort()
    return [url for _, _, url in ranked]


def load_store_urls(store: Store) -> list:
    cache = CACHE_DIR / f"{store.domain}_urls.txt.gz"
    if cache.exists() and time.time() - cache.stat().st_mtime < SITEMAP_TTL:
        return gzip.decompress(cache.read_bytes()).decode("utf-8").splitlines()

    log.info("[%s] Fetching sitemaps...", store.name)
    base = f"https://{store.domain}"
    allowed_by_robots(base + "/")
    parser = _ROBOTS.get(base)
    queue = list(parser.site_maps()) if parser and parser.site_maps() else [base + "/sitemap.xml"]
    
    seen: set = set()
    urls: list = []
    while queue and len(seen) < 30:
        sitemap = queue.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)
        try:
            resp = SESSION.get(sitemap, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.content
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            xml = data.decode("utf-8", errors="replace")
            locs = [htmllib.unescape(m.strip()) for m in re.findall(r"<loc>(.*?)</loc>", xml, re.S)]
            if "<sitemapindex" in xml[:2000]:
                queue.extend(locs)
            else:
                urls.extend(canonicalize(l) for l in locs if _is_product_like(store, l))
        except Exception as exc:
            log.debug("[%s] Error loading sitemap %s: %s", store.name, sitemap, exc)

    CACHE_DIR.mkdir(exist_ok=True)
    if urls:
        cache.write_bytes(gzip.compress("\n".join(urls).encode("utf-8")))
    return urls


def sitemap_links(store: Store, query: Query) -> list:
    ranked = []
    for order, url in enumerate(load_store_urls(store)):
        score = relevance(query, urlsplit(url).path, min_score=0.4)
        if score is not None:
            ranked.append((-score, order, url))
    ranked.sort()
    unique: dict = {}
    for _, _, url in ranked:
        unique.setdefault(product_id_for(url, store), url)
    return list(unique.values())


def search_store(store: Store, query: Query, max_results: int) -> list:
    try:
        if not store.search_url:
            raise ExtractionError("No search URL configured")
        search_url = store.search_url.replace("{q}", quote_plus(query.keywords))
        log.info("[%s] searching: %s", store.name, search_url)
        links = fetch_and_extract(
            search_url, lambda html: find_ranked_links(html, store, search_url, query))
    except (RobotsDisallowed, ExtractionError, FetchError) as exc:
        log.info("[%s] Search failed (%s) - attempting sitemap discovery fallback", store.name, exc)
        try:
            links = sitemap_links(store, query)
        except Exception as exc2:
            raise ExtractionError(f"search: {exc}; sitemap: {exc2}") from exc2

    products = []
    for link in links[:max_results * 3]:
        if len(products) >= max_results:
            break
        try:
            products.append(scrape_product(link, store.name, query))
        except Exception as exc:
            log.debug("[%s] skipped candidate %s: %s", store.name, link, exc)

    if not products:
        raise ExtractionError("No matching products accepted")
    return products


# --------------------------------------------------------------------------- #
# Database Management & Reporting
# --------------------------------------------------------------------------- #

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
    conn.commit()
    return conn


def get_previous_and_lowest(conn: sqlite3.Connection, product: Product):
    where = "product_id = ? OR url = ?"
    args = (product.product_id, product.url)
    prev = conn.execute(f"SELECT price FROM price_history WHERE {where} ORDER BY id DESC LIMIT 1", args).fetchone()
    low = conn.execute(f"SELECT MIN(price) FROM price_history WHERE {where}", args).fetchone()
    return (prev[0] if prev else None), (low[0] if low else None)


def save_price(conn: sqlite3.Connection, product: Product, query_text: Optional[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO price_history (url, product_name, price, scanned_at, product_id, store) VALUES (?, ?, ?, ?, ?, ?)",
        (product.url, product.name, product.price, now, product.product_id, product.store),
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


# --------------------------------------------------------------------------- #
# CLI Parser & Interactive Prompt
# --------------------------------------------------------------------------- #

def print_flag_reminder():
    print("\n" + "=" * 65)
    print("💡 HELPER FLAGS REMINDER (Pass these via command-line or interactive prompt):")
    print("  --category CAT [CAT...] : Filter shops (electronics, design, furniture, auction, general)")
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
        description="Search Swedish web shops by category and track product prices.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_argument_group("what to do")
    mode.add_argument("--search", help='Product to look for, e.g. "Sony WH-1000XM5"')
    mode.add_argument("--category", "--categories", nargs="+", help="Filter stores by category (e.g. design furniture)")
    mode.add_argument("--recheck", action="store_true", help="Re-scan all tracked products")
    mode.add_argument("--list", action="store_true", help="List tracked products")
    mode.add_argument("--check-stores", action="store_true", help="Check robots.txt status")
    mode.add_argument("--url", nargs="+", help="Track specific product page URLs")

    opt = p.add_argument_group("options")
    opt.add_argument("--stores-file", default=str(Path(__file__).with_name("stores.txt")), help="Stores file")
    opt.add_argument("--stores", nargs="+", type=str.lower, help="Only search specific stores by name")
    opt.add_argument("--max-per-store", type=int, default=10, help="Products per store (default: 10)")
    opt.add_argument("--refresh-cache", action="store_true", help="Clear sitemap/URL cache")
    opt.add_argument("--exclude", nargs="+", default=[], help="Disqualifying words (e.g. --exclude skal fodral)")
    opt.add_argument("--min-price", type=float, help="Minimum price limit in SEK")
    opt.add_argument("--max-price", type=float, help="Maximum price limit in SEK")
    opt.add_argument("--retry-blocked", action="store_true", help="Retry blocked stores")
    opt.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file path (default: {DEFAULT_DB})")
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


if __name__ == "__main__":
    sys.exit(main())