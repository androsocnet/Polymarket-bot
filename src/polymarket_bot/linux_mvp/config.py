"""PAPER_ONLY configuration. Any other mode is a hard startup error, not a flag."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re
from typing import Mapping

MODE = 'PAPER_ONLY'
_IDENTIFIER = re.compile(r'[A-Za-z0-9._:-]{1,64}')


class ConfigError(ValueError):
    """Unsafe or malformed configuration; the service must not start."""


def _text(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    value = default if raw is None else raw
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigError(f'{key} must be a non-empty, unpadded string')
    return value


def _decimal(env, key, default, *, low=None, high=None, low_open=False, high_open=False):
    text = _text(env, key, str(default))
    if not re.fullmatch(r'[0-9]+(\.[0-9]+)?', text):
        raise ConfigError(f'{key} is not a plain decimal number')
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f'{key} is not a decimal number') from exc
    if not value.is_finite():
        raise ConfigError(f'{key} must be finite')
    if low is not None and (value < low or (low_open and value == low)):
        raise ConfigError(f'{key} is below its minimum')
    if high is not None and (value > high or (high_open and value == high)):
        raise ConfigError(f'{key} is above its maximum')
    return value


def _integer(env, key, default, *, low, high):
    text = _text(env, key, str(default))
    if not re.fullmatch(r'[0-9]+', text):
        raise ConfigError(f'{key} must be a non-negative integer')
    value = int(text)
    if not low <= value <= high:
        raise ConfigError(f'{key} must be between {low} and {high}')
    return value


@dataclass(frozen=True)
class Settings:
    mode: str
    bot_id: str
    db_path: Path
    state_dir: Path
    initial_cash: Decimal
    max_order: Decimal
    max_exposure: Decimal
    fee_bps: Decimal
    threshold: Decimal
    lookback: int
    holding_period: int
    max_trades_per_window: int
    max_daily_loss: Decimal
    sample_seconds: int
    poll_seconds: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    max_source_age_seconds: float
    max_consecutive_errors: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, base: Path | None = None) -> 'Settings':
        env = os.environ if env is None else env
        base = Path.cwd() if base is None else Path(base)
        mode = _text(env, 'MODE', MODE)
        if mode != MODE:
            raise ConfigError(f'MODE must be {MODE}; live or unknown modes are refused')
        bot_id = _text(env, 'MVP_BOT_ID', 'default')
        if not _IDENTIFIER.fullmatch(bot_id):
            raise ConfigError('MVP_BOT_ID must be 1-64 characters of [A-Za-z0-9._:-]')
        db_path = Path(_text(env, 'MVP_DB_PATH', str(base / 'paper.sqlite')))
        state_dir = Path(_text(env, 'MVP_STATE_DIR', str(base / 'state')))
        settings = cls(
            mode=mode,
            bot_id=bot_id,
            db_path=db_path,
            state_dir=state_dir,
            initial_cash=_decimal(env, 'MVP_INITIAL_CASH', '100', low=0, low_open=True),
            max_order=_decimal(env, 'MVP_MAX_ORDER', '10', low=0, low_open=True),
            max_exposure=_decimal(env, 'MVP_MAX_EXPOSURE', '10', low=0, low_open=True),
            fee_bps=_decimal(env, 'MVP_FEE_BPS', '10', low=0, high=10000),
            threshold=_decimal(env, 'MVP_THRESHOLD', '0.02', low=0, high=1, low_open=True, high_open=True),
            lookback=_integer(env, 'MVP_LOOKBACK', 3, low=3, high=60),
            holding_period=_integer(env, 'MVP_HOLDING_PERIOD', 3, low=1, high=449),
            max_trades_per_window=_integer(env, 'MVP_MAX_TRADES_PER_WINDOW', 6, low=0, high=450),
            max_daily_loss=_decimal(env, 'MVP_MAX_DAILY_LOSS', '10', low=0, low_open=True),
            sample_seconds=_integer(env, 'MVP_SAMPLE_SECONDS', 2, low=2, high=2),
            poll_seconds=_integer(env, 'MVP_POLL_SECONDS', 2, low=2, high=60),
            backoff_base_seconds=float(_decimal(env, 'MVP_BACKOFF_BASE_SECONDS', '2',
                                                low=0, high=60, low_open=True)),
            backoff_max_seconds=float(_decimal(env, 'MVP_BACKOFF_MAX_SECONDS', '30',
                                               low=1, high=300)),
            max_source_age_seconds=float(_decimal(env, 'MVP_MAX_SOURCE_AGE_SECONDS', '5',
                                                  low=0, high=30, low_open=True)),
            max_consecutive_errors=_integer(env, 'MVP_MAX_CONSECUTIVE_ERRORS', 5, low=1, high=100),
        )
        if settings.max_exposure < settings.max_order:
            raise ConfigError('MVP_MAX_EXPOSURE must be at least MVP_MAX_ORDER')
        if settings.backoff_base_seconds > settings.backoff_max_seconds:
            raise ConfigError('MVP_BACKOFF_BASE_SECONDS must not exceed MVP_BACKOFF_MAX_SECONDS')
        return settings

    @property
    def stop_path(self) -> Path:
        return self.state_dir / 'STOP'

    def as_dict(self) -> dict[str, object]:
        return {name: (str(value) if isinstance(value, (Decimal, Path)) else value)
                for name, value in self.__dict__.items()}
