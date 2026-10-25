#!/usr/bin/env python3
"""Command-line entry point for the Swedish price monitor.

Run ``python pricemonitor.py --search "product name"`` to search, ``--web`` to
open the local database dashboard, or ``--daily`` to recheck tracked items and
send a Telegram summary. The implementation lives in ``price_monitor`` modules.
Playwright can retry recoverable fetch failures, but never overrides a
robots.txt denial or an explicit site block.

Install dependencies with ``pip install -r requirements.txt``. For
JavaScript-rendered pages, install Playwright and Chromium separately.
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
