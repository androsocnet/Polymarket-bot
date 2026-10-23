"""Production collector: the accepted public snapshot path. No authenticated or order calls."""
from __future__ import annotations

from datetime import datetime, timezone
import time

import httpx

from ..market_data.clob import ClobClient
from ..market_data.gamma import GammaClient
from ..market_data.http import PublicGet
from ..market_data.snapshot import collect_snapshot


class CollectionStopped(Exception):
    """The service asked the collector to abort between bounded HTTP phases."""


class LiveCollector:
    """Composes the accepted discovery/time/book stack into one MarketSnapshot per cycle."""

    def __init__(self, client: httpx.Client | None = None, *, monotonic=time.monotonic,
                 should_stop=None):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0))
        self.monotonic = monotonic
        self.should_stop = should_stop
        public = PublicGet(self.client, monotonic=monotonic, sleep=time.sleep,
                           utcnow=lambda: datetime.now(timezone.utc),
                           before_attempt=self._before_attempt)
        self.gamma = GammaClient(public)
        self.clob = ClobClient(public)

    def set_stop_check(self, check) -> None:
        self.should_stop = check

    def _before_attempt(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise CollectionStopped('stop requested')

    def collect(self):
        return collect_snapshot(self.gamma, self.clob, monotonic=self.monotonic)

    def get_resolution(self, condition_id: str, *, deadline: float | None = None) -> dict | None:
        from .settlement import fetch_resolution
        dl = deadline if deadline is not None else (self.monotonic() + 5.0)
        return fetch_resolution(self.gamma.public, condition_id, deadline=dl)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
