#!/usr/bin/env python3
"""Command-line entry point for the Swedish price monitor.

Run ``python pricemonitor.py --search "product name"``. The implementation
lives in ``price_monitor`` modules. Playwright can retry recoverable fetch
failures, but never overrides a robots.txt denial or an explicit site block.

Install core dependencies with ``pip install requests beautifulsoup4``.
For JavaScript-rendered pages, install Playwright and Chromium separately.
"""

import sys

from price_monitor.models import *  # noqa: F401,F403
from price_monitor.query import *  # noqa: F401,F403
from price_monitor.fetch import *  # noqa: F401,F403
from price_monitor.extraction import *  # noqa: F401,F403
from price_monitor.search import *  # noqa: F401,F403
from price_monitor.storage import *  # noqa: F401,F403
from price_monitor.cli import *  # noqa: F401,F403


if __name__ == "__main__":
    sys.exit(main())
