"""Phase 1 ingest-only cycle: store public top-of-book evidence. No decisions or orders."""
from __future__ import annotations

from dataclasses import dataclass
import time

from ..market_data.models import MarketDataError
from .config import Settings
from .store import MvpStore

UP, DOWN = 'UP', 'DOWN'


@dataclass(frozen=True)
class IngestResult:
    status: str
    market_id: str | None = None
    slot_index: int | None = None
    inserted: int = 0
    reason: str = ''


def _us(moment) -> int:
    return int(moment.timestamp() * 1_000_000)


class IngestEngine:
    """One causal ingest cycle over an injected collector.

    It persists windows and both top-of-book observations. It never writes decisions,
    orders, positions, or equity, and never infers a settlement.
    """

    def __init__(self, store: MvpStore, collector, settings: Settings, *, run_id: str,
                 now_us=None):
        self.store = store
        self.collector = collector
        self.settings = settings
        self.run_id = run_id
        self.bot_id = settings.bot_id
        self.now_us = now_us or (lambda: time.time_ns() // 1000)
        self.consecutive_errors = 0
        self.cycles = 0

    def step(self) -> IngestResult:
        at = self.now_us()
        self.cycles += 1
        heartbeat = self.store.heartbeat_row(self.bot_id) or {}

        if (heartbeat.get('status') == 'FAILED_STOP'
                and heartbeat.get('run_id') == self.run_id):
            return IngestResult('FAILED_STOP', reason=heartbeat.get('last_error') or 'FAILED_STOP')

        if self.settings.stop_path.exists():
            if heartbeat.get('status') != 'STOPPED':
                self.store.event(self.bot_id, self.run_id, at_us=at, kind='emergency_stop',
                                 detail=str(self.settings.stop_path))
                self._beat(at, heartbeat.get('market_id'), 'STOPPED', None)
            return IngestResult('STOPPED', reason='EMERGENCY_STOP')

        try:
            snapshot = self.collector.collect()
        except MarketDataError as exc:
            self.consecutive_errors += 1
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='data_error',
                             detail=f'{exc.reason}: {str(exc)[:300]}')
            status = ('FAILED_STOP' if self.consecutive_errors >= self.settings.max_consecutive_errors
                      else 'ERROR')
            self._beat(at, heartbeat.get('market_id'), status, exc.reason)
            return IngestResult(status, reason=exc.reason)
        self.consecutive_errors = 0

        market = snapshot.market
        start_us, end_us = _us(market.window_start), _us(market.window_end)
        validated = _us(snapshot.validated_at)
        active = self.store.active_window(self.bot_id)

        if active is not None and active['market_id'] == market.market_id and validated >= active['end_us']:
            self._abandon(active, at, 'ABORTED', 'window ended while active', 'window_expired')
            self._beat(at, market.market_id, 'RUNNING', None)
            return IngestResult('WINDOW_EXPIRED', market.market_id, reason='WINDOW_EXPIRED')

        if active is None:
            if self.store.ensure_window(self.bot_id, self.run_id, market_id=market.market_id,
                                        condition_id=market.condition_id, event_id=market.event_id,
                                        start_us=start_us, end_us=end_us, at_us=at):
                self.store.event(self.bot_id, self.run_id, at_us=at, kind='window_start',
                                 detail=market.market_id)
            else:
                return self._conflict(at, market.market_id, heartbeat)
        elif active['market_id'] != market.market_id:
            # Insert the successor before closing the predecessor. If the discovered market is
            # already a closed/abandoned window, leave the active window untouched.
            with self.store.transaction():
                inserted = self.store.ensure_window(
                    self.bot_id, self.run_id, market_id=market.market_id,
                    condition_id=market.condition_id, event_id=market.event_id,
                    start_us=start_us, end_us=end_us, at_us=at)
                if inserted:
                    self.store.close_window(self.bot_id, active['market_id'],
                                            'UNKNOWN_SETTLEMENT', at_us=at,
                                            note=f'rollover to {market.market_id}')
            if not inserted:
                return self._conflict(at, market.market_id, heartbeat)
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='rollover',
                             detail=f'{active["market_id"]}:rollover to {market.market_id}')
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='window_start',
                             detail=market.market_id)

        if not start_us <= validated < end_us:
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='out_of_window',
                             detail=f'{validated} not in [{start_us},{end_us})')
            self._beat(at, market.market_id, 'RUNNING', 'OUT_OF_WINDOW')
            return IngestResult('OUT_OF_WINDOW', market.market_id, reason='OUT_OF_WINDOW')

        slot_index = int((validated - start_us) // (self.settings.sample_seconds * 1_000_000))
        inserted = 0
        for outcome, observation in ((UP, snapshot.up), (DOWN, snapshot.down)):
            book = observation.book
            if self.store.observe(
                    self.bot_id, self.run_id, market.market_id, slot_index, outcome,
                    observation.outcome.token_id, book.best_bid, book.best_ask, book.midpoint,
                    _us(book.source_at), _us(observation.request_finished_at)):
                inserted += 1

        self._beat(at, market.market_id, 'RUNNING', None)
        return IngestResult('OK' if inserted else 'DUPLICATE_SLOT', market.market_id,
                            slot_index, inserted)

    def _conflict(self, at: int, market_id: str, heartbeat: dict) -> IngestResult:
        """A discovered window is closed/abandoned; keep the active window and retry discovery."""
        if heartbeat.get('last_error') != 'WINDOW_CONFLICT':
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='window_conflict',
                             detail=market_id)
        self._beat(at, market_id, 'RUNNING', 'WINDOW_CONFLICT')
        return IngestResult('WINDOW_CONFLICT', market_id, reason='WINDOW_CONFLICT')

    def _abandon(self, window: dict, at: int, status: str, note: str, kind: str) -> None:
        self.store.close_window(self.bot_id, window['market_id'], status, at_us=at, note=note)
        self.store.event(self.bot_id, self.run_id, at_us=at, kind=kind,
                         detail=f'{window["market_id"]}:{note}')

    def _beat(self, at: int, market_id: str | None, status: str, error: str | None) -> None:
        self.store.heartbeat(self.bot_id, self.run_id, at_us=at, market_id=market_id,
                             status=status, cycles=self.cycles, last_error=error)
