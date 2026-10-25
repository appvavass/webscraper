from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .models import PriceParseError


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
    
    # Core mandatory words: 'samsung' and 'frame' must be present, 
    # but we allow size dimensions (like 55, 65, 75, 85) to appear between them.
    return Query(
        raw=cleaned,
        keywords=" ".join(raw_tokens),
        tokens=tokens,
        must=tokens,
        excludes=[normalize(e) for e in (excludes or []) if normalize(e)],
        min_price=min_price,
        max_price=max_price,
    )


def relevance(query: Query, *texts: str, min_score: float = 0.4) -> Optional[float]:
    joined = " ".join(texts)
    words = re.findall(rf"[{_ALNUM}]+", joined.lower())
    if any(w.startswith(e) for e in query.excludes for w in words):
        return None
    
    haystack = normalize(joined)
    
    # Ensure core anchor words ('samsung' and 'frame') are present anywhere in the title/URL
    core_anchors = [t for t in query.must if t in ("samsung", "frame")]
    
    if any(anchor not in haystack for anchor in core_anchors):
        return None
        
    # For other tokens, allow flexible matching
    matched_count = sum(token in haystack for token in query.tokens)
    score = matched_count / len(query.tokens)
    
    return score if score >= min_score else None


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


def to_number(value) -> Optional[float]:
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
