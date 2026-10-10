"""Recorder outcome and request evidence records."""
from dataclasses import dataclass

from polymarket_bot.market_data.http import HttpAttempt
from polymarket_bot.market_data.models import OutcomeBook


@dataclass(frozen=True)
class OutcomeResult:
    outcome: str
    token_id: str
    condition_id: str
    status: str
    book: OutcomeBook | None
    attempt_index: int | None
    error_code: str | None
    quality: dict
    book_metadata: dict


@dataclass(frozen=True)
class PairedBooks:
    up: OutcomeResult
    down: OutcomeResult
    attempts: tuple[HttpAttempt, ...]
