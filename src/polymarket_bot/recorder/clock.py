"""Run-local CLOB midpoint calibration; never adjusts the system clock."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Callable

from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.models import MarketDataError
from .models import ClockAnchor

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _integer(value, *, minimum=-(2**63)) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise MarketDataError("time_unavailable", "Recorder clock requires bounded integer timing.")
    return value


class RecorderClock:
    def __init__(self, *, monotonic_ns: Callable[[], int], wall_us: Callable[[], int]):
        self.monotonic_ns = monotonic_ns
        self.wall_us = wall_us
        # No loading/passing an anchor from a prior process monotonic epoch.
        self.anchor: ClockAnchor | None = None

    def calibrate(self, read: JsonRead) -> ClockAnchor:
        seconds = _integer(read.payload, minimum=1)
        if seconds > 253_402_300_799:
            raise MarketDataError("time_unavailable", "CLOB time is outside the UTC datetime range.")
        observed = (read.started_mono_ns, read.finished_mono_ns,
                    read.started_wall_us, read.finished_wall_us)
        diagnostic = {}
        if all(value is None for value in observed):
            # Compatibility only: do not imply nanosecond precision or that the
            # reconstructed start wall time was actually observed.
            for value in (read.started_mono, read.finished_mono):
                if (type(value) not in (float, int) or value < 0
                        or value > (2**63 - 1) / 1e9 or not isfinite(value)):
                    raise MarketDataError("time_unavailable", "Invalid legacy monotonic timing.")
            start_ns = int(read.started_mono * 1_000_000_000)
            end_ns = int(read.finished_mono * 1_000_000_000)
            end_wall = _integer(self.wall_us())
            start_wall = end_wall - (end_ns - start_ns) // 1000
            diagnostic = {"timing_source": "LEGACY_FLOAT_DERIVED",
                          "wall_start": "ESTIMATED_FROM_CALIBRATION_WALL_AND_RTT"}
        elif any(value is None for value in observed):
            raise MarketDataError("time_unavailable", "Incomplete observed request timing.")
        else:
            start_ns, end_ns, start_wall, end_wall = observed
        start_ns = _integer(start_ns, minimum=0)
        end_ns = _integer(end_ns, minimum=0)
        _integer(start_wall)
        _integer(end_wall)
        current_ns = _integer(self.monotonic_ns(), minimum=0)
        if end_ns < start_ns or current_ns < end_ns:
            raise MarketDataError("time_unavailable", "Request timing is reversed or outside this monotonic epoch.")
        if self.anchor is not None and start_ns < self.anchor.request_end_mono_ns:
            raise MarketDataError("time_unavailable", "Clock calibration cannot reuse an older monotonic epoch.")
        midpoint_ns = (start_ns + end_ns) // 2
        # Ceiling avoids understating uncertainty for sub-microsecond RTTs.
        uncertainty_us = 1_000_000 + (end_ns - start_ns + 1999) // 2000
        server_us = seconds * 1_000_000
        status = "VALID"
        if self.anchor is not None:
            previous_us = self.anchor.server_us + (midpoint_ns - self.anchor.midpoint_ns) // 1000
            shift_us = server_us - previous_us
            threshold = self.anchor.uncertainty_us + uncertainty_us + 1_000_000
            if abs(shift_us) > threshold:
                status = "DISCONTINUITY"
                diagnostic.update(shift_us=shift_us, threshold_us=threshold)
        anchor = ClockAnchor(seconds, start_ns, end_ns, start_wall, end_wall,
                             midpoint_ns, server_us, uncertainty_us, status, diagnostic)
        self.anchor = anchor
        return anchor

    def usable(self) -> bool:
        if self.anchor is None or self.anchor.status != "VALID":
            return False
        moment = _integer(self.monotonic_ns(), minimum=0)
        return (moment >= self.anchor.request_end_mono_ns
                and moment - self.anchor.midpoint_ns <= 120_000_000_000)

    def refresh_due(self) -> bool:
        if self.anchor is None or not self.usable():
            return True
        return self.monotonic_ns() - self.anchor.midpoint_ns >= 60_000_000_000

    def _usable_anchor(self) -> ClockAnchor:
        if not self.usable():
            raise MarketDataError("time_unavailable", "Recorder clock needs a fresh valid calibration.")
        return self.anchor

    def server_us_at(self, mono_ns: int) -> int:
        anchor = self._usable_anchor()
        _integer(mono_ns, minimum=0)
        return _integer(anchor.server_us + (mono_ns - anchor.midpoint_ns) // 1000)

    def deadline_ns(self, target_us: int) -> int:
        anchor = self._usable_anchor()
        _integer(target_us)
        return _integer(anchor.midpoint_ns + (target_us - anchor.server_us) * 1000)

    def now(self) -> datetime:
        try:
            return EPOCH + timedelta(microseconds=self.server_us_at(self.monotonic_ns()))
        except OverflowError as exc:
            raise MarketDataError("time_unavailable", "Estimated server time is outside the UTC datetime range.") from exc
