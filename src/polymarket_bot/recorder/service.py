"""Single-owner, fixed-cadence recorder coordinator."""
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Callable

from polymarket_bot.market_data.gamma import GammaClient
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.http import PublicGet
from .books import collect_books
from .clock import RecorderClock
from .metadata import acquire_market, refresh_revision
from .metrics import take_checkpoint
from .models import RunConfig, RunResult, SlotCommit
from .reference import disabled_reference
from .resolution import reconcile_due
from .schedule import classify_wake, select_future_slot, slot_grid
from .store import RecorderStore, RecorderStoreError


class ReferenceWorker:
    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._thread = None
        self._result = None
        self._error = None
        self._stopped = False

    def submit(self, operation):
        import threading
        with self._lock:
            if self._stopped or self._thread is not None:
                return False
            def run():
                try:
                    result = operation()
                except Exception as exc:
                    with self._lock:
                        self._error = exc
                else:
                    with self._lock:
                        self._result = result
            self._thread = threading.Thread(target=run, name='reference-fetch', daemon=False)
            self._thread.start()
            return True

    def take(self):
        with self._lock:
            if self._thread is None or self._thread.is_alive():
                return None
            self._thread.join()
            self._thread = None
            result, error = self._result, self._error
            self._result = self._error = None
        if error is not None:
            raise error
        return result

    def stop(self, timeout=5):
        with self._lock:
            self._stopped = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise MarketDataError('reference_shutdown_failed', 'Reference worker remains active; owner must retain lifecycle handle.')


class ApproximateReferenceWorker:
    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._thread = None
        self._result = None
        self._error = None
        self._stopped = False

    def submit(self, operation):
        import threading
        with self._lock:
            if self._stopped or self._thread is not None:
                return False
            def run():
                try:
                    result = operation()
                except Exception as exc:
                    with self._lock:
                        self._error = exc
                else:
                    with self._lock:
                        self._result = result
            self._thread = threading.Thread(target=run, name='approximate-reference-fetch', daemon=False)
            self._thread.start()
            return True

    def take(self):
        with self._lock:
            if self._thread is None or self._thread.is_alive():
                return None
            self._thread.join()
            self._thread = None
            result, error = self._result, self._error
            self._result = self._error = None
        if error is not None:
            raise error
        return result

    def stop(self, timeout=10.1):
        with self._lock:
            self._stopped = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise MarketDataError('approximate_reference_shutdown_failed', 'Approximate reference worker remains active; owner must retain lifecycle handle.')


class _StopRequested(Exception):
    """Internal normal-stop signal raised before an actual HTTP attempt."""


def _epoch_us(value):
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


class Recorder:
    def __init__(self, store: RecorderStore, public: PublicGet, *,
                 monotonic_ns: Callable[[], int], wall_us: Callable[[], int],
                 sleep: Callable[[float], None], offline_reference=None,
                 approximate_reference=None):
        self.offline_reference = offline_reference
        self.approximate_reference = approximate_reference
        self.reference_worker = None
        self.approximate_worker = None
        self.reference_gap_end = None
        self.reference_plan_cursor = ''
        self.store, self.public = store, public
        self.monotonic_ns, self.wall_us, self.sleep = monotonic_ns, wall_us, sleep
        self.clock = RecorderClock(monotonic_ns=monotonic_ns, wall_us=wall_us)
        # Both transport timing representations derive from the same run epoch.
        public.monotonic_ns = monotonic_ns
        public.monotonic = lambda: monotonic_ns() / 1_000_000_000
        public.utcnow = lambda: datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=wall_us())
        public.sleep = sleep
        self.gamma = GammaClient(public)

    def _reference_submit(self, row, deadline_ns):
        if self.reference_worker is None or not self.clock.usable() or self.monotonic_ns() >= deadline_ns:
            return False
        anchor, anchor_id, run_id = self.clock.anchor, self.anchor_id, self.run_id
        project = lambda ns: anchor.server_us + (ns-anchor.midpoint_ns)//1000
        return self.reference_worker.submit(lambda: self.offline_reference.fetch(row,
            deadline_ns/1_000_000_000, run_id=run_id, anchor_id=anchor_id, project=project))

    def _reference_drain(self, *, stopping=False, result=None):
        if self.reference_worker is None:
            return
        try:
            result = self.reference_worker.take() if result is None else result
        except MarketDataError:
            raise
        except Exception:
            raise MarketDataError('reference_worker_failed', 'Reference worker failed.') from None
        if result is None:
            return
        from dataclasses import replace
        from .reference import boundary_retry_due
        row, fetch, reports, deadline = result
        self.store.save_reference_evidence(fetch, reports, ())
        now = self._now() if self.clock.usable() else fetch.received_server_us
        reason, status, selected = fetch.error_code or 'DON_UNVERIFIED', 'UNAVAILABLE', None
        if row.role != 'SLOT_CURRENT':
            from .reference import select_boundary_report
            status, reason = 'PENDING', 'BOUNDARY_PENDING'
            try:
                retained = self.store._rows("SELECT report_hash FROM reference_reports WHERE feed_id=? AND decode_status='DECODED' AND valid_from_seconds<=? AND observations_seconds>=? LIMIT 2",
                    (fetch.feed_id, row.target_us//1_000_000, row.target_us//1_000_000))
                if len({r['report_hash'] for r in retained}) > 1:
                    raise MarketDataError('BOUNDARY_AMBIGUOUS', 'Retained reports contain boundary.')
                selected = select_boundary_report(reports, row.target_us)
            except MarketDataError as exc:
                if exc.code != 'BOUNDARY_AMBIGUOUS':
                    raise
                status, reason = 'AMBIGUOUS', exc.code
            if selected is not None:
                status, reason = 'AVAILABLE', None
        elif reports:
            selected = reports[0]
            if selected.decode_status != 'DECODED':
                reason = 'INVALID_REFERENCE'
            elif (fetch.received_server_us is None or selected.observations_seconds is None
                    or selected.valid_from_seconds is None or selected.expires_at_seconds is None
                    or not selected.valid_from_seconds*1_000_000 <= fetch.received_server_us <= selected.expires_at_seconds*1_000_000
                    or not 0 <= fetch.received_server_us-selected.observations_seconds*1_000_000 <= 5_000_000):
                reason = 'STALE_REFERENCE'
            elif fetch.error_code is None:
                status, reason = 'AVAILABLE', None
        if self.monotonic_ns()/1_000_000_000 >= deadline and status != 'AMBIGUOUS':
            status, reason = 'UNAVAILABLE', 'LATE_REFERENCE'
        from .reference import ManagedReferenceSession
        managed = isinstance(self.offline_reference, ManagedReferenceSession)
        learned_wall = self.wall_us()
        prior = self.store._one('SELECT MAX(learned_wall_us) AS learned FROM reference_associations WHERE market_id=? AND role=? AND slot_id IS ?',
            (row.market_id, row.role, row.slot_id))['learned']
        if managed and learned_wall == prior:
            self.sleep(0.000001)
            learned_wall = self.wall_us()
        if managed and (learned_wall < fetch.attempt.ended_wall_us or
                        (prior is not None and learned_wall < prior)):
            self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=learned_wall,
                detail={'reason': 'REFERENCE_CLOCK_ORDER_INVALID', 'diagnostic_only': True,
                    'receipt_wall_us': fetch.attempt.ended_wall_us, 'prior_learned_wall_us': prior,
                    'market_id': row.market_id, 'role': row.role, 'slot_id': row.slot_id})
            return
        if managed and learned_wall == prior:
            self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=learned_wall,
                detail={'reason': 'REFERENCE_LEARNING_TIME_COLLISION', 'diagnostic_only': True,
                    'market_id': row.market_id, 'role': row.role, 'slot_id': row.slot_id})
            return
        if not managed:
            learned_wall = max(learned_wall, fetch.attempt.ended_wall_us,
                (prior+1) if prior is not None else learned_wall)
        association = replace(row, status=status, reason=reason, origin=fetch.origin,
            available_live=status == 'AVAILABLE' and row.role == 'SLOT_CURRENT' and fetch.origin == 'LIVE_LATEST',
            learned_wall_us=learned_wall, learned_server_us=max(now, fetch.received_server_us),
            report_hash=selected.report_hash if selected else None, report_id=None, fetch_id=None,
            attempt_count=row.attempt_count+1, selection_policy_revision='signed-inclusive-seconds-v1',
            next_due_us=None if row.role == 'SLOT_CURRENT' else boundary_retry_due(row.target_us,
                attempt_count=row.attempt_count+1, now_us=max(now, fetch.received_server_us)))
        if managed:
            association = replace(association, selection_policy_revision='managed-signed-inclusive-seconds-v1')
        if association.available_live and not managed:
            self.store.save_reference_evidence(fetch, reports, (replace(association,
                status='UNAVAILABLE', available_live=False, reason='PERSISTENCE_PENDING'),))
            association = replace(association, learned_wall_us=max(self.wall_us(), association.learned_wall_us+1),
                learned_server_us=self._now() if self.clock.usable() else association.learned_server_us)
            if self.monotonic_ns()/1_000_000_000 >= deadline:
                association = replace(association, status='UNAVAILABLE', available_live=False, reason='LATE_REFERENCE')
        self.store.save_reference_evidence(fetch, reports, (association,))
        if association.available_live and not managed and self.monotonic_ns()/1_000_000_000 >= deadline:
            association = replace(association, status='UNAVAILABLE', available_live=False,
                reason='LATE_REFERENCE', learned_wall_us=max(self.wall_us(), association.learned_wall_us+1),
                learned_server_us=self._now() if self.clock.usable() else association.learned_server_us)
            self.store.save_reference_evidence(fetch, reports, (association,))
        identity = {'slot_id': row.slot_id, 'market_id': row.market_id, 'role': row.role,
            'target_us': row.target_us, 'learned_wall_us': association.learned_wall_us,
            'report_hash': association.report_hash, 'available_live': association.available_live}
        # Evidence barrier commits first; receipt timestamps observe its completed commit.
        commit_event_id = self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=self.wall_us(),
            detail=dict(identity, reason='REFERENCE_COMMIT', diagnostic_only=True))
        durable_wall = self.wall_us()
        if durable_wall < association.learned_wall_us:
            self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=durable_wall,
                detail=dict(identity, reason='REFERENCE_CLOCK_ORDER_INVALID', diagnostic_only=True,
                    commit_event_id=commit_event_id))
        else:
            self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=durable_wall,
                detail=dict(identity, reason='REFERENCE_DURABLE', diagnostic_only=True, time_domain='LOCAL_WALL',
                    durability_basis='POST_COMMIT_BARRIER_V1', commit_event_id=commit_event_id,
                    durable_server_us=self._now() if self.clock.usable() else None))
        if fetch.error_code in ('chainlink_auth_failed', 'websocket_failed'):
            raise MarketDataError(fetch.error_code, 'Reference permission or worker gate lost.')

    def _approximate_drain(self, *, stopping=False, result=None):
        if self.approximate_worker is None:
            return
        try:
            result = self.approximate_worker.take() if result is None else result
        except MarketDataError:
            raise
        except Exception:
            raise MarketDataError('approximate_reference_worker_failed', 'Approximate reference worker failed.') from None
        if result is None:
            return
        report, observations = result
        for observation in observations:
            self.store.save_approximate_observation(self.run_id, observation)
        self.store.save_approximate_reference(self.run_id, report)

    def _approximate_submit(self, deadline_ns):
        if self.approximate_worker is None or not self.clock.usable() or self.monotonic_ns() >= deadline_ns:
            return False
        anchor, run_id = self.clock.anchor, self.run_id
        session = self.approximate_reference
        if anchor is None or session is None:
            return False
        anchor_id = self.anchor_id
        deadline = min(deadline_ns, self.run_deadline_ns) / 1_000_000_000
        project = lambda ns: anchor.server_us + (ns-anchor.midpoint_ns)//1000
        return self.approximate_worker.submit(lambda: session.fetch(
            run_id=run_id, anchor_id=anchor_id, project=project,
            deadline=deadline))

    def _reference_housekeeping(self, deadline_ns):
        if self.reference_worker is None or not self.clock.usable() or self.monotonic_ns() >= deadline_ns:
            return
        self._reference_drain()
        gaps = getattr(self.offline_reference, 'gaps', None)
        if gaps is not None:
            for start, end in gaps()[-128:]:
                if self.reference_gap_end is None or end > self.reference_gap_end:
                    self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=self.wall_us(),
                        detail={'reason': 'REFERENCE_CONNECTION_GAP', 'diagnostic_only': True,
                            'time_domain': 'RUN_MONOTONIC', 'start_mono_ns': round(start*1_000_000_000),
                            'end_mono_ns': round(end*1_000_000_000)})
                    self.reference_gap_end = end
        from .reference import boundary_reference, due_reference_boundaries
        if self.monotonic_ns() >= deadline_ns:
            return
        plans = self.store._rows('SELECT market_id,window_start_us,window_end_us FROM markets WHERE market_id>? ORDER BY market_id LIMIT 2',
            (self.reference_plan_cursor,))
        for market in plans:
            for role in ('OPENING', 'CLOSING'):
                if self.monotonic_ns() >= deadline_ns:
                    return
                if self.store._one('SELECT association_id FROM reference_associations WHERE market_id=? AND role=? LIMIT 1',
                        (market['market_id'], role)) is None:
                    self.store.save_reference_plan(boundary_reference(market['market_id'], role,
                        window_start_us=market['window_start_us'], window_end_us=market['window_end_us'],
                        learned_wall_us=self.wall_us(), learned_server_us=self._now()))
            self.reference_plan_cursor = market['market_id']
        if self.monotonic_ns() < deadline_ns:
            for row in due_reference_boundaries(self.store, self._now(), limit=1):
                self._reference_submit(row, deadline_ns)

    def _control(self, operation):
        attempts = []
        previous = self.public.on_attempt
        self.public.on_attempt = attempts.append
        try:
            return operation()
        finally:
            self.public.on_attempt = previous
            self.store.save_control_attempts(self.run_id, tuple(attempts))

    def _now(self):
        return self.clock.server_us_at(self.monotonic_ns())

    def _wait(self, deadline_ns):
        remaining = min(deadline_ns, self.run_deadline_ns) - self.monotonic_ns()
        if remaining > 0:
            self.sleep(remaining / 1_000_000_000)

    def _calibrate(self, deadline):
        deadline = min(deadline, self.run_deadline_ns / 1_000_000_000)
        try:
            read = self._control(lambda: self.public.get_json("clob", "/time", deadline=deadline))
            anchor = self.clock.calibrate(read)
        except MarketDataError as exc:
            self.next_clock_ns = self.monotonic_ns() + (60_000_000_000 if self.clock.usable() else 2_000_000_000)
            if not self.clock.usable():
                self.store.record_event(self.run_id, "CLOCK_PAUSE", started_us=self.wall_us(),
                                        detail={"reason": exc.code, "time_domain": "LOCAL_WALL"})
            return False
        self.anchor_id = self.store.save_anchor(self.run_id, anchor)
        from .reference import ManagedReferenceSession
        if isinstance(self.offline_reference, ManagedReferenceSession) and self.reference_worker is not None:
            completed = self.offline_reference.discovery_completed_mono_ns
            if anchor.status != 'DISCONTINUITY' and 0 <= anchor.midpoint_ns-completed <= 60_000_000_000:
                from .models import CHAINLINK_FEED_ID
                verified = anchor.server_us+(completed-anchor.midpoint_ns)//1000
                with self.store._transaction():
                    self.store._connection.execute('UPDATE reference_feeds SET verified_us=? WHERE feed_id=?',
                        (verified, CHAINLINK_FEED_ID))
                self.offline_reference.discovery_verified_us = verified
        self.next_clock_ns = anchor.midpoint_ns + 60_000_000_000
        if anchor.status == "DISCONTINUITY":
            self.next_clock_ns = self.monotonic_ns()
            self.store.record_event(self.run_id, "CLOCK_PAUSE", started_us=self.wall_us(),
                                    detail=anchor.diagnostic)
            self.revision = None
            self.clock_recovery_needed = True
            self.miss_reason = "CLOCK_PAUSE"
            return False
        return True

    def _clock_ready_after_io(self, reason):
        if self.clock.usable():
            return True
        self.miss_reason = "CLOCK_PAUSE"
        self.clock_recovery_needed = True
        self.revision = None
        self.next_clock_ns = self.monotonic_ns()
        self.store.record_event(self.run_id, "CLOCK_PAUSE", started_us=self.wall_us(),
                                detail={"reason": reason, "time_domain": "LOCAL_WALL"})
        return False

    def _housekeeping(self, target_ns, *, before_boundary=False):
        """Serial logical jobs share the writer and stop at the armed target."""
        deadline_ns = min(target_ns - 1_000_000, self.run_deadline_ns)
        if self.monotonic_ns() >= deadline_ns:
            return
        self._reference_housekeeping(deadline_ns)
        self._checkpoint_due(target_ns)
        if self.monotonic_ns() >= deadline_ns:
            return
        if before_boundary or self.monotonic_ns() >= self.next_clock_ns:
            if not self._calibrate(deadline_ns / 1_000_000_000) and not self.clock.usable():
                return
        if self.monotonic_ns() >= deadline_ns:
            return
        if self.revision is None:
            self._reconcile(deadline_ns)
            return
        if self.revalidate:
            try:
                candidate = self._control(lambda: acquire_market(self.gamma, self.public,
                    clock=self.clock, deadline=deadline_ns / 1_000_000_000,
                    use_fast_path=self.config.use_fast_path))
            except MarketDataError as exc:
                if not self._clock_ready_after_io("ANCHOR_EXPIRED_DURING_REVALIDATION"):
                    return
                self.store.record_event(self.run_id, "DISCOVERY_GAP", started_us=self._now(),
                                        detail={"reason": exc.code, "revalidation": True, "diagnostic_only": True})
                self._reconcile(deadline_ns)
                return
            if not self._clock_ready_after_io("ANCHOR_EXPIRED_DURING_REVALIDATION"):
                return
            if not self._save_revision(candidate):
                return
            self.revalidate = False
        if self.monotonic_ns() >= deadline_ns:
            return
        previous = self.revision
        last = previous.last_refresh_attempt_us or previous.observed_us
        if previous.market.market_id not in self.invalidated and self._now() >= last + 60_000_000:
            candidate = self._control(lambda: refresh_revision(previous, self.gamma, self.public,
                clock=self.clock, deadline=deadline_ns / 1_000_000_000))
            if not self._clock_ready_after_io("ANCHOR_EXPIRED_DURING_METADATA"):
                return
            if not self._save_revision(candidate):
                return
            warnings = self.revision.warnings
            if any(warning.startswith("metadata_refresh_failed") for warning in warnings):
                self.store.record_event(self.run_id, "DISCOVERY_GAP", started_us=self._now(),
                    market_id=previous.market.market_id, detail={"warnings": warnings, "diagnostic_only": True})
        if self.monotonic_ns() >= deadline_ns:
            return
        self._reconcile(deadline_ns)

    def _checkpoint_due(self, target_ns=None):
        """Admit hourly work only in a transaction gap with armed-target budget.

        Actual storage overhead remains observable. The caller rechecks time
        afterward, so a slow checkpoint cannot cause burst catch-up captures.
        """
        now_ns = self.monotonic_ns()
        if now_ns < self.next_checkpoint_ns:
            return
        if target_ns is not None and now_ns >= target_ns-1_000_000:
            return
        take_checkpoint(self.store,run_id=self.run_id,wall_us=self.wall_us())
        self.next_checkpoint_ns = self.monotonic_ns()+3_600_000_000_000

    def _save_revision(self, candidate):
        try:
            revision_id = self.store.save_market_revision(self.run_id, candidate)
        except RecorderStoreError as exc:
            if exc.code != "identity_conflict":
                raise
            market_id = candidate.market.market_id
            first_conflict = market_id not in self.invalidated
            self.invalidated.add(market_id)
            self.revision, self.revision_id = self.store.paused_revision(market_id)
            self.revalidate = first_conflict
            if first_conflict:
                self.next_clock_ns = self.monotonic_ns()
            server_time_available = self.clock.usable()
            self.store.record_event(self.run_id, "IDENTITY_CONFLICT",
                started_us=self._now() if server_time_available else self.wall_us(),
                market_id=market_id, detail={"capture_paused": True,
                    "persisted_invalidation": not first_conflict,
                    "time_domain": "SERVER" if server_time_available else "LOCAL_WALL"})
            return False
        self.revision, self.revision_id = candidate, revision_id
        if self.reference_worker is not None:
            from .reference import boundary_reference
            market = candidate.market
            for role in ('OPENING', 'CLOSING'):
                if self.store._one('SELECT association_id FROM reference_associations WHERE market_id=? AND role=? LIMIT 1',
                        (market.market_id, role)) is None:
                    self.store.save_reference_plan(boundary_reference(market.market_id, role,
                        window_start_us=_epoch_us(market.window_start), window_end_us=_epoch_us(market.window_end),
                        learned_wall_us=self.wall_us(), learned_server_us=self._now()))
        return True

    def _reconcile(self, deadline_ns):
        if self.monotonic_ns() >= deadline_ns or not self.clock.usable():
            return
        anchor = self.clock.anchor
        project = lambda moment_ns: anchor.server_us + (moment_ns-anchor.midpoint_ns)//1000
        previous_callback = self.public.on_attempt
        self.public.on_attempt = None
        try:
            reconcile_due(self.public, self.store, now_us=self._now(),
                          deadline=deadline_ns / 1_000_000_000, receipt_server_us=project)
        finally:
            self.public.on_attempt = previous_callback
        self._clock_ready_after_io("ANCHOR_EXPIRED_DURING_RESOLUTION")

    def _recover_known(self, *, reason="DOWNTIME", markets=None):
        now = self._now()
        for market in self.known_markets if markets is None else markets:
            covered = self.store.covered_indices(market["market_id"])
            for target in slot_grid(market["window_start_us"]):
                if target.scheduled_us <= now and target.index not in covered:
                    self.store.mark_missed(market["market_id"], target, run_id=self.run_id,
                                          reason=reason, wall_us=self.wall_us())
            if now >= market["window_end_us"]:
                self.store.expire_market(market["market_id"], observed_us=now)
        self.known_recovered = True

    def run(self, config: RunConfig, *, should_stop: Callable[[], bool],
            deadline_ns: int | None = None) -> RunResult:
        self.run_deadline_ns = deadline_ns if deadline_ns is not None else 2**63 - 1
        requested_stop = should_stop
        should_stop = lambda: self.monotonic_ns() >= self.run_deadline_ns or requested_stop()
        self.run_id, self.finalized = "", 0
        try:
            return self._execute_run(config, should_stop=should_stop)
        except MarketDataError as exc:
            return RunResult(self.run_id, 'FAILED', 2, self.finalized, exc.code)
        except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
            # Includes failure to close; no successful durability claim escapes.
            return RunResult(self.run_id, "FAILED", 1, self.finalized,
                exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")

    def _execute_run(self, config, *, should_stop):
        try:
            return self._execute_started_run(config, should_stop=should_stop)
        except (ConnectionError, TimeoutError):
            raise MarketDataError('reference_transport_failed', 'Reference startup transport failed.') from None
        finally:
            try:
                from .reference import ManagedReferenceSession
                if config.mode == 'REFERENCE_REQUIRED' and isinstance(self.offline_reference, ManagedReferenceSession):
                    self.offline_reference.stop()
            finally:
                worker = self.approximate_worker
                if worker is None or worker._thread is None or not worker._thread.is_alive():
                    self.store.close()

    def _execute_started_run(self, config, *, should_stop):
        from .reference import OfflineReferenceSession
        if config.mode == "REFERENCE_REQUIRED" and self.approximate_reference is not None:
            self.store.close()
            return RunResult("", "FAILED", 2, 0, "mixed_modes_rejected")
        if config.mode == "APPROXIMATE_REFERENCE":
            if self.approximate_reference is None:
                self.store.close()
                return RunResult("", "FAILED", 2, 0, "approximate_session_required")
            if self.store._connection.execute('PRAGMA user_version').fetchone()[0] != 2:
                raise RecorderStoreError('schema_version_mismatch', 'Approximate mode requires a v2 database.')
            self.approximate_worker = ApproximateReferenceWorker()
        elif config.mode != "BOOK_ONLY" and (config.mode != 'REFERENCE_REQUIRED'
                or not isinstance(self.offline_reference, OfflineReferenceSession)):
            self.store.close()
            return RunResult("", "FAILED", 2, 0, "reference_runtime_unavailable")
        if config.mode == 'REFERENCE_REQUIRED':
            from .reference import require_reference_activation
            try:
                require_reference_activation(config.mode, self.offline_reference.evidence)
                from .reference import ManagedReferenceSession
                if isinstance(self.offline_reference, ManagedReferenceSession):
                    from .models import CHAINLINK_FEED_ID
                    pin = self.store._one('SELECT price_scale,decoder_revision FROM reference_feeds WHERE feed_id=?', (CHAINLINK_FEED_ID,))
                    try:
                        self.offline_reference.start(config.mode, self.monotonic_ns()/1_000_000_000+10, persisted_pin=pin)
                    except (ConnectionError, TimeoutError, OSError):
                        raise MarketDataError('reference_transport_failed', 'Reference startup transport failed.') from None
            except MarketDataError as exc:
                self.store.close()
                return RunResult('', 'FAILED', 2, 0, exc.code)
            self.reference_worker = ReferenceWorker()
        original_error = None
        original_attempts = self.public.max_attempts
        original_guard = self.public.before_attempt
        def stop_guard():
            if should_stop():
                raise _StopRequested
        self.public.before_attempt = stop_guard
        try:
            return self._record(config, should_stop=should_stop)
        except _StopRequested:
            if self.run_id:
                try:
                    self.store.recover_interrupted(wall_us=self.wall_us(), run_id=self.run_id,
                                                   finish_runs=False, reason="STOP_REQUESTED")
                    self._shutdown("STOPPED", "STOP_REQUESTED")
                except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
                    original_error = exc
                    return RunResult(self.run_id, "FAILED", 1, self.finalized,
                        exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")
            return RunResult(self.run_id, "STOPPED", 0, self.finalized, "STOP_REQUESTED")
        except KeyboardInterrupt:
            try:
                if self.run_id:
                    self.store.recover_interrupted(wall_us=self.wall_us(), run_id=self.run_id,
                                                   finish_runs=False, reason="KEYBOARD_INTERRUPT")
                    self._shutdown("INTERRUPTED", "KEYBOARD_INTERRUPT")
            except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
                original_error = exc
                return RunResult(self.run_id, "FAILED", 1, self.finalized,
                                 exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")
            return RunResult(self.run_id, "INTERRUPTED", 130, self.finalized, "KEYBOARD_INTERRUPT")
        except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
            original_error = exc
            # No further persistence or HTTP follows a failed writer/invariant.
            return RunResult(self.run_id, "FAILED", 1, self.finalized,
                             exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")
        except MarketDataError as exc:
            original_error = exc
            if self.run_id and not self.store._write_failed:
                self.store.recover_interrupted(wall_us=self.wall_us(), run_id=self.run_id, finish_runs=False)
                self.store.finish_run(self.run_id, state='FAILED', wall_us=self.wall_us(), reason=exc.code)
            return RunResult(self.run_id, 'FAILED', 2, self.finalized, exc.code)
        finally:
            self.public.max_attempts = original_attempts
            self.public.before_attempt = original_guard
            try:
                if self.reference_worker is not None:
                    self.reference_worker.stop()
            finally:
                try:
                    if self.approximate_worker is not None:
                        try:
                            self.approximate_worker.stop()
                        except MarketDataError:
                            if original_error is None:
                                raise
                finally:
                    try:
                        from .reference import ManagedReferenceSession
                        if config.mode == 'REFERENCE_REQUIRED' and isinstance(self.offline_reference, ManagedReferenceSession):
                            self.offline_reference.stop()
                    finally:
                        worker = self.approximate_worker
                        if worker is None or worker._thread is None or not worker._thread.is_alive():
                            self.store.close()

    def _shutdown(self, state, reason):
        if self.reference_worker is not None:
            self.reference_worker.stop()
            self._reference_drain(stopping=True)
        if self.approximate_worker is not None:
            self.approximate_worker.stop()
            self._approximate_drain(stopping=True)
        self.store.record_event(self.run_id, "SHUTDOWN", started_us=self.wall_us(),
                                detail={"reason": reason})
        self.store.finish_run(self.run_id, state=state, wall_us=self.wall_us(), reason=reason)
        take_checkpoint(self.store,run_id=self.run_id,wall_us=self.wall_us())

    def _start_discovery_gap(self, started_us, **detail):
        if self.discovery_gap_id is None:
            self.discovery_gap_start = started_us
            self.discovery_gap_id = self.store.record_event(self.run_id, "DISCOVERY_GAP",
                started_us=started_us, detail={"time_domain": "SERVER", "acquisition_gap": True, **detail})

    def _finish_discovery_gap(self, observed_us):
        if self.discovery_gap_id is not None and observed_us >= self.discovery_gap_start:
            self.store.finish_discovery_gap(self.discovery_gap_id, run_id=self.run_id, ended_us=observed_us)
            self.discovery_gap_id = None

    def _record(self, config, *, should_stop):
        self.config = config
        self.revalidate = False
        previous_wall_us, self.known_markets = self.store.restart_context()
        self.invalidated = {row["market_id"] for row in self.known_markets if row["lifecycle"] == "INVALIDATED"}
        self.store.recover_interrupted(wall_us=self.wall_us())
        self.public.max_attempts = min(self.public.max_attempts, config.max_attempts)
        self.run_id = self.store.start_run(config, wall_us=self.wall_us(), app_revision=config.policy_revision,
            offline_reference=self.offline_reference if self.reference_worker else None)
        take_checkpoint(self.store,run_id=self.run_id,wall_us=self.wall_us())
        self.next_checkpoint_ns = self.monotonic_ns()+3_600_000_000_000
        self.store.record_event(self.run_id, "STARTUP", started_us=self.wall_us())
        if previous_wall_us is not None:
            self.store.record_event(self.run_id, "RESTART", started_us=previous_wall_us,
                ended_us=self.wall_us(), detail={"monotonic_epoch": "NEW"})
            if not self.known_markets:
                self.store.record_event(self.run_id, "DISCOVERY_GAP", started_us=previous_wall_us,
                    ended_us=self.wall_us(), detail={"unknown_windows": True, "reason": "DOWNTIME",
                                                   "time_domain": "LOCAL_WALL"})
        self.revision = None
        self.next_clock_ns = self.monotonic_ns()
        self.miss_reason = "BEFORE_START"
        self.known_recovered = False
        self.clock_recovery_needed = False
        self.discovery_gap_id = None
        self.discovery_gap_start = None
        while not should_stop():
            if self.reference_worker is not None:
                health = getattr(self.offline_reference, 'check_health', None)
                if health is not None:
                    health()
            if self.revision is None or not self.clock.usable():
                self._checkpoint_due()
            if not self.clock.usable():
                self.miss_reason = "CLOCK_PAUSE" if self.clock.anchor else "BEFORE_START"
                if self.monotonic_ns() < self.next_clock_ns:
                    self._wait(self.next_clock_ns)
                    continue
                if not self._calibrate(self.public.monotonic()+10):
                    self.sleep(2)
                    continue
            if not self.known_recovered:
                self._recover_known()
                if self.known_markets:
                    known_end = max(row["window_end_us"] for row in self.known_markets)
                    if known_end <= self._now():
                        self._start_discovery_gap(known_end, unknown_windows=True, reason="DOWNTIME")
            if self.clock_recovery_needed:
                self._recover_known(reason="CLOCK_PAUSE", markets=self.store.restart_context()[1])
                self.clock_recovery_needed = False
            if self.revision is None:
                started_us = self._now()
                try:
                    candidate = self._control(lambda: acquire_market(self.gamma, self.public,
                        clock=self.clock, deadline=min(self.public.monotonic()+10, self.run_deadline_ns/1_000_000_000),
                        use_fast_path=config.use_fast_path))
                except MarketDataError as exc:
                    self._clock_ready_after_io("ANCHOR_EXPIRED_DURING_DISCOVERY")
                    if self.discovery_gap_id is None:
                        self._start_discovery_gap(started_us, reason=exc.code, unknown_market=True)
                    else:
                        self.store.record_event(self.run_id, "DISCOVERY_GAP", started_us=started_us,
                            ended_us=max(started_us,self._now()) if self.clock.usable() else None,
                            detail={"reason": exc.code, "unknown_market": True,
                                    "time_domain": "SERVER", "diagnostic_only": True})
                    retry_ns = self.monotonic_ns() + 2_000_000_000
                    self._housekeeping(retry_ns)
                    self._wait(retry_ns)
                    continue
                if not self._clock_ready_after_io("ANCHOR_EXPIRED_DURING_DISCOVERY"):
                    continue
                if self._save_revision(candidate):
                    self._finish_discovery_gap(candidate.observed_us)
            market = self.revision.market
            start_us = _epoch_us(market.window_start)
            end_us = _epoch_us(market.window_end)
            now = self._now()
            selection = select_future_slot(market, now_us=now,
                terminal_indices=self.store.covered_indices(market.market_id))
            grid = slot_grid(start_us)
            for index in selection.missed:
                self.store.mark_missed(market.market_id, grid[index], run_id=self.run_id,
                                      reason=self.miss_reason, wall_us=self.wall_us())
            self.miss_reason = "PROCESSING_OVERRUN" if self.revision.capture_ready else "METADATA_NOT_READY"
            if now >= end_us:
                self.store.expire_market(market.market_id, observed_us=now)
                self._start_discovery_gap(end_us, reason="ROLLOVER", prior_market_id=market.market_id)
                self.revision = None
                self.miss_reason = "BEFORE_START"
                continue
            target = selection.target
            if target is None:
                self._housekeeping(self.clock.deadline_ns(end_us), before_boundary=True)
                if self.revision is None or not self.clock.usable():
                    continue
                self._wait(self.clock.deadline_ns(end_us))
                continue
            target_ns = self.clock.deadline_ns(target.scheduled_us)
            self._housekeeping(target_ns)
            if (self.revision is None or self.revision.market.market_id != market.market_id
                    or not self.clock.usable()):
                continue
            target_ns = self.clock.deadline_ns(target.scheduled_us)
            if self.monotonic_ns() >= target_ns:
                continue
            self._wait(target_ns)
            if should_stop():
                break
            if not self.clock.usable():
                self.miss_reason = "CLOCK_PAUSE"
                self.store.record_event(self.run_id, "CLOCK_PAUSE", started_us=self.wall_us(),
                                        detail={"reason": "ANCHOR_EXPIRED_DURING_WAIT"})
                continue
            if classify_wake(target, now_us=self._now(), window_end_us=end_us) != "CAPTURE":
                continue
            if not self.revision.capture_ready:
                self.store.mark_missed(market.market_id, target, run_id=self.run_id,
                                      reason="METADATA_NOT_READY", wall_us=self.wall_us())
                self.miss_reason = "METADATA_NOT_READY"
                continue
            started_ns = self.monotonic_ns()
            slot_id = self.store.reserve_slot(market.market_id, target, run_id=self.run_id,
                anchor_id=self.anchor_id, metadata_revision_id=self.revision_id, started_mono_ns=started_ns,
                started_server_us=self._now())
            anchor = self.clock.anchor
            project = lambda moment_ns: anchor.server_us + (moment_ns-anchor.midpoint_ns)//1000
            if self.approximate_worker is not None:
                reference_deadline_ns = self.clock.deadline_ns(min(target.scheduled_us+2_000_000, end_us))
                self._approximate_drain()
                self._approximate_submit(reference_deadline_ns)
            if self.reference_worker is not None:
                from .models import ReferenceAssociationDraft
                row = ReferenceAssociationDraft(market.market_id, 'SLOT_CURRENT', target.scheduled_us,
                    'UNAVAILABLE', 'NOT_FETCHED', False, self.wall_us(), self._now(), slot_id=slot_id)
                self._reference_drain()
                reference_deadline_ns = self.clock.deadline_ns(min(target.scheduled_us+2_000_000, end_us))
                current_reference_result = None
                if self.offline_reference.live is None and not self._reference_submit(row, reference_deadline_ns):
                    current_reference_result = 'BUSY'
            previous = self.public.on_attempt
            self.public.on_attempt = None
            try:
                books = collect_books(self.public, market,
                    deadline=min(self.run_deadline_ns, self.clock.deadline_ns(min(target.scheduled_us+2_000_000, end_us)))/1_000_000_000,
                    receipt_server_us=lambda moment: project(round(moment*1_000_000_000)),
                    uncertainty_us=self.clock.anchor.uncertainty_us)
            finally:
                self.public.on_attempt = previous
            ended_ns = self.monotonic_ns()
            reference = disabled_reference(market.market_id, target.scheduled_us,
                learned_wall_us=self.wall_us(), learned_server_us=project(ended_ns))
            if self.approximate_worker is not None:
                from dataclasses import replace
                reference = replace(reference, status='UNAVAILABLE', reason='APPROXIMATE_REFERENCE')
            if self.reference_worker is not None:
                from dataclasses import replace
                reference = replace(reference, status='UNAVAILABLE',
                    reason='REFERENCE_BUSY' if current_reference_result == 'BUSY' else 'REFERENCE_PENDING')
            self.store.finalize_slot(slot_id, SlotCommit(market.market_id, target, self.run_id,
                self.anchor_id, self.revision_id, books, reference, started_ns, ended_ns),
                committed_wall_us=self.wall_us())
            self.finalized += 1
            if self.approximate_worker is not None:
                self._approximate_drain()
            if self.reference_worker is not None and self.offline_reference.live is not None:
                current_reference_result = self.offline_reference.fetch(row, reference_deadline_ns/1_000_000_000,
                    run_id=self.run_id, anchor_id=self.anchor_id, project=project, buffer_only=True)
                self._reference_drain(result=current_reference_result)
                if not current_reference_result[2] and current_reference_result[1].error_code == 'REFERENCE_PENDING':
                    self._reference_submit(row, reference_deadline_ns)
        self._shutdown("STOPPED", "STOP_REQUESTED")
        return RunResult(self.run_id, "STOPPED", 0, self.finalized, "STOP_REQUESTED")
