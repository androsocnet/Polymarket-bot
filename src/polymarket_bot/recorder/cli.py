"""Explicit book-only recorder and offline measurement commands."""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Sequence

import httpx

from polymarket_bot.market_data.http import PublicGet
from .metrics import measure
from .models import RunConfig
from .service import Recorder
from .store import RecorderStore, RecorderStoreError


def _utc_us(value: str) -> int:
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an ISO 8601 UTC time.") from exc
    if instant.tzinfo is None or instant.utcoffset().total_seconds() != 0:
        raise argparse.ArgumentTypeError("Expected a timezone-aware UTC time.")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = instant - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _wall_us() -> int:
    now = datetime.now(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (now.days * 86400 + now.seconds) * 1_000_000 + now.microseconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Public BTC market recorder; reference runtime disabled.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    record = subcommands.add_parser("record", help="Record public books at fixed two-second slots.")
    record.add_argument("--mode", required=True, choices=("book-only", "reference-required"))
    record.add_argument("--db", required=True, type=Path)
    record.add_argument("--no-fast-path", action="store_true")
    report = subcommands.add_parser("report", help="Measure an existing database without writes or APIs.")
    report.add_argument("--db", required=True, type=Path)
    report.add_argument("--start", required=True, type=_utc_us, metavar="UTC")
    report.add_argument("--end", required=True, type=_utc_us, metavar="UTC")
    return parser


def _record(path: Path, use_fast_path: bool) -> int:
    print("BOOK_ONLY / REFERENCE_INCOMPLETE: public book evidence; no verified live reference coverage.")
    store = None
    try:
        store = RecorderStore.open(path)
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            public = PublicGet(client, monotonic=time.monotonic, monotonic_ns=time.monotonic_ns,
                               sleep=time.sleep, utcnow=lambda: datetime.now(timezone.utc),
                               max_attempts=2)
            recorder = Recorder(store, public, monotonic_ns=time.monotonic_ns,
                                wall_us=_wall_us,
                                sleep=time.sleep)
            result = recorder.run(RunConfig(use_fast_path=use_fast_path), should_stop=lambda: False)
        print(f"run_id={result.run_id} state={result.state} slots_finalized={result.slots_finalized} reason={result.reason}")
        return result.exit_code
    except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
        print(f"recorder_error: {exc.code if isinstance(exc, RecorderStoreError) else 'storage_failure'}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


def _report(path: Path, start_us: int, end_us: int) -> int:
    if end_us <= start_us:
        print("report_error: end must be after start", file=sys.stderr)
        return 2
    try:
        store = RecorderStore.read_only(path)
        try:
            result = measure(store, start_us=start_us, end_us=end_us)
        finally:
            store.close()
    except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
        print(f"report_error: {exc.code if isinstance(exc, RecorderStoreError) else 'storage_failure'}", file=sys.stderr)
        return 1
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    format_utc = lambda us: (epoch + timedelta(microseconds=us)).isoformat().replace("+00:00", "Z")
    document = asdict(result)
    document["interval_utc"] = {"start": format_utc(start_us), "end": format_utc(end_us)}
    document["units"] = {"elapsed_seconds": "seconds", "physical_growth_bytes": "bytes",
                         "mb_per_day": "decimal MB/day", "gb_per_30_days": "decimal GB/30 days",
                         "bytes_per_terminal_slot": "bytes/new terminal slot",
                         "bytes_per_complete_slot": "bytes/new COMPLETE book slot",
                         "average_depth": "returned bid+ask levels/VALID book",
                         "latency_percentiles": "nanoseconds per request attempt",
                         "status_percentages": "percent of eligible scheduled slots",
                         "reference_coverage": "percent of eligible scheduled slots; original live associations",
                         "rollover_gaps": "seconds in calibrated server time"}
    document["denominator_labels"] = {"status_percentages": "eligible_scheduled_slots",
                                      "reference_coverage": "eligible_scheduled_slots; live original association only",
                                      "bytes_per_terminal_slot": "physical_terminal_slots",
                                      "bytes_per_complete_slot": "physical_complete_slots"}
    print(json.dumps(document, sort_keys=True, indent=2, default=lambda obj: dict(obj) if isinstance(obj, sqlite3.Row) else str(obj)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "record":
        if args.mode == "reference-required":
            print("activation_required: verified Chainlink reference runtime is unavailable.", file=sys.stderr)
            return 2
        return _record(args.db, not args.no_fast_path)
    return _report(args.db, args.start, args.end)
