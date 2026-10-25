from __future__ import annotations

import atexit
import hashlib
import importlib
import random
import time
from typing import Callable, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import CFG, HEADERS, ExtractionError, FetchBlocked, FetchError, RobotsDisallowed


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
        raise FetchBlocked(f"blocked or rate-limited (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        if resp.status_code < 500:
            raise FetchBlocked(f"HTTP {resp.status_code}")
        raise FetchError(f"HTTP {resp.status_code}")

    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "utf-8"
    html = resp.text
    if len(html) < 30000 and any(m in html.lower() for m in _BLOCK_MARKERS):
        raise FetchBlocked("bot-protection page returned")
    return html


_PW: dict = {"pw": None, "browser": None, "context": None}


def playwright_available() -> bool:
    try:
        importlib.import_module("playwright.sync_api")
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
            if not allowed_by_robots(url):
                raise RobotsDisallowed("disallowed by robots.txt")
            _polite_delay()
            html = fetch_playwright(url) if use_js else fetch_requests(url)
            _dump_html(url, html, use_js)
            result = extractor(html)
            if result:
                return result
            last_error = ExtractionError("nothing usable found")
        except RobotsDisallowed:
            raise
        except FetchBlocked:
            raise
        except FetchError as exc:
            last_error = exc
        except ExtractionError as exc:
            last_error = exc
    raise last_error

atexit.register(close_browser)
