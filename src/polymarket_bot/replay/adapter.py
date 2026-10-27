"""ReplayAdapter: CP5 recorder evidence → validated MarketSnapshot sequences with settlement.

Reads from CP5 recorder database (read-only, mode=ro, query_only=ON).
Produces validated snapshots, exclusion records, execution opportunities, and settlement evidence.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from polymarket_bot.market_data.models import (
    BookObservation, MarketDataError, MarketDescriptor,
    MarketSnapshot, TokenOutcome
)
from polymarket_bot.paper import load_recording as load_paper_recording
from polymarket_bot.recorder.store import decode_evidence
from polymarket_bot.linux_mvp.settlement import verify_evidence

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _epoch_us(moment: datetime) -> int:
    delta = moment - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _epoch_datetime(us: int) -> datetime:
    from datetime import timedelta
    return EPOCH + timedelta(microseconds=us)


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
    paper_recording: Any = None      # validated paper.Recording (real clocks; engine source of truth)


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
        self._paper_recording: Any = None

    def load(self) -> 'ReplayAdapter':
        """Load and validate all evidence. Idempotent; fails closed on any error."""
        if self._loaded:
            return self
        result = load_replay_recording(self.db, self.run_id, self.market_id, max_age_us=self.max_age_us)
        (self._snapshots, self._excluded, self._opportunities,
         self._settlement_evidence, self._settlement_result, self._paper_recording) = result
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
    def paper_recording(self):
        """The validated paper.Recording: real anchor clocks feed the replay engine."""
        if not self._loaded:
            self.load()
        return self._paper_recording

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
) -> tuple[tuple[MarketSnapshot, ...], tuple[dict, ...], tuple[dict, ...], dict | None, dict | None, Any]:
    """Load one market window from CP5 recorder DB for replay.

    Returns: (validated_snapshots, excluded_records, opportunities,
              settlement_evidence, settlement_result, paper_recording)

    The paper recording is the single source of truth for replay clocks: it
    carries the anchor-verified receipt/source times that causality and the
    staleness gate depend on. MarketSnapshots are kept for fingerprinting.
    """
    db = Path(db).resolve(strict=True)

    # Load the paper recording (does all validation)
    from polymarket_bot.strategy import load_strategy_recording
    paper_recording = load_strategy_recording(db, run_id, market_id)

    market_row = _load_market_row(db, paper_recording.market_id)
    descriptor = _market_descriptor(paper_recording, market_row)

    # Pair UP/DOWN paper snapshots into MarketSnapshots
    snapshots = _pair_paper_snapshots(paper_recording, descriptor)

    # Load settlement evidence for this market
    settlement_evidence, settlement_result = _load_settlement_evidence(db, market_row)

    return (snapshots, paper_recording.excluded, paper_recording.opportunities,
            settlement_evidence, settlement_result, paper_recording)


def load_all_windows(
    db: Path,
    *,
    max_age_us: int = 5_000_000
) -> tuple[list[MarketWindow], list[dict]]:
    """Load all completed market windows from a CP5 recording run for chronological replay.

    Returns: (windows sorted by window_start_us, skipped diagnostics).
    Data completeness failures are reported, never silently swallowed.
    """
    db = Path(db).resolve(strict=True)

    windows: list[MarketWindow] = []
    skipped: list[dict] = []
    seen_markets: set[str] = set()
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')

        runs = conn.execute(
            "SELECT run_id FROM recording_runs WHERE ended_wall_us IS NOT NULL "
            "ORDER BY started_wall_us, run_id"
        ).fetchall()

        for run in runs:
            run_id = run['run_id']
            markets = conn.execute(
                """SELECT m.* FROM markets m
                   JOIN market_revisions r ON r.market_id = m.market_id
                   WHERE r.run_id = ? AND m.lifecycle IN ('EXPIRED', 'INVALIDATED')
                   ORDER BY m.market_id""",
                (run_id,)
            ).fetchall()

            for market in markets:
                market_id = market['market_id']
                if market_id in seen_markets:
                    continue  # one window per market: the earliest completed recording run wins
                try:
                    (snapshots, excluded, opportunities, settlement_evidence,
                     settlement_result, paper_recording) = load_replay_recording(
                        db, run_id, market_id, max_age_us=max_age_us)
                    if snapshots:
                        seen_markets.add(market_id)
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
                            horizon_ns=paper_recording.horizon_ns,
                            horizon_us=paper_recording.horizon_us,
                            paper_recording=paper_recording
                        ))
                    else:
                        skipped.append(dict(market_id=market_id, run_id=run_id,
                                            reason='no validated snapshots'))
                except (ValueError, TypeError, KeyError, MarketDataError) as exc:
                    skipped.append(dict(market_id=market_id, run_id=run_id, reason=str(exc)))

    # Sort chronologically by window_start_us
    windows.sort(key=lambda w: (w.window_start_us, w.market_id))
    return windows, skipped


def _load_market_row(db: Path, market_id: str) -> dict:
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        row = conn.execute("SELECT * FROM markets WHERE market_id = ?", (market_id,)).fetchone()
    if row is None:
        raise ValueError(f'market row missing for {market_id}')
    return dict(row)


def _market_descriptor(paper_recording, row: dict) -> MarketDescriptor:
    """Real persisted identity: window bounds and metadata come from the markets row."""
    tokens = paper_recording.tokens
    return MarketDescriptor(
        event_id=row['event_id'],
        event_slug=row['slug'],
        event_title=row['question'],
        market_id=paper_recording.market_id,
        slug=row['slug'],
        question=row['question'],
        condition_id=paper_recording.condition_id,
        window_start=_epoch_datetime(row['window_start_us']),
        window_end=_epoch_datetime(row['window_end_us']),
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        up=TokenOutcome('Up', tokens['UP']),
        down=TokenOutcome('Down', tokens['DOWN']),
        metadata={},
        warnings=()
    )


def _pair_paper_snapshots(paper_recording, descriptor: MarketDescriptor | None = None) -> tuple[MarketSnapshot, ...]:
    """Pair UP/DOWN paper Snapshots into MarketSnapshot tuples by slot.

    paper.py produces one Snapshot per outcome per slot. MarketSnapshot
    requires both up and down BookObservations paired. Timestamps stay real:
    book source is the only recorded source moment.
    """
    # Group by slot_id
    groups: dict[int, dict[str, Any]] = {}
    for s in paper_recording.snapshots:
        groups.setdefault(s.slot_id, {})[s.outcome] = s

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

        market = descriptor if descriptor is not None else _fallback_descriptor(paper_recording, source_at)

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


def _fallback_descriptor(paper_recording, source_at) -> MarketDescriptor:
    tokens = paper_recording.tokens
    return MarketDescriptor(
        event_id=paper_recording.market_id,
        event_slug='',
        event_title='',
        market_id=paper_recording.market_id,
        slug='',
        question='',
        condition_id=paper_recording.condition_id,
        window_start=source_at,
        window_end=source_at,
        active=True,
        closed=False,
        accepting_orders=True,
        enable_order_book=True,
        up=TokenOutcome('Up', tokens['UP']),
        down=TokenOutcome('Down', tokens['DOWN']),
        metadata={},
        warnings=()
    )


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


def _load_settlement_evidence(db: Path, market_row: dict) -> tuple[dict | None, dict | None]:
    """Load and verify settlement evidence exactly as the live verifier shapes it.

    Builds evidence from the correct sources:
      - gamma_raw: the market's Gamma revision payload (market_revisions.gamma_raw_z),
        which carries id/conditionId/outcomes/clobTokenIds/endDate/slug/closed.
      - resolution_raw: the data-api envelope from resolution_observations.raw_resolution_z,
        filtered to this condition (the recorder batches up to 20 conditions per response).
      - window: projected to the {start_us, end_us} keys verify_evidence reads.
      - at_us: the real learning time (learned_server_us, falling back to learned_wall_us).
    Failures stay honest: the verifier returns UNKNOWN_SETTLEMENT with a diagnostic.
    """
    market_id = market_row['market_id']
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')

        observation = conn.execute(
            """SELECT * FROM resolution_observations
               WHERE market_id = ? AND classification = 'RESOLVED_DIRECTIONAL'
               ORDER BY learned_wall_us DESC LIMIT 1""",
            (market_id,)
        ).fetchone()
        if observation is None:
            return None, None

        revision = conn.execute(
            """SELECT gamma_raw_z, raw_sizes_json, codec_version FROM market_revisions
               WHERE market_id = ? ORDER BY revision_id DESC LIMIT 1""",
            (market_id,)
        ).fetchone()
        if revision is None:
            return None, None

    try:
        sizes = _raw_sizes(revision['raw_sizes_json'])
        gamma_raw = decode_evidence(
            revision['gamma_raw_z'], sizes['gamma'], revision['codec_version']).decode('utf-8')
        envelope = _decode_json_bytes(
            decode_evidence(observation['raw_resolution_z'],
                            observation['raw_size_bytes'], observation['codec_version']))
        rows = [row for row in envelope.get('data', [])
                if isinstance(row, dict) and row.get('condition_id') == market_row['condition_id']]
        at_us = observation['learned_server_us']
        if at_us is None:
            at_us = observation['learned_wall_us']
    except (KeyError, ValueError, TypeError):
        return None, None
    if not rows or at_us is None:
        return None, None

    evidence = {
        'gamma_raw': gamma_raw,
        'resolution_raw': _encode_json({'data': rows}),
        'market_id': market_row['market_id'],
        'condition_id': market_row['condition_id'],
        'window_start_us': market_row['window_start_us'],
        'window_end_us': market_row['window_end_us'],
        'at_us': int(at_us),
    }
    window = {
        'market_id': evidence['market_id'],
        'condition_id': evidence['condition_id'],
        'start_us': evidence['window_start_us'],
        'end_us': evidence['window_end_us'],
    }
    tokens = {'UP': market_row['up_token_id'], 'DOWN': market_row['down_token_id']}
    result = verify_evidence(evidence, window=window, tokens=tokens, at_us=int(at_us))
    return evidence, result


def _raw_sizes(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('invalid raw sizes')
    return value


def _decode_json_bytes(raw: bytes) -> dict:
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('invalid envelope')
    return value


def _encode_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def load_replay_recording_from_paper(
    paper_recording,
    *,
    market_descriptor: MarketDescriptor,
    settlement_evidence: dict | None = None,
    settlement_result: dict | None = None
) -> MarketWindow:
    """Create MarketWindow from paper.py Recording + MarketDescriptor + settlement."""
    snapshots = _pair_paper_snapshots(paper_recording, market_descriptor)

    return MarketWindow(
        market_id=paper_recording.market_id,
        condition_id=paper_recording.condition_id,
        event_id=market_descriptor.event_id,
        window_start_us=_epoch_us(market_descriptor.window_start),
        window_end_us=_epoch_us(market_descriptor.window_end),
        up_token_id=paper_recording.tokens['UP'],
        down_token_id=paper_recording.tokens['DOWN'],
        snapshots=snapshots,
        excluded=paper_recording.excluded,
        opportunities=paper_recording.opportunities,
        settlement_evidence=settlement_evidence,
        settlement_result=settlement_result,
        horizon_ns=paper_recording.horizon_ns,
        horizon_us=paper_recording.horizon_us,
        paper_recording=paper_recording
    )
