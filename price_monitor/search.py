from __future__ import annotations

import gzip
import html as htmllib
import re
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit

from bs4 import BeautifulSoup

from .extraction import canonicalize, product_id_for, scrape_product
from .fetch import SESSION, _ROBOTS, allowed_by_robots, fetch_and_extract
from .models import ExtractionError, FetchBlocked, FetchError, RobotsDisallowed, Store, log
from .query import Query, relevance


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
        text = a.get_text(" ", strip=True)
        card = a.find_parent("article")
        if card:
            text = f"{text} {card.get_text(' ', strip=True)}"
        add(href, text[:1000])

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
    search_url = ""
    try:
        if not store.search_url:
            raise ExtractionError("No search URL configured")
        search_url = store.search_url.replace("{q}", quote_plus(query.keywords))
        log.info("[%s] searching: %s", store.name, search_url)
        links = fetch_and_extract(
            search_url, lambda html: find_ranked_links(html, store, search_url, query))
    except RobotsDisallowed as exc:
        raise ExtractionError(
            f"Search is disallowed by robots.txt; open this URL manually: {search_url}"
        ) from exc
    except FetchBlocked as exc:
        raise ExtractionError(
            f"Search was rejected by the site ({exc}); not retrying with a browser or sitemap"
        ) from exc
    except (ExtractionError, FetchError) as exc:
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
