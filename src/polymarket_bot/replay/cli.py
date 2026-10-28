"""Offline historical replay CLI: executes deterministic replay on CP5 recorder databases."""
from __future__ import annotations

import argparse
from decimal import InvalidOperation
import json
from pathlib import Path
import sqlite3
import sys

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.store import EvidenceError
from .adapter import load_replay_recording
from .engine import run_replay
from .report import BacktestReport
from .variants import get_variant


def main(argv=None) -> int:
    """CLI entry point for offline historical replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Path to CP5 recorder SQLite database')
    parser.add_argument('--run-id', required=True, help='Recording run ID')
    parser.add_argument('--market-id', required=True, help='Market ID to replay')
    parser.add_argument('--output-dir', type=Path, required=True, help='Output directory (must not exist)')
    parser.add_argument('--legacy-scope', action='store_true', help='Opt in to recorded automatic-discovery scope for legacy market 5483041 only')
    parser.add_argument('--expected-sha256', help='Exact source file SHA256 (required with --legacy-scope)')
    parser.add_argument('--settlement-file', type=Path, help='Opt-in later settlement provenance bundle (legacy scope only)')
    parser.add_argument('--settlement-sha256', help='Exact external settlement bundle SHA256')

    # Strategy configuration
    parser.add_argument('--initial-cash', default='100', help='Initial cash in USD')
    parser.add_argument('--max-order', default='10', help='Max order size in USD')
    parser.add_argument('--max-exposure', default='10', help='Max portfolio exposure in USD')
    parser.add_argument('--fee-bps', default='10', help='Assumed fee in basis points')
    parser.add_argument('--threshold', default='0.02', help='Momentum signal threshold')
    parser.add_argument('--lookback', type=int, default=3, help='Momentum lookback slots')
    parser.add_argument('--holding-period', type=int, default=3, help='Holding period slots')
    parser.add_argument('--delay-us', type=int, default=0, help='Execution delay in microseconds')
    parser.add_argument('--max-age-us', type=int, default=5_000_000, help='Max book age in microseconds')
    parser.add_argument('--max-wait-us', type=int, default=5_000_000, help='Max wait for fill in microseconds')
    parser.add_argument('--max-positions', type=int, default=1, help='Max simultaneous positions (0 or 1)')
    parser.add_argument('--max-trades-per-window', type=int, default=6, help='Max executed trades per window')
    parser.add_argument('--max-daily-loss', default='10', help='Max daily realized loss in USD')
    parser.add_argument('--variant', help='Strategy variant name to evaluate')

    args = parser.parse_args(argv)

    created = False
    names = ('report.json', 'decisions.jsonl', 'ledger.jsonl')

    try:
        if args.output_dir.exists():
            raise ValueError(f'output directory already exists: {args.output_dir}')

        # Apply variant overrides if specified
        kwargs = {
            'initial_cash': args.initial_cash,
            'max_order': args.max_order,
            'max_exposure': args.max_exposure,
            'fee_bps': args.fee_bps,
            'threshold': args.threshold,
            'lookback': args.lookback,
            'holding_period': args.holding_period,
            'delay_us': args.delay_us,
            'max_age_us': args.max_age_us,
            'max_wait_us': args.max_wait_us,
            'max_positions': args.max_positions,
            'max_trades_per_window': args.max_trades_per_window,
            'max_daily_loss': args.max_daily_loss,
        }

        if args.variant:
            variant = get_variant(args.variant)
            kwargs.update(variant.parameters)

        # Normal loader remains strict; compatibility requires both explicit options.
        scope = None
        if (args.settlement_file is not None or args.settlement_sha256 is not None) and not args.legacy_scope:
            raise ValueError('--settlement-file and --settlement-sha256 require --legacy-scope')
        if args.legacy_scope:
            if not args.expected_sha256:
                raise ValueError('--legacy-scope requires --expected-sha256')
            from .legacy import load_legacy_replay_recording
            loaded, scope = load_legacy_replay_recording(
                args.db, args.run_id, args.market_id, expected_sha256=args.expected_sha256,
                settlement_file=args.settlement_file, settlement_sha256=args.settlement_sha256)
        else:
            if args.expected_sha256 is not None:
                raise ValueError('--expected-sha256 requires --legacy-scope')
            loaded = load_replay_recording(args.db, args.run_id, args.market_id, max_age_us=args.max_age_us)
        snapshots, excluded, opportunities, settlement_evidence, settlement_result, paper_recording = loaded

        # Run replay
        report = run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            **kwargs
        )

        if scope is not None:
            from dataclasses import replace
            report = replace(report, diagnostics={**report.diagnostics, 'legacy_scope': scope})

        # Create output directory and write files
        args.output_dir.mkdir(parents=True)
        created = True

        report_json = report.to_json()
        (args.output_dir / 'report.json').write_text(
            json.dumps(json.loads(report_json), indent=2, sort_keys=True) + '\n',
            encoding='utf8'
        )

        decisions_text = ''.join(json.dumps(d, sort_keys=True, default=str) + '\n' for d in report.decisions)
        (args.output_dir / 'decisions.jsonl').write_text(decisions_text, encoding='utf8')

        ledger_text = ''.join(json.dumps(t, sort_keys=True, default=str) + '\n' for t in report.ledger)
        (args.output_dir / 'ledger.jsonl').write_text(ledger_text, encoding='utf8')

        print(f'PHASE3B_REPLAY_SUCCESS {json.dumps(report.counts, sort_keys=True)}')
        return 0

    except (ValueError, TypeError, KeyError, InvalidOperation, OSError, AttributeError,
            sqlite3.Error, MarketDataError, EvidenceError) as exc:
        if created:
            for name in names:
                (args.output_dir / name).unlink(missing_ok=True)
            args.output_dir.rmdir()
        print(f'replay: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
