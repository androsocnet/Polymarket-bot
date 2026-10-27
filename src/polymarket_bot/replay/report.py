"""BacktestReport: metrics, drawdown, daily P&L, splits, determinism proof."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any


@dataclass(frozen=True)
class BacktestReport:
    """Complete backtest report with all metrics and determinism proofs."""
    schema: str = 'phase3b-backtest-v1'
    config_hash: str = ''
    source_fingerprint: str = ''
    decisions: tuple[dict, ...] = field(default_factory=tuple)
    ledger: tuple[dict, ...] = field(default_factory=tuple)
    counts: dict = field(default_factory=dict)
    cash: str = '0'
    fees_paid: str = '0'
    positions: dict = field(default_factory=dict)
    realized_pnl: str | None = None
    unrealized_pnl: str | None = None
    total_pnl: str | None = None
    max_drawdown: Decimal | None = None
    daily_pnl: dict = field(default_factory=dict)
    settlement: dict | None = None
    exit_reason_breakdown: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self):
        # Ensure tuples for immutability/hashing
        object.__setattr__(self, 'decisions', tuple(self.decisions))
        object.__setattr__(self, 'ledger', tuple(self.ledger))

    def to_json(self) -> str:
        """Serialize to canonical JSON for determinism verification."""
        data = {
            'schema': self.schema,
            'config_hash': self.config_hash,
            'source_fingerprint': self.source_fingerprint,
            'decisions': self.decisions,
            'ledger': self.ledger,
            'counts': self.counts,
            'cash': self.cash,
            'fees_paid': self.fees_paid,
            'positions': self.positions,
            'realized_pnl': self.realized_pnl,
            'unrealized_pnl': self.unrealized_pnl,
            'total_pnl': self.total_pnl,
            'max_drawdown': str(self.max_drawdown) if self.max_drawdown is not None else None,
            'daily_pnl': self.daily_pnl,
            'settlement': self.settlement,
            'exit_reason_breakdown': self.exit_reason_breakdown,
            'diagnostics': self.diagnostics,
        }
        return json.dumps(data, sort_keys=True, separators=(',', ':'))


def compute_config_hash(config: dict) -> str:
    """SHA256 of sorted config JSON for determinism proof."""
    return sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def compute_source_fingerprint(snapshots: tuple, opportunities: tuple, excluded: tuple) -> str:
    """SHA256 of canonical recorder rows used."""
    canonical = {
        'snapshots': tuple(_snapshot_fingerprint(s) for s in snapshots),
        'opportunities': opportunities,
        'excluded': excluded,
    }
    return sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _snapshot_fingerprint(snapshot) -> dict:
    """Extract stable fingerprint from MarketSnapshot."""
    return {
        'market_id': snapshot.market.market_id,
        'condition_id': snapshot.market.condition_id,
        'up_token': snapshot.up.outcome.token_id,
        'down_token': snapshot.down.outcome.token_id,
        'up_book': _book_fingerprint(snapshot.up.book),
        'down_book': _book_fingerprint(snapshot.down.book),
        'validated_at': snapshot.validated_at.isoformat(),
    }


def _book_fingerprint(book) -> dict:
    """Stable fingerprint of an OutcomeBook."""
    return {
        'token_id': book.token_id,
        'condition_id': book.condition_id,
        'source_timestamp_raw': book.source_timestamp_raw,
        'best_bid': str(book.best_bid) if book.best_bid is not None else None,
        'best_ask': str(book.best_ask) if book.best_ask is not None else None,
        'bid_levels': [(str(l.price), str(l.size)) for l in book.bids],
        'ask_levels': [(str(l.price), str(l.size)) for l in book.asks],
    }


@dataclass(frozen=True)
class ChronologicalSplit:
    """Result of chronological splitting with leakage prevention and purging."""
    train: tuple[dict, ...] = field(default_factory=tuple)
    val: tuple[dict, ...] = field(default_factory=tuple)
    test: tuple[dict, ...] = field(default_factory=tuple)
    purged: tuple[dict, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, 'train', tuple(self.train))
        object.__setattr__(self, 'val', tuple(self.val))
        object.__setattr__(self, 'test', tuple(self.test))
        object.__setattr__(self, 'purged', tuple(self.purged))


def split_windows(
    windows: list[dict],
    *,
    split_us: int | None = None,
    train_end_us: int | None = None,
    val_end_us: int | None = None,
) -> ChronologicalSplit:
    """Split windows chronologically into train, validation, and test partitions.

    Leakage prevention rules:
    - Overlapping windows spanning any split boundary are purged.
    - Windows ending before boundary but resolving after boundary are purged from earlier split.
    - Test partition remains untouched and held out.
    """
    if split_us is not None:
        t_boundary = split_us
        v_boundary = None
    elif train_end_us is not None:
        t_boundary = train_end_us
        v_boundary = val_end_us
    else:
        raise ValueError('Either split_us or train_end_us must be provided')

    train, val, test, purged = [], [], [], []

    for w in windows:
        start = w['start_us']
        end = w['end_us']
        resolved = w.get('resolved_us', end)

        if v_boundary is not None:
            # 3-way split: train < t_boundary <= val < v_boundary <= test
            # Check overlap across t_boundary
            if start < t_boundary < end or (end <= t_boundary and resolved > t_boundary):
                purged.append(w)
            elif end <= t_boundary:
                train.append(w)
            # Check overlap across v_boundary
            elif start < v_boundary < end or (end <= v_boundary and resolved > v_boundary):
                purged.append(w)
            elif t_boundary <= start and end <= v_boundary:
                val.append(w)
            elif v_boundary <= start:
                test.append(w)
            else:
                purged.append(w)
        else:
            # 2-way split: train < t_boundary <= test
            if start < t_boundary < end or (end <= t_boundary and resolved > t_boundary):
                purged.append(w)
            elif end <= t_boundary:
                train.append(w)
            elif t_boundary <= start:
                test.append(w)
            else:
                purged.append(w)

    return ChronologicalSplit(
        train=tuple(train),
        val=tuple(val),
        test=tuple(test),
        purged=tuple(purged)
    )