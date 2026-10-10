from typing import Callable
from .gamma import GammaClient
from .clob import ClobClient, ServerClock, parse_book
from .discovery import discover_current
from .models import BookObservation, MarketSnapshot, MarketDataError

def collect_snapshot(gamma: GammaClient, clob: ClobClient, *, monotonic: Callable[[], float],
                     use_fast_path: bool = True) -> MarketSnapshot:
    started = monotonic()
    deadline = started + 120.0
    for attempt in range(2):
        if monotonic() >= deadline:
            raise MarketDataError("transport_error","120-second run deadline exhausted.")
        clock = ServerClock(clob.get_time(deadline=deadline),monotonic=monotonic)
        if attempt == 0:
            collection_start = clock.utc_at(started)
        market, source = discover_current(gamma,clock=clock,deadline=deadline,use_fast_path=use_fast_path)
        if not market.pricing_ready:
            raise MarketDataError("book_unavailable","Current market identified; acceptingOrders/enableOrderBook not explicitly true.",
                                  market=market)
        observations = []
        try:
            for outcome in (market.up,market.down):
                read = clob.get_book(outcome.token_id,deadline=deadline)
                book = parse_book(read,token_id=outcome.token_id,condition_id=market.condition_id)
                observations.append(BookObservation(outcome,book,clock.utc_at(read.started_mono),clock.utc_at(read.finished_mono)))
        except MarketDataError as exc:
            if exc.http_status != 404:
                raise
            fresh = ServerClock(clob.get_time(deadline=deadline),monotonic=monotonic)
            if market.window_start <= fresh.now() < market.window_end:
                raise
            continue
        final = ServerClock(clob.get_time(deadline=deadline),monotonic=monotonic)
        validated = final.now()
        if monotonic() >= deadline:
            raise MarketDataError("transport_error","120-second run deadline exhausted; no late snapshot.")
        if market.window_start <= validated < market.window_end:
            return MarketSnapshot(market,source,observations[0],observations[1],collection_start,
                                  final.now(),validated,final.round_trip_seconds,bool(attempt))
    raise MarketDataError("rollover_exhausted","Market rolled over twice; no snapshot published.")
