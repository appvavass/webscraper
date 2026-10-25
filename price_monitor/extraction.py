from __future__ import annotations

import hashlib
import json
import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .fetch import fetch_and_extract
from .models import ExtractionError, Product, Store, store_for_url
from .query import Query, relevance, to_number


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
        price = to_number(offer.get(key))
        if price is not None:
            return price, offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    if isinstance(spec, dict):
        price = to_number(spec.get("price"))
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
    return to_number(value)


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
        price = to_number(_meta(soup, "product:price:amount", "og:price:amount", "product:sale_price:amount"))
        currency = currency or _meta(soup, "product:price:currency", "og:price:currency")

    if price is None:
        for selector in PRICE_CSS_PATTERNS:
            for el in soup.select(selector)[:6]:
                text = el.get_text(" ", strip=True)
                if len(text) <= 40 and re.search(r"kr|:-|sek", text, re.I):
                    price = to_number(text)
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
