"""ReplayAdapter: CP5 recorder evidence → validated MarketSnapshot sequences with settlement.

Reads from CP5 recorder database (read-only, mode=ro, query_only=ON).
Produces validated snapshots, exclusion records, execution opportunities, and settlement evidence.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any

from polymarket_bot.market_data.clob import parse_book
from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.models import (
    BookLevel, BookObservation, MarketDataError, MarketDescriptor,
    MarketSnapshot, OutcomeBook, TokenOutcome
)
from polymarket_bot.paper import load_recording as load_paper_recording
from polymarket_bot.recorder.store import decode_evidence
from polymarket_bot.linux_mvp.settlement import verify_evidence


@dataclass(frozen=True)
class MarketWindow:
    """One 15-minute BTC Up/Down market window with all replay data."""
    market_id: str
    condition_id: str
    event_id: str
    window_start_us: int
    window_end_us: int
    up_token_id: str
    down_token_id: str
    snapshots: tuple[MarketSnapshot, ...]
    excluded: tuple[dict, ...]
    opportunities: tuple[dict, ...]
    settlement_evidence: dict | None  # None if no evidence, else raw evidence dict
    settlement_result: dict | None   # Result from verify_evidence
    horizon_ns: int
    horizon_us: int


class ReplayAdapter:
    """Read-only adapter over CP5 recorder evidence for historical replay.

    Loads a single run/market and its settlement evidence. All validation
    (identity, staleness, one-sided, missing) is delegated to paper.load_recording.
    """

    def __init__(self, db: Path, run_id: str, market_id: str, *, max_age_us: int = 5_000_000):
        self.db = Path(db).resolve(strict=True)
        self.run_id = run_id
        self.market_id = market_id
        self.max_age_us = max_age_us
        self._loaded = False
        self._snapshots: tuple[MarketSnapshot, ...] = ()
        self._excluded: tuple[dict, ...] = ()
        self._opportunities: tuple[dict, ...] = ()
        self._settlement_evidence: dict | None = None
        self._settlement_result: dict | None = None

    def load(self) -> 'ReplayAdapter':
        """Load and validate all evidence. Idempotent; fails closed on any error."""
        if self._loaded:
            return self
        self._snapshots, self._excluded, self._opportunities, self._settlement_evidence, self._settlement_result = (
            load_replay_recording(self.db, self.run_id, self.market_id, max_age_us=self.max_age_us)
        )
        self._loaded = True
        return self

    @property
    def snapshots(self) -> tuple[MarketSnapshot, ...]:
        if not self._loaded:
            self.load()
        return self._snapshots

    @property
    def excluded(self) -> tuple[dict, ...]:
        if not self._loaded:
            self.load()
        return self._excluded

    @property
    def opportunities(self) -> tuple[dict, ...]:
        if not self._loaded:
            self.load()
        return self._opportunities

    @property
    def settlement_evidence(self) -> dict | None:
        if not self._loaded:
            self.load()
        return self._settlement_evidence

    @property
    def settlement_result(self) -> dict | None:
        if not self._loaded:
            self.load()
        return self._settlement_result

    @property
    def db_sha256(self) -> str:
        from hashlib import sha256
        return sha256(self.db.read_bytes()).hexdigest()


def load_replay_recording(
    db: Path,
    run_id: str,
    market_id: str,
    *,
    max_age_us: int = 5_000_000
) -> tuple[tuple[MarketSnapshot, ...], tuple[dict, ...], tuple[dict, ...], dict | None, dict | None]:
    """Load one market window from CP5 recorder DB for replay.

    Returns: (validated_snapshots, excluded_records, opportunities, settlement_evidence, settlement_result)
    """
    db = Path(db).resolve(strict=True)

    # Load the paper recording (does all validation)
    from polymarket_bot.strategy import load_strategy_recording
    paper_recording = load_strategy_recording(db, run_id, market_id)

    # Pair UP/DOWN paper snapshots into MarketSnapshots
    snapshots = _pair_paper_snapshots(paper_recording)

    # Load settlement evidence for this market
    settlement_evidence, settlement_result = _load_settlement_evidence(db, market_id)

    return snapshots, paper_recording.excluded, paper_recording.opportunities, settlement_evidence, settlement_result


def load_all_windows(
    db: Path,
    *,
    max_age_us: int = 5_000_000
) -> list[MarketWindow]:
    """Load all completed market windows from a CP5 recording run for chronological replay."""
    db = Path(db).resolve(strict=True)
    
    windows = []
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        
        # Find all STOPPED/COMPLETED runs
        runs = conn.execute(
            "SELECT run_id FROM recording_runs WHERE state IN ('STOPPED', 'FAILED', 'INTERRUPTED') AND ended_wall_us IS NOT NULL"
        ).fetchall()
        
        for run in runs:
            run_id = run['run_id']
            markets = conn.execute(
                "SELECT * FROM markets WHERE lifecycle IN ('EXPIRED', 'CURRENT', 'INVALIDATED')"
            ).fetchall()
            
            for market in markets:
                market_id = market['market_id']
                try:
                    snapshots, excluded, opportunities, settlement_evidence, settlement_result = load_replay_recording(
                        db, run_id, market_id, max_age_us=max_age_us
                    )
                    if snapshots:  # Only include windows with valid data
                        windows.append(MarketWindow(
                            market_id=market_id,
                            condition_id=market['condition_id'],
                            event_id=market['event_id'],
                            window_start_us=market['window_start_us'],
                            window_end_us=market['window_end_us'],
                            up_token_id=market['up_token_id'],
                            down_token_id=market['down_token_id'],
                            snapshots=snapshots,
                            excluded=excluded,
                            opportunities=opportunities,
                            settlement_evidence=settlement_evidence,
                            settlement_result=settlement_result,
                            horizon_ns=max(s.available_ns for s in snapshots) if snapshots else 0,
                            horizon_us=int((max(s.available_ns for s in snapshots) if snapshots else 0) / 1000)
                        ))
                except (ValueError, TypeError, KeyError, MarketDataError):
                    # Skip windows that fail to load
                    continue
    
    # Sort chronologically by window_start_us
    windows.sort(key=lambda w: w.window_start_us)
    return windows


def _pair_paper_snapshots(paper_recording) -> tuple[MarketSnapshot, ...]:
    """Pair UP/DOWN paper Snapshots into MarketSnapshot tuples by slot.

    paper.py produces one Snapshot per outcome per slot. MarketSnapshot
    requires both up and down BookObservations paired.
    """
    from datetime import datetime, timezone

    # Group by slot_id
    groups: dict[int, dict[str, Any]] = {}
    for s in paper_recording.snapshots:
        groups.setdefault(s.slot_id, {})[s.outcome] = s

    # Build market descriptor from paper recording metadata
    tokens = paper_recording.tokens
    up_token = tokens.get('UP', '')
    down_token = tokens.get('DOWN', '')

    # Use horizon for window bounds approximation
    condition_id = paper_recording.condition_id

    snapshots: list[MarketSnapshot] = []
    for slot_id in sorted(groups):
        pair = groups[slot_id]
        up_snap = pair.get('UP')
        down_snap = pair.get('DOWN')
        if up_snap is None or down_snap is None:
            continue  # One-sided: skip (already tracked in excluded)

        up = _make_observation(up_snap)
        down = _make_observation(down_snap)

        # Both books share the same market context
        source_at = up.book.source_at
        validated_at = source_at  # Use source as validated for replay

        market = MarketDescriptor(
            event_id=paper_recording.market_id,
            event_slug='',
            event_title='',
            market_id=paper_recording.market_id,
            slug='',
            question='',
            condition_id=condition_id,
            window_start=source_at,
            window_end=source_at,
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            up=TokenOutcome('Up', up_token),
            down=TokenOutcome('Down', down_token),
            metadata={},
            warnings=()
        )

        snapshots.append(MarketSnapshot(
            market=market,
            discovery_source='recorder',
            up=up,
            down=down,
            collection_started_at=source_at,
            collection_finished_at=source_at,
            validated_at=validated_at,
            time_round_trip_seconds=0.0,
            rolled_over=False
        ))

    return tuple(snapshots)


def _make_observation(paper_snap):
    """Create a BookObservation from a paper.py Snapshot."""
    book = paper_snap.book
    token = TokenOutcome(label=paper_snap.outcome, token_id=book.token_id)
    source_at = book.source_at
    return BookObservation(
        outcome=token,
        book=book,
        request_started_at=source_at,
        request_finished_at=source_at
    )


def _load_settlement_evidence(db: Path, market_id: str) -> tuple[dict | None, dict | None]:
    """Load settlement evidence from resolution_observations table."""
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        
        # Get the latest RESOLVED_DIRECTIONAL observation for this market
        row = conn.execute(
            """SELECT * FROM resolution_observations 
               WHERE market_id = ? AND classification = 'RESOLVED_DIRECTIONAL'
               ORDER BY learned_wall_us DESC LIMIT 1""",
            (market_id,)
        ).fetchone()
        
        if row is None:
            return None, None
        
        evidence = {
            'gamma_raw': decode_evidence(row['raw_resolution_z'], row['raw_size_bytes'], row['codec_version']),
            'resolution_raw': row['payouts_raw_json']
        }
        
        # Get window info for verification
        window_row = conn.execute(
            "SELECT * FROM markets WHERE market_id = ?", (market_id,)
        ).fetchone()
        
        if window_row is None:
            return evidence, None
        
        window = dict(window_row)
        tokens = {
            'UP': window['up_token_id'],
            'DOWN': window['down_token_id']
        }
        
        # Verify the evidence
        result = verify_evidence(evidence, window=window, tokens=tokens, at_us=window['window_end_us'])
        return evidence, result


def load_replay_recording_from_paper(
    paper_recording,
    *,
    market_descriptor: MarketDescriptor,
    settlement_evidence: dict | None = None,
    settlement_result: dict | None = None
) -> MarketWindow:
    """Create MarketWindow from paper.py Recording + MarketDescriptor + settlement."""
    snapshots = _pair_paper_snapshots(paper_recording)

    # Fix market descriptor on each snapshot
    fixed_snapshots = []
    for s in snapshots:
        fixed_snapshots.append(MarketSnapshot(
            market=market_descriptor,
            discovery_source=s.discovery_source,
            up=s.up,
            down=s.down,
            collection_started_at=s.collection_started_at,
            collection_finished_at=s.collection_finished_at,
            validated_at=s.validated_at,
            time_round_trip_seconds=s.time_round_trip_seconds,
            rolled_over=s.rolled_over
        ))

    return MarketWindow(
        market_id=paper_recording.market_id,
        condition_id=paper_recording.condition_id,
        event_id=market_descriptor.event_id,
        window_start_us=int(market_descriptor.window_start.timestamp() * 1_000_000),
        window_end_us=int(market_descriptor.window_end.timestamp() * 1_000_000),
        up_token_id=paper_recording.tokens['UP'],
        down_token_id=paper_recording.tokens['DOWN'],
        snapshots=tuple(fixed_snapshots),
        excluded=paper_recording.excluded,
        opportunities=paper_recording.opportunities,
        settlement_evidence=settlement_evidence,
        settlement_result=settlement_result,
        horizon_ns=paper_recording.horizon_ns,
        horizon_us=paper_recording.horizon_us
    )