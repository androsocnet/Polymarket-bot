"""Long-running PAPER service: one instance, signal-safe stop, bounded error backoff."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time

from .collector import CollectionStopped
from .config import Settings
from .store import MvpStore

TERMINAL = ('STOPPED', 'RISK_STOPPED', 'FAILED_STOP')


class InstanceLockError(RuntimeError):
    """Another service instance owns this state directory."""


@contextmanager
def single_instance(state_dir: Path):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    handle = (state_dir / 'service.lock').open('a+', encoding='utf8')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if handle.read(1) == '':
                handle.write('0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise InstanceLockError(f'state directory already in use: {state_dir}') from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()) + '\n')
        handle.flush()
        yield handle
    finally:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class Service:
    def __init__(self, store: MvpStore, collector, settings: Settings, *, run_id: str | None = None,
                 sleep=time.sleep, now_us=None, log=print, engine_factory=None):
        self.store = store
        self.collector = collector
        self.settings = settings
        self.run_id = run_id or f'{settings.bot_id}:{time.time_ns() // 1000}'
        self._sleep = sleep
        self._now_us = now_us
        self._log = log
        self._engine_factory = engine_factory
        if self._engine_factory is None:
            from .engine import PaperEngine
            self._engine_factory = PaperEngine
        self._stop = False
        self.last_result = None

    def request_stop(self, *_args) -> None:
        self._stop = True

    def run(self, *, max_cycles: int | None = None) -> str:
        self.store.start_run(self.run_id, self.settings.bot_id, initial_cash=self.settings.initial_cash,
                             config=self.settings.as_dict(), started_us=time.time_ns() // 1000)
        self._log(f'start run={self.run_id} bot={self.settings.bot_id} db={self.settings.db_path} '
                  f'mode={self.settings.mode}')
        engine = self._engine_factory(self.store, self.collector, self.settings, run_id=self.run_id,
                                      now_us=self._now_us or (lambda: time.time_ns() // 1000))
        setter = getattr(self.collector, 'set_stop_check', None)
        if setter is not None:
            setter(lambda: self._stop or self.settings.stop_path.exists())
        status = 'COMPLETED'
        while not self._stop:
            try:
                self.last_result = engine.step()
            except CollectionStopped:
                market_id = None if self.last_result is None else self.last_result.market_id
                status = 'STOPPED' if self.settings.stop_path.exists() else 'STOPPED_BY_SIGNAL'
                self.store.heartbeat(self.settings.bot_id, self.run_id,
                                     at_us=time.time_ns() // 1000, market_id=market_id,
                                     status=status, cycles=engine.cycles, last_error=None)
                break
            status = self.last_result.status
            if status in TERMINAL:
                break
            if engine.cycles % 30 == 0:
                self._log(f'cycles={engine.cycles} market={self.last_result.market_id} '
                          f'slot={self.last_result.slot_index} status={status} '
                          f'reason={self.last_result.reason}')
            if max_cycles is not None and engine.cycles >= max_cycles:
                status = 'CYCLE_LIMIT'
                break
            healthy = ('OK', 'DUPLICATE_SLOT', 'WINDOW_EXPIRED', 'OUT_OF_WINDOW', 'WINDOW_ABANDONED')
            if status in healthy:
                delay = float(self.settings.poll_seconds)
            else:
                failures = max(1, getattr(engine, 'consecutive_errors', 1))
                delay = min(self.settings.backoff_max_seconds,
                            self.settings.backoff_base_seconds * (2 ** min(failures - 1, 16)))
            self._pause(delay)
        if self._stop and status not in TERMINAL and status != 'STOPPED':
            status = 'STOPPED_BY_SIGNAL'
            self.store.heartbeat(self.settings.bot_id, self.run_id,
                                 at_us=time.time_ns() // 1000,
                                 market_id=None, status='STOPPED_BY_SIGNAL',
                                 cycles=engine.cycles, last_error=None)
        self._log(f'stop status={status} cycles={engine.cycles}')
        return status

    def _pause(self, seconds: float) -> None:
        """Sleep in short slices so SIGTERM/STOP is observed promptly even during backoff."""
        remaining = float(seconds)
        while remaining > 0 and not self._stop and not self.settings.stop_path.exists():
            slice_ = min(0.25, remaining)
            self._sleep(slice_)
            remaining -= slice_
