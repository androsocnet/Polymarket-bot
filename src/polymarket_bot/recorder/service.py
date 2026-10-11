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


class _StopRequested(Exception):
    """Internal normal-stop signal raised before an actual HTTP attempt."""


def _epoch_us(value):
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


class Recorder:
    def __init__(self, store: RecorderStore, public: PublicGet, *,
                 monotonic_ns: Callable[[], int], wall_us: Callable[[], int],
                 sleep: Callable[[float], None]):
        self.store, self.public = store, public
        self.monotonic_ns, self.wall_us, self.sleep = monotonic_ns, wall_us, sleep
        self.clock = RecorderClock(monotonic_ns=monotonic_ns, wall_us=wall_us)
        # Both transport timing representations derive from the same run epoch.
        public.monotonic_ns = monotonic_ns
        public.monotonic = lambda: monotonic_ns() / 1_000_000_000
        public.utcnow = lambda: datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=wall_us())
        public.sleep = sleep
        self.gamma = GammaClient(public)

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
        remaining = deadline_ns - self.monotonic_ns()
        if remaining > 0:
            self.sleep(remaining / 1_000_000_000)

    def _calibrate(self, deadline):
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
        deadline_ns = target_ns - 1_000_000
        if self.monotonic_ns() >= deadline_ns:
            return
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

    def run(self, config: RunConfig, *, should_stop: Callable[[], bool]) -> RunResult:
        self.run_id, self.finalized = "", 0
        try:
            return self._execute_run(config, should_stop=should_stop)
        except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
            # Includes failure to close; no successful durability claim escapes.
            return RunResult(self.run_id, "FAILED", 1, self.finalized,
                exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")

    def _execute_run(self, config, *, should_stop):
        if config.mode != "BOOK_ONLY":
            self.store.close()
            return RunResult("", "FAILED", 2, 0, "reference_runtime_unavailable")
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
                return RunResult(self.run_id, "FAILED", 1, self.finalized,
                                 exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")
            return RunResult(self.run_id, "INTERRUPTED", 130, self.finalized, "KEYBOARD_INTERRUPT")
        except (RecorderStoreError, sqlite3.DatabaseError, OSError) as exc:
            # No further persistence or HTTP follows a failed writer/invariant.
            return RunResult(self.run_id, "FAILED", 1, self.finalized,
                             exc.code if isinstance(exc, RecorderStoreError) else "storage_failure")
        finally:
            self.public.max_attempts = original_attempts
            self.public.before_attempt = original_guard
            self.store.close()

    def _shutdown(self, state, reason):
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
        self.run_id = self.store.start_run(config, wall_us=self.wall_us(), app_revision=config.policy_revision)
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
                        clock=self.clock, deadline=self.public.monotonic()+10,
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
            previous = self.public.on_attempt
            self.public.on_attempt = None
            try:
                books = collect_books(self.public, market,
                    deadline=self.clock.deadline_ns(min(target.scheduled_us+2_000_000, end_us))/1_000_000_000,
                    receipt_server_us=lambda moment: project(round(moment*1_000_000_000)),
                    uncertainty_us=self.clock.anchor.uncertainty_us)
            finally:
                self.public.on_attempt = previous
            ended_ns = self.monotonic_ns()
            reference = disabled_reference(market.market_id, target.scheduled_us,
                learned_wall_us=self.wall_us(), learned_server_us=project(ended_ns))
            self.store.finalize_slot(slot_id, SlotCommit(market.market_id, target, self.run_id,
                self.anchor_id, self.revision_id, books, reference, started_ns, ended_ns),
                committed_wall_us=self.wall_us())
            self.finalized += 1
        self._shutdown("STOPPED", "STOP_REQUESTED")
        return RunResult(self.run_id, "STOPPED", 0, self.finalized, "STOP_REQUESTED")
