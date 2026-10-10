"""Pure decisions for the fixed two-second market-window grid."""
from datetime import datetime, timezone

from polymarket_bot.market_data.models import MarketDescriptor
from .models import SlotSelection, SlotTarget


def _integer(value: int) -> None:
    if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
        raise ValueError("Slot timing requires bounded integer microseconds and indices.")


def slot_grid(window_start_us: int) -> tuple[SlotTarget, ...]:
    _integer(window_start_us)
    _integer(window_start_us + 898_000_000)
    return tuple(SlotTarget(index, window_start_us + index * 2_000_000)
                 for index in range(450))


def select_future_slot(market: MarketDescriptor, *, now_us: int,
                       terminal_indices: set[int]) -> SlotSelection:
    _integer(now_us)
    for index in terminal_indices:
        _integer(index)
        if not 0 <= index < 450:
            raise ValueError("Terminal slot index must belong to the 450-slot grid.")
    delta = market.window_start - datetime(1970, 1, 1, tzinfo=timezone.utc)
    start_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    missed = []
    for target in slot_grid(start_us):
        if target.index in terminal_indices:
            continue
        if target.scheduled_us <= now_us:
            missed.append(target.index)
        else:
            return SlotSelection(tuple(missed), target)
    return SlotSelection(tuple(missed), None)


def classify_wake(target: SlotTarget, *, now_us: int, window_end_us: int) -> str:
    """Classify an already armed target; unarmed targets use strict selection."""
    _integer(target.scheduled_us)
    _integer(now_us)
    _integer(window_end_us)
    end_us = min(target.scheduled_us + 2_000_000, window_end_us)
    return "CAPTURE" if target.scheduled_us <= now_us < end_us else "MISSED"
