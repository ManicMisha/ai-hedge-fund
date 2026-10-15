"""Which market-data source a run uses.

    DATA_SOURCE=financialdatasets   paid: prices + point-in-time fundamentals
    DATA_SOURCE=yahoo               free: daily prices only
    (unset)                         Financial Datasets if its key is set, else Yahoo

`open_client()` returns the raw client for the active source and
`with_cache()` adds the disk cache where the source needs one (Yahoo keeps
its own). The CLI and the terminal app import these under the names
FDClient / CachedDataClient so every existing call site, and the tests that
replace them, keep working unchanged.
"""

from __future__ import annotations

import os

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient
from hedge_fund.data.yahoo import YahooClient

DATA_KEY = "FINANCIAL_DATASETS_API_KEY"
SOURCES = ("financialdatasets", "yahoo")
LABELS = {"financialdatasets": "Financial Datasets", "yahoo": "Yahoo Finance (free, prices only)"}


def data_source() -> str:
    explicit = os.environ.get("DATA_SOURCE", "").strip().lower()
    if explicit in SOURCES:
        return explicit
    return "financialdatasets" if os.environ.get(DATA_KEY) else "yahoo"


def needs_data_key() -> bool:
    """True when the active source is Financial Datasets and its key is missing."""
    return data_source() == "financialdatasets" and not os.environ.get(DATA_KEY)


def open_client():
    """The raw client for the active source; use as a context manager."""
    return FDClient() if data_source() == "financialdatasets" else YahooClient()


def with_cache(raw):
    """Wrap *raw* in the disk cache unless it caches for itself."""
    return raw if isinstance(raw, YahooClient) else CachedDataClient(raw)
