"""Recorder outcome and request evidence records."""
from dataclasses import dataclass, field
from decimal import Decimal

from polymarket_bot.market_data.http import HttpAttempt
from polymarket_bot.market_data.models import MarketDescriptor, OutcomeBook

CHAINLINK_FEED_ID = "0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f"
CHAINLINK_FEED_NAME = "BTC/USD-Streams-TWAP-60s-mainnet-production"


@dataclass(frozen=True)
class MarketRevision:
    market: MarketDescriptor
    up_index: int
    down_index: int
    description: str
    resolution_source_url: str
    gamma_raw: bytes
    clob_config_raw: bytes | None
    observed_us: int
    config: dict
    discovery_source: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)
    capture_ready: bool = True
    last_refresh_attempt_us: int | None = None


@dataclass(frozen=True)
class ResolutionObservation:
    market_id: str
    condition_id: str
    raw_status: str | None
    payouts: tuple[Decimal, ...] | None
    reported_us: int | None
    learned_wall_us: int
    learned_server_us: int | None
    classification: str
    mapped_outcome: str | None
    raw_response: bytes
    provenance: dict
    raw_payouts_json: str | None = None


@dataclass(frozen=True)
class RunConfig:
    mode: str = "BOOK_ONLY"
    use_fast_path: bool = True
    policy_revision: str = field(default="recorder-v1", init=False)
    cadence_us: int = field(default=2_000_000, init=False)
    window_us: int = field(default=900_000_000, init=False)
    slot_count: int = field(default=450, init=False)
    capture_deadline_us: int = field(default=2_000_000, init=False)
    max_attempts: int = field(default=2, init=False)
    max_response_bytes: int = field(default=10_485_760, init=False)
    decimal_input_characters: int = field(default=256, init=False)
    decimal_coefficient_digits: int = field(default=128, init=False)
    decimal_exponent_limit: int = field(default=128, init=False)
    arithmetic_precision: int = field(default=512, init=False)
    anchor_refresh_us: int = field(default=60_000_000, init=False)
    anchor_max_age_us: int = field(default=120_000_000, init=False)
    future_source_allowance_us: int = field(default=5_000_000, init=False)
    reference_max_age_us: int = field(default=5_000_000, init=False)
    decoder_revision: str | None = field(default=None, init=False)


@dataclass(frozen=True)
class SlotTarget:
    index: int
    scheduled_us: int


@dataclass(frozen=True)
class SlotSelection:
    missed: tuple[int, ...]
    target: SlotTarget | None


@dataclass(frozen=True)
class ClockAnchor:
    server_seconds: int
    request_start_mono_ns: int
    request_end_mono_ns: int
    request_start_wall_us: int
    request_end_wall_us: int
    midpoint_ns: int
    server_us: int
    uncertainty_us: int
    status: str
    diagnostic: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReferenceAssociationDraft:
    market_id: str
    role: str
    target_us: int
    status: str
    origin: str
    available_live: bool
    learned_wall_us: int
    learned_server_us: int | None
    slot_id: int | None = None
    fetch_id: int | None = None
    report_id: int | None = None
    selection_policy_revision: str | None = None
    next_due_us: int | None = None
    attempt_count: int = 0
    reason: str | None = None
    report_hash: str | None = None

    @property
    def feed_id(self) -> str:
        return CHAINLINK_FEED_ID


@dataclass(frozen=True)
class ReferenceFetchDraft:
    attempt: HttpAttempt
    run_id: str
    feed_id: str
    purpose: str
    origin: str
    target_us: int | None
    status: str
    error_code: str | None
    raw_response: bytes | None
    received_server_us: int | None
    market_id: str | None = None
    anchor_id: int | None = None


@dataclass(frozen=True)
class ReferenceReportDraft:
    feed_id: str
    report_schema_version: int
    raw_signed_report: bytes
    report_hash: str
    valid_from_seconds: int | None
    observations_seconds: int | None
    expires_at_seconds: int | None
    decoded_price: Decimal | None
    decoder_revision: str | None
    decode_status: str
    verification_status: str
    first_received_wall_us: int
    first_received_server_us: int | None
    diagnostics: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class SlotCommit:
    market_id: str
    target: SlotTarget
    run_id: str
    anchor_id: int
    metadata_revision_id: int
    books: PairedBooks
    reference: ReferenceAssociationDraft
    started_mono_ns: int
    ended_mono_ns: int
