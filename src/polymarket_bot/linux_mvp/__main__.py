"""CLI: `ingest` (Phase 1), `run` (paper engine), and `report` (read-only summary)."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import signal
import sqlite3
import sys

from .collector import LiveCollector
from .config import ConfigError, Settings
from .ingest import IngestEngine
from .report import build_report, render
from .service import InstanceLockError, Service, single_instance
from .store import MvpStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='polymarket_bot.linux_mvp',
                                     description='PAPER_ONLY BTC 15-minute paper bot')
    sub = parser.add_subparsers(dest='command', required=True)
    for name, help_text in (('ingest', 'run the Phase 1 ingest-only service until stopped'),
                            ('run', 'run the paper service until stopped')):
        command = sub.add_parser(name, help=help_text)
        command.add_argument('--db', type=Path, help='override MVP_DB_PATH')
        command.add_argument('--state-dir', type=Path, help='override MVP_STATE_DIR')
        command.add_argument('--cycles', type=int, default=None, help='stop after N cycles (smoke tests)')
    report = sub.add_parser('report', help='read-only report from SQLite')
    report.add_argument('--db', type=Path, required=True)
    report.add_argument('--run-id', default=None)
    report.add_argument('--json', action='store_true')
    return parser


def _run_service(args, *, engine_factory=None) -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f'configuration refused: {exc}', file=sys.stderr)
        return 2
    if args.db is not None:
        settings = replace(settings, db_path=args.db)
    if args.state_dir is not None:
        settings = replace(settings, state_dir=args.state_dir)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with single_instance(settings.state_dir):
            store = MvpStore.open(settings.db_path)
            try:
                with LiveCollector() as collector:
                    service = Service(store, collector, settings, engine_factory=engine_factory)
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        try:
                            signal.signal(sig, service.request_stop)
                        except (ValueError, OSError):
                            pass
                    status = service.run(max_cycles=args.cycles)
            finally:
                store.close()
    except InstanceLockError as exc:
        print(f'service refused: {exc}', file=sys.stderr)
        return 3
    print(f'finished: {status}')
    return 75 if status == 'FAILED_STOP' else 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'report':
        try:
            report = build_report(args.db, run_id=args.run_id)
        except (ValueError, sqlite3.Error) as exc:
            print(f'report refused: {exc}', file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render(report))
        return 0
    return _run_service(args, engine_factory=IngestEngine if args.command == 'ingest' else None)


if __name__ == '__main__':
    raise SystemExit(main())
