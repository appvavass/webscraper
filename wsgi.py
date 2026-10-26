"""Production WSGI entry point for the Price Monitor web dashboard."""

import os

from price_monitor.models import DEFAULT_DB
from price_monitor.web import create_app


app = create_app(os.environ.get("PRICE_MONITOR_DB", DEFAULT_DB))
