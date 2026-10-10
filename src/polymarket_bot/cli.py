"""Console presentation for one public market-data snapshot."""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, TextIO, Sequence
import httpx
from .market_data.http import PublicGet
from .market_data.gamma import GammaClient
from .market_data.clob import ClobClient
from .market_data.models import MarketSnapshot, MarketDataError
from .market_data.snapshot import collect_snapshot

WARNING = "The Up and Down books were fetched separately; this REST snapshot is not atomic."

def render_snapshot(snapshot: MarketSnapshot, *, jakarta: bool = False, debug: bool = False) -> str:
    market = snapshot.market
    def price(value):
        return str(value) if value is not None else "unavailable"
    lines = [market.event_title, f"Question: {market.question}",
             f"Event slug: {market.event_slug}; market slug: {market.slug}",
             f"Event ID: {market.event_id}; Gamma market ID: {market.market_id}",
             f"Condition ID: {market.condition_id}",
             f"UTC window: {market.window_start.isoformat()} to {market.window_end.isoformat()}",
             f"Remaining seconds at validation: {(market.window_end-snapshot.validated_at).total_seconds():.3f}",
             f"Status: active={market.active}; closed={market.closed}; acceptingOrders={market.accepting_orders}; enableOrderBook={market.enable_order_book}",
             f"Discovery: {snapshot.discovery_source}; rolled over: {snapshot.rolled_over}"]
    for observation in (snapshot.up,snapshot.down):
        book = observation.book
        lines.extend([f"{observation.outcome.label} token ID: {observation.outcome.token_id}",
                      f"  best bid: {price(book.best_bid)}; best ask: {price(book.best_ask)}",
                      f"  midpoint reference price: {price(book.midpoint)}; spread: {price(book.spread)}",
                      f"  top bid size: {price(book.bid_size)}; top ask size: {price(book.ask_size)}",
                      f"  depth: {len(book.bids)} bid levels; {len(book.asks)} ask levels",
                      f"  source UTC: {book.source_at.isoformat()} (raw ms: {book.source_timestamp_raw})",
                      f"  retrieved UTC: {observation.request_started_at.isoformat()} to {observation.request_finished_at.isoformat()}"])
    lines.extend([f"Collection UTC: {snapshot.collection_started_at.isoformat()} to {snapshot.collection_finished_at.isoformat()}",
                  f"Final validation UTC: {snapshot.validated_at.isoformat()}; time round trip: {snapshot.time_round_trip_seconds:.3f}s",
                  WARNING])
    lines.extend(f"Warning: {warning}" for warning in market.warnings)
    if jakarta:
        zone = timezone(timedelta(hours=7))
        lines.extend([f"Jakarta window: {market.window_start.astimezone(zone).isoformat()} to {market.window_end.astimezone(zone).isoformat()}",
                      f"Jakarta validation: {snapshot.validated_at.astimezone(zone).isoformat()}"])
    if debug:
        lines.extend([f"Gamma metadata prices (not quotes): Up={price(market.up.metadata_price)}; Down={price(market.down.metadata_price)}",
                      f"Gamma metadata version: {market.metadata.get('version')}; positionIds: {market.metadata.get('positionIds')}"])
    return "\n".join(lines)

def run_snapshot(gamma: GammaClient, clob: ClobClient, *, monotonic: Callable[[], float],
                 use_fast_path: bool, jakarta: bool, debug: bool, stdout: TextIO, stderr: TextIO) -> int:
    try:
        snapshot = collect_snapshot(gamma,clob,monotonic=monotonic,use_fast_path=use_fast_path)
        print(render_snapshot(snapshot,jakarta=jakarta,debug=debug),file=stdout)
        return 0
    except MarketDataError as exc:
        print(f"{exc.code}: {exc}",file=stderr)
        if exc.market is not None:
            market = exc.market
            print(f"incomplete: {market.question}; Gamma market ID: {market.market_id}; condition ID: {market.condition_id}; pricing unavailable",file=stdout)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; no snapshot published.",file=stderr)
        return 130

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read one current BTC 15-minute public market snapshot.")
    parser.add_argument("--no-fast-path",action="store_true",help="Discover through the independent Gamma filtered list.")
    parser.add_argument("--jakarta",action="store_true",help="Also display UTC+07:00 times.")
    parser.add_argument("--debug",action="store_true",help="Show optional Gamma metadata, never quote substitutes.")
    args = parser.parse_args(argv)
    try:
        with httpx.Client(trust_env=False,follow_redirects=False) as client:
            get = PublicGet(client,monotonic=time.monotonic,sleep=time.sleep,
                            utcnow=lambda:datetime.now(timezone.utc))
            return run_snapshot(GammaClient(get),ClobClient(get),monotonic=time.monotonic,
                                use_fast_path=not args.no_fast_path,jakarta=args.jakarta,debug=args.debug,
                                stdout=sys.stdout,stderr=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted; no snapshot published.",file=sys.stderr)
        return 130
