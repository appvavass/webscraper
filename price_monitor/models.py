from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


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


KNOWN = {
    "elgiganten.se": dict(
        key="Elgiganten",
        search_url="https://www.elgiganten.se/search?q={q}",
        product_re=re.compile(r"/product/" + _LINK),
        id_re=re.compile(r"/product/[^/]+/(\d+)")
    ),
    "blocket.se": dict(
        key="Blocket",
        search_url="https://www.blocket.se/recommerce/forsale/search?q={q}",
        product_re=re.compile(r"/recommerce/forsale/item/\d+"),
        id_re=re.compile(r"/recommerce/forsale/item/(\d+)"),
    ),
    "facebook.com": dict(
        key="Facebook Marketplace",
        search_url="https://www.facebook.com/marketplace/search/?query={q}",
        product_re=re.compile(r"/marketplace/item/\d+"),
        id_re=re.compile(r"/marketplace/item/(\d+)"),
    ),
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
Blocket - https://www.blocket.se # secondhand
# Facebook Marketplace search requires sign-in and is blocked by robots.txt; the CLI prints a manual search link.
Facebook Marketplace - https://www.facebook.com # secondhand
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
    for line in path.read_text(encoding="utf-8-sig").splitlines():
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


class FetchError(Exception):
    """Page download error."""


class FetchBlocked(FetchError):
    """The site explicitly rejected the request or returned a challenge page."""


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

log = logging.getLogger("price_monitor")
