"""One recorder connection and OS writer ownership; no generic SQL interface."""
import os
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import json
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
import zlib
from uuid import uuid4

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import decimal_text, exact_decimal_context, parse_decimal
from .models import (CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME, ClockAnchor, RunConfig,
                     MarketRevision, ReferenceFetchDraft, ReferenceReportDraft,
                     ResolutionObservation, SlotCommit, SlotTarget)
from .metadata import _normalize_clob_numbers, _selected_market
from .free_reference import ApproximateObservation, ApproximateReferenceReport


MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise RecorderStoreError("fatal_invariant", "Recorder JSON evidence is not supported.") from exc


def _epoch_us(value: datetime) -> int:
    _require(isinstance(value, datetime) and value.tzinfo is not None,
             "Book source time must be timezone aware.")
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _decimal(value) -> str | None:
    try:
        return None if value is None else decimal_text(value)
    except MarketDataError as exc:
        raise RecorderStoreError("fatal_invariant", "Book summary must be finite exact Decimal evidence.") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecorderStoreError("fatal_invariant", message)


def _integer(value, field: str, *, minimum: int = -(2**63)) -> None:
    _require(type(value) is int and minimum <= value <= 2**63 - 1,
             f"Invalid recorder integer {field}.")


class RecorderStoreError(Exception):
    """A structured persistence or exclusive-ownership failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class EvidenceError(RecorderStoreError):
    """Original response bytes cannot be encoded or safely recovered."""

    def __init__(self, message: str):
        super().__init__("invalid_evidence", message)


def encode_evidence(raw: bytes) -> tuple[bytes, int, int]:
    """Compress original bytes without parsing or reformatting their values."""
    if type(raw) is not bytes or len(raw) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("Evidence must be bytes within the 10 MiB body ceiling.")
    return zlib.compress(raw), len(raw), 1


def decode_evidence(blob: bytes, raw_size: int, codec_version: int) -> bytes:
    """Recover one complete zlib stream with bounded, byte-exact length."""
    if (type(blob) is not bytes or type(raw_size) is not int
            or not 0 <= raw_size <= MAX_EVIDENCE_BYTES
            or type(codec_version) is not int or codec_version != 1):
        raise EvidenceError("Unsupported evidence codec or uncompressed byte count.")
    decompressor = zlib.decompressobj()
    try:
        # One detection byte rejects dishonest stored lengths and expansion bombs.
        raw = decompressor.decompress(blob, raw_size + 1)
    except zlib.error as exc:
        raise EvidenceError("Invalid compressed evidence.") from exc
    if (len(raw) != raw_size or not decompressor.eof
            or decompressor.unconsumed_tail or decompressor.unused_data):
        raise EvidenceError("Incomplete, trailing or oversized compressed evidence.")
    return raw


def _schema_contract(script: str) -> dict[tuple[str, str], str]:
    """Definitions from this fixed schema, including its CHECK/unique clauses."""
    definitions = {}
    for statement in script.split(";"):
        normalized = " ".join(statement.split())
        if not normalized:
            continue
        if not normalized.startswith("CREATE "):
            continue
        match = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX) ([a-z_]+) ", normalized)
        if match is None:
            raise RecorderStoreError("invalid_schema", "Invalid bundled schema definition.")
        definitions[(match[1].lower(), match[2])] = normalized
    return definitions


def _split_schema(script: str) -> tuple[str, str]:
    """Split schema.sql into v1 portion (before marker) and v2 portion (full script)."""
    parts = script.split("-- APPROXIMATE MODE ADDITIONS (v2 only)")
    if len(parts) != 2:
        raise RecorderStoreError("invalid_schema", "Schema marker not found.")
    v1_script = parts[0]
    v2_script = script  # full script includes v2 additions
    return v1_script, v2_script


def _load_v1_schema() -> str:
    """Load v1-only schema (without approximate tables)."""
    script = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    return _split_schema(script)[0]


def _load_v2_schema() -> str:
    """Load full v2 schema (with approximate tables)."""
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


class RecorderStore:
    """Own the canonical database lock until close, including commit gaps."""

    def __init__(self, path: Path, lock_file, connection: sqlite3.Connection):
        self.path = path
        self._lock_file = lock_file
        self._connection = connection
        self._write_failed = False
        self._read_only = False
        self._closed = False

    @classmethod
    def read_only(cls, path: Path) -> "RecorderStore":
        """Open existing evidence without initialization or writer ownership."""
        canonical = Path(os.path.normcase(str(Path(path).resolve())))
        connection = None
        try:
            connection = sqlite3.connect(canonical.as_uri() + "?mode=ro", uri=True, timeout=0)
            connection.execute("PRAGMA query_only=ON")
            script = _load_v2_schema()
            cls._verify_existing(connection, script, approximate=False)
            store = cls(canonical, None, connection)
            store._read_only = True
            return store
        except (OSError, sqlite3.DatabaseError, RecorderStoreError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, RecorderStoreError):
                raise
            raise RecorderStoreError("storage_failure", "Cannot read existing recorder database.") from exc

    @classmethod
    def open(cls, path: Path) -> "RecorderStore":
        """Open a v1 database for BOOK_ONLY mode (accepts existing v1 or v2)."""
        return cls._open(path, approximate=False)

    @classmethod
    def open_approximate(cls, path: Path) -> "RecorderStore":
        """Open or create a v2 database for approximate mode."""
        return cls._open(path, approximate=True)

    @classmethod
    def _open(cls, path: Path, *, approximate: bool) -> "RecorderStore":
        canonical = Path(os.path.normcase(str(Path(path).resolve())))
        lock_file = None
        connection = None
        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(str(canonical) + ".writer.lock", "a+b", buffering=0)
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
            lock_file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RecorderStoreError("writer_locked", "The recorder database already has a writer.") from exc

            v2_script = _load_v2_schema()
            v1_script = _load_v1_schema()
            is_new = not canonical.exists()
            # Claim a new file without truncating a path created by another actor.
            if is_new:
                descriptor = os.open(canonical, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            connection = sqlite3.connect(canonical.as_uri() + "?mode=rw", uri=True, timeout=0)
            connection.execute("PRAGMA foreign_keys=ON")
            if is_new:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                if approximate:
                    connection.executescript("BEGIN IMMEDIATE;\n" + v2_script + "\nPRAGMA user_version=2;\nCOMMIT;")
                else:
                    connection.executescript("BEGIN IMMEDIATE;\n" + v1_script + "\nPRAGMA user_version=1;\nCOMMIT;")
            else:
                cls._verify_existing(connection, v2_script, approximate=approximate)
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
            return cls(canonical, lock_file, connection)
        except Exception as exc:
            if connection is not None:
                connection.close()
            if lock_file is not None:
                lock_file.close()
            if isinstance(exc, RecorderStoreError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise RecorderStoreError("database_integrity", "Cannot initialize or verify recorder database.") from exc
            if isinstance(exc, OSError):
                raise RecorderStoreError("storage_failure", "Cannot acquire or open recorder database storage.") from exc
            raise

    @staticmethod
    def _verify_existing(connection: sqlite3.Connection, v2_script: str, *, approximate: bool = False) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1 and approximate:
            raise RecorderStoreError("schema_version_mismatch",
                "Approximate mode requires a v2 database; existing database is v1.")
        if version not in (1, 2):
            raise RecorderStoreError("unknown_schema", "Existing recorder database schema version is unsupported.")
        if version == 1:
            v1_script = _split_schema(v2_script)[0]
            expected = _schema_contract(v1_script)
        else:
            expected = _schema_contract(v2_script)
        actual = {(kind, name): " ".join(sql.split()) for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if actual != expected:
            raise RecorderStoreError("invalid_schema",
                f"Existing recorder database does not match schema version {version}.")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RecorderStoreError("database_integrity", "Recorder database integrity check failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RecorderStoreError("database_integrity", "Recorder database foreign-key integrity check failed.")

    def close(self) -> None:
        if self._closed:
            return
        # Failed connection closure (for example a wrong thread) retains ownership.
        self._connection.close()
        if self._lock_file is not None:
            self._lock_file.close()
        self._lock_file = None
        self._closed = True

    @contextmanager
    def _transaction(self):
        self._check_writer()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
            self._connection.commit()
        except sqlite3.DatabaseError as exc:
            self._write_failed = True
            self._connection.rollback()
            raise RecorderStoreError("storage_failure", "Recorder transaction could not persist.") from exc
        except BaseException:
            self._connection.rollback()
            raise

    def _check_writer(self) -> None:
        if self._read_only:
            raise RecorderStoreError("read_only", "Measurement reader cannot mutate evidence.")
        if self._write_failed:
            raise RecorderStoreError("storage_failure", "Writer stopped after a storage failure.")
        _require(self._lock_file is not None, "Recorder writer is closed.")

    def _rows(self, sql: str, params=()) -> list[dict]:
        if not self._read_only:
            self._check_writer()
        _require(not self._closed, "Recorder reader is closed.")
        try:
            cursor = self._connection.execute(sql, params)
            keys = [column[0] for column in cursor.description]
            return [dict(zip(keys, row)) for row in cursor]
        except sqlite3.DatabaseError as exc:
            self._write_failed = True
            raise RecorderStoreError("storage_failure", "Cannot read recorder transaction state.") from exc

    def _insert(self, table: str, values: dict) -> int:
        self._check_writer()
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        cursor = self._connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                                          tuple(values.values()))
        return cursor.lastrowid

    def save_checkpoint(self, *, run_id: str, wall_us: int, values: dict) -> int:
        """Persist one pre-transaction physical/counter observation."""
        self._check_writer()
        _require(not self._connection.in_transaction, "Checkpoint requires a transaction gap.")
        _integer(wall_us, "checkpoint wall")
        with self._transaction():
            self._run(run_id, running=False)
            return self._insert("storage_checkpoints", dict(values, run_id=run_id, observed_wall_us=wall_us))

    def update_checkpoint_physical(self, checkpoint_id: int, values: dict) -> None:
        """Include the checkpoint row's own committed page overhead."""
        self._check_writer()
        _require(not self._connection.in_transaction, "Checkpoint readings must be outside a transaction.")
        names = ("db_file_bytes","page_size","page_count","freelist_count","journal_bytes")
        _require(set(values)==set(names), "Checkpoint physical fields are fixed.")
        for name in names:
            _integer(values[name],name,minimum=0)
        with self._transaction():
            cursor = self._connection.execute("UPDATE storage_checkpoints SET " +
                ",".join(name+"=?" for name in names)+" WHERE checkpoint_id=?",
                tuple(values[name] for name in names)+(checkpoint_id,))
            _require(cursor.rowcount==1, "Checkpoint is unknown.")

    def _one(self, sql: str, params=()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _equal(existing: dict, expected: dict) -> bool:
        return all(existing.get(key) == value for key, value in expected.items())

    def _market_target(self, market_id: str, target: SlotTarget) -> dict:
        market = self._one("SELECT * FROM markets WHERE market_id=?", (market_id,))
        _require(market is not None, "Slot market identity is unknown.")
        _integer(target.index, "slot_index", minimum=0)
        _integer(target.scheduled_us, "scheduled_us")
        _require(target.index < 450 and target.scheduled_us == market["window_start_us"] + target.index * 2_000_000,
                 "Slot target violates the market window formula.")
        return market

    def _run(self, run_id: str, *, running: bool = True) -> dict:
        run = self._one("SELECT * FROM recording_runs WHERE run_id=?", (run_id,))
        _require(run is not None and (not running or run["state"] == "RUNNING"),
                 "Slot run is unknown or already terminal.")
        return run

    def _revision(self, market_id: str, revision_id: int) -> None:
        _integer(revision_id, "metadata_revision_id", minimum=1)
        revision = self._one("SELECT market_id FROM market_revisions WHERE revision_id=?", (revision_id,))
        _require(revision is not None and revision["market_id"] == market_id,
                 "Metadata revision does not belong to the slot market.")

    def _anchor(self, run_id: str, anchor_id: int) -> dict:
        _integer(anchor_id, "anchor_id", minimum=1)
        anchor = self._one("SELECT * FROM clock_anchors WHERE anchor_id=?", (anchor_id,))
        _require(anchor is not None and anchor["run_id"] == run_id and anchor["status"] == "VALID",
                 "Clock anchor does not belong to the slot run or is invalid.")
        return anchor

    def start_run(self, config: RunConfig, *, wall_us: int, app_revision: str, offline_reference=None) -> str:
        self._check_writer()
        from .reference import OfflineReferenceSession, ManagedReferenceSession
        offline = isinstance(offline_reference, OfflineReferenceSession) and config.mode == 'REFERENCE_REQUIRED'
        managed = isinstance(offline_reference, ManagedReferenceSession)
        if offline:
            from .reference import require_reference_activation
            require_reference_activation(config.mode, offline_reference.evidence)
            if managed:
                _require(offline_reference.started, 'Managed reference must complete startup before persistence.')
                pinned = self._one('SELECT price_scale,decoder_revision FROM reference_feeds WHERE feed_id=?', (CHAINLINK_FEED_ID,))
                _require(pinned is None or pinned['price_scale'] is None or
                    (pinned['price_scale'] == offline_reference.price_scale and pinned['decoder_revision'] == offline_reference.decoder_revision),
                    'Discovery conflicts with persisted reference pin.')
        _require(isinstance(config, RunConfig) and (config.mode in ("BOOK_ONLY", "APPROXIMATE_REFERENCE") or offline) and type(config.use_fast_path) is bool,
                 "Only BOOK_ONLY or APPROXIMATE_REFERENCE can start without verified reference activation gates.")
        _integer(wall_us, "started_wall_us")
        _require(isinstance(app_revision, str) and bool(app_revision), "Application revision is required.")
        run_id = str(uuid4())
        with self._transaction():
            self._insert("recording_runs", dict(run_id=run_id, started_wall_us=wall_us,
                mode=config.mode, reference_activation="VERIFIED" if offline else "DISABLED", state="RUNNING",
                app_revision=app_revision, config_json=_json(asdict(config)),
                activation_evidence_json=_json({'scope': 'MANAGED_PRODUCTION' if managed else 'SYNTHETIC_OFFLINE_ONLY',
                    'live_activation': managed, 'gates': asdict(offline_reference.evidence)}) if offline else None))
            self._connection.execute("""INSERT OR IGNORE INTO reference_feeds
                (feed_id,name,environment,source_url,catalog_metadata_json,lookback_seconds,report_schema_version)
                VALUES (?,?, 'mainnet',?, '{}',60,2)""",
                (CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME, "https://data.chain.link/streams/btc-usd-twap-60s-streams"))
            if offline:
                self._connection.execute('UPDATE reference_feeds SET verified_us=NULL,price_scale=?,decoder_revision=?,catalog_metadata_json=?,verification_evidence_json=? WHERE feed_id=?',
                    (offline_reference.price_scale, offline_reference.decoder_revision,
                     _json(offline_reference.metadata.catalog_metadata),
                     _json({'scope': 'MANAGED_PRODUCTION' if managed else 'SYNTHETIC_OFFLINE_ONLY', 'signed_binary_decoder_verified': managed, 'structural_decoder_validation': asdict(offline_reference.authorization) if managed else None, 'discovery_calibration': 'PENDING',
                            'don_verified': False}), CHAINLINK_FEED_ID))
        return run_id

    def save_anchor(self, run_id: str, anchor: ClockAnchor) -> int:
        """Append run-scoped integer timing evidence, including discontinuities."""
        with self._transaction():
            self._run(run_id)
            _require(isinstance(anchor, ClockAnchor), "Clock anchor record is required.")
            for name in ("server_seconds", "request_start_mono_ns", "request_end_mono_ns",
                         "midpoint_ns", "server_us", "uncertainty_us"):
                _integer(getattr(anchor, name), name, minimum=0)
            for name in ("request_start_wall_us", "request_end_wall_us"):
                _integer(getattr(anchor, name), name)
            _require(0 < anchor.server_seconds <= 253_402_300_799
                     and anchor.server_us == anchor.server_seconds * 1_000_000,
                     "Clock server seconds and microseconds are inconsistent.")
            rtt_ns = anchor.request_end_mono_ns - anchor.request_start_mono_ns
            _require(rtt_ns >= 0 and anchor.midpoint_ns == (
                     anchor.request_start_mono_ns + anchor.request_end_mono_ns) // 2,
                     "Clock anchor must use the request midpoint.")
            _require(anchor.uncertainty_us == 1_000_000 + (rtt_ns + 1999) // 2000,
                     "Clock uncertainty must include one second plus half RTT.")
            _require(anchor.status in ("VALID", "DISCONTINUITY")
                     and type(anchor.diagnostic) is dict, "Unsupported clock anchor status or diagnostic.")
            return self._insert("clock_anchors", dict(run_id=run_id,
                server_seconds=anchor.server_seconds,
                request_start_mono_ns=anchor.request_start_mono_ns,
                request_end_mono_ns=anchor.request_end_mono_ns,
                request_start_wall_us=anchor.request_start_wall_us,
                request_end_wall_us=anchor.request_end_wall_us,
                anchor_mono_ns=anchor.midpoint_ns, anchor_server_us=anchor.server_us,
                uncertainty_us=anchor.uncertainty_us, status=anchor.status,
                diagnostic_json=_json(anchor.diagnostic) if anchor.diagnostic else None))

    def save_market_revision(self, run_id: str, revision: MarketRevision) -> int:
        """Keep market identity immutable and append only changed raw metadata."""
        self._check_writer()
        _require(isinstance(revision, MarketRevision), "Market revision is required.")
        market = revision.market
        _require(revision.up_index in (0, 1) and revision.down_index in (0, 1)
                 and revision.up_index != revision.down_index,
                 "Original outcome indices must be distinct.")
        observed_us = revision.observed_us
        _integer(observed_us, "observed_us")
        start_us, end_us = _epoch_us(market.window_start), _epoch_us(market.window_end)
        _require(end_us - start_us == 900_000_000,
                 "Market window must be exactly 900 seconds.")
        gamma_z, gamma_size, codec = encode_evidence(revision.gamma_raw)
        clob_z = None
        clob_size = None
        if revision.clob_config_raw is not None:
            clob_z, clob_size, _ = encode_evidence(revision.clob_config_raw)
        raw_sizes = {"gamma": gamma_size, "clob": clob_size}
        # Discovery can select an event envelope and refresh a market detail.
        # Compare the selected market content, while retaining each actual raw body.
        conflict = "identity_conflict" in revision.warnings
        try:
            selected = _selected_market(revision.gamma_raw, market.market_id)
            canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str).encode()
        except MarketDataError:
            if not conflict:
                raise
            canonical = revision.gamma_raw
        digest = sha256(canonical + b"\x00" + (revision.clob_config_raw or b"")).hexdigest()
        config = json.loads(revision.clob_config_raw.decode("utf-8"), parse_float=str) if revision.clob_config_raw else {}
        _require(isinstance(config, dict), "CLOB config evidence must be an object.")
        config, numeric_warnings = _normalize_clob_numbers(config)
        identity = dict(market_id=market.market_id, event_id=market.event_id,
                        condition_id=market.condition_id, slug=market.slug,
                        question=market.question, window_start_us=start_us,
                        window_end_us=end_us, up_token_id=market.up.token_id,
                        down_token_id=market.down.token_id,
                        up_outcome_index=revision.up_index,
                        down_outcome_index=revision.down_index)
        with self._transaction():
            self._run(run_id)
            existing = self._one("SELECT * FROM markets WHERE market_id=?", (market.market_id,))
            if existing is None:
                _require(not conflict, "Cannot first acquire a conflicting market revision.")
                self._insert("markets", dict(identity, first_seen_us=observed_us,
                                             lifecycle="CURRENT", resolution_state="PENDING",
                                             resolution_attempts=0))
            elif existing["lifecycle"] == "INVALIDATED":
                # Preserve newly observed raw metadata without ever reviving capture.
                conflict = True
            if existing is not None and not self._equal(existing, {key: identity[key] for key in (
                    "market_id", "event_id", "condition_id", "window_start_us", "window_end_us",
                    "up_token_id", "down_token_id", "up_outcome_index", "down_outcome_index")}):
                conflict = True
            prior = self._one("""SELECT revision_id,content_hash FROM market_revisions
                                  WHERE market_id=? ORDER BY revision_id DESC LIMIT 1""",
                              (market.market_id,))
            if prior is not None and prior["content_hash"] == digest:
                revision_id = prior["revision_id"]
                if not conflict:
                    return revision_id
            else:
                revision_id = self._insert("market_revisions", dict(
                    market_id=market.market_id, observed_us=observed_us, run_id=run_id,
                    description=revision.description, resolution_source_url=revision.resolution_source_url,
                    active=int(market.active), closed=int(market.closed),
                    accepting_orders=None if market.accepting_orders is None else int(market.accepting_orders),
                    enable_order_book=None if market.enable_order_book is None else int(market.enable_order_book),
                    fee_config_json=_json({key: config[key] for key in ("mbf", "tbf", "fd") if key in config}),
                    tick_size_text=str(config["mts"]) if "mts" in config else None,
                    min_order_size_text=str(config["mos"]) if "mos" in config else None,
                    delay_config_json=_json({key: config[key] for key in ("oas", "itode", "ibce") if key in config}),
                    gamma_raw_z=gamma_z, clob_config_raw_z=clob_z,
                    raw_sizes_json=_json(raw_sizes), codec_version=codec, content_hash=digest))
                if numeric_warnings:
                    self._insert("recorder_events", dict(run_id=run_id, market_id=market.market_id,
                        kind="DISCOVERY_GAP", started_us=observed_us,
                        detail_json=_json({"warnings": numeric_warnings, "capture_paused": True,
                                           "time_domain": "SERVER", "revision_id": revision_id,
                                           "diagnostic_only": True, "reason": "INVALID_CLOB_NUMERIC"})))
            if conflict:
                self._connection.execute("UPDATE markets SET lifecycle='INVALIDATED' WHERE market_id=?",
                                         (market.market_id,))
        if conflict:
            raise RecorderStoreError("identity_conflict", "Market identity changed; capture is invalidated.")
        return revision_id

    def expire_market(self, market_id: str, *, observed_us: int) -> None:
        """Queue resolution at the window end independently of open flags."""
        _integer(observed_us, "observed_us")
        with self._transaction():
            market = self._one("SELECT * FROM markets WHERE market_id=?", (market_id,))
            _require(market is not None, "Cannot expire an unknown market.")
            _require(observed_us >= market["window_end_us"], "Market has not expired.")
            if market["lifecycle"] == "CURRENT":
                self._connection.execute("""UPDATE markets SET lifecycle='EXPIRED',
                    resolution_next_due_us=? WHERE market_id=?""",
                    (market["window_end_us"], market_id))
            elif market["lifecycle"] == "INVALIDATED" and market["resolution_next_due_us"] is None:
                self._connection.execute("""UPDATE markets SET resolution_next_due_us=?
                    WHERE market_id=?""", (market["window_end_us"], market_id))

    def due_resolutions(self, now_us: int, *, limit: int = 20) -> tuple[MarketRevision, ...]:
        """Read the persisted queue without inspecting reference availability."""
        _integer(now_us, "resolution now")
        _require(type(limit) is int and 1 <= limit <= 20, "Resolution batch limit must be 1..20.")
        from .resolution import _revision_from_row
        rows = self._rows("""SELECT * FROM markets WHERE lifecycle IN ('EXPIRED','INVALIDATED')
            AND resolution_state IN ('PENDING','RETRY_WAIT')
            AND resolution_next_due_us IS NOT NULL AND resolution_next_due_us<=?
            ORDER BY resolution_next_due_us,window_end_us,market_id LIMIT ?""", (now_us, limit))
        return tuple(_revision_from_row(row) for row in rows)

    def save_resolutions(self, observations: tuple[ResolutionObservation, ...], *,
                         requests: tuple) -> None:
        """Append one batch's attempts and all condition-grain results atomically."""
        self._check_writer()
        _require(type(observations) is tuple and bool(observations)
                 and len(observations) <= 20 and type(requests) is tuple and bool(requests),
                 "Resolution batch requires observations and actual request evidence.")
        conditions = tuple(obs.condition_id for obs in observations)
        _require(len(set(conditions)) == len(conditions), "Resolution conditions must be distinct.")
        selector = "condition=" + ",".join(conditions)
        attempt_values = []
        for attempt in requests:
            _require(attempt.operation == "RESOLUTION" and attempt.method == "GET"
                     and attempt.path == "/v2/resolutions" and attempt.selectors == (selector,)
                     and type(attempt.raw_complete) is bool,
                     "Resolution attempt must use the fixed public condition selector.")
            for name in ("started_wall_us", "ended_wall_us", "started_mono_ns", "ended_mono_ns"):
                _integer(getattr(attempt, name), name)
            _require(attempt.ended_mono_ns >= attempt.started_mono_ns,
                     "Resolution attempt times are reversed.")
            _integer(attempt.attempt_number, "resolution attempt number", minimum=1)
            _require(attempt.attempt_number <= 3, "Resolution HTTP retry policy exceeded.")
            z, size, codec = encode_evidence(attempt.raw_body)
            attempt_values.append(dict(operation="RESOLUTION", attempt_number=attempt.attempt_number,
                method="GET", path="/v2/resolutions", selector_json=_json(attempt.selectors),
                started_wall_us=attempt.started_wall_us, ended_wall_us=attempt.ended_wall_us,
                start_mono_ns=attempt.started_mono_ns, end_mono_ns=attempt.ended_mono_ns,
                latency_ns=attempt.ended_mono_ns-attempt.started_mono_ns,
                http_status=attempt.http_status, result=attempt.result,
                error_code=attempt.error_code, response_bytes=size,
                response_raw_z=z, raw_size_bytes=size, codec_version=codec,
                raw_complete=int(attempt.raw_complete)))
        prepared = []
        for observation in observations:
            _require(isinstance(observation, ResolutionObservation), "Resolution observation record is required.")
            _integer(observation.learned_wall_us, "resolution learned wall")
            if observation.learned_server_us is not None:
                _integer(observation.learned_server_us, "resolution learned server")
            if observation.reported_us is not None:
                _integer(observation.reported_us, "reported resolution time", minimum=0)
            _require(observation.classification in ("ABSENT", "PENDING", "INVALID",
                     "RESOLVED_DIRECTIONAL", "RESOLVED_OTHER")
                     and (observation.mapped_outcome in ("UP", "DOWN")
                          if observation.classification == "RESOLVED_DIRECTIONAL"
                          else observation.mapped_outcome is None),
                     "Resolution classification and winner disagree.")
            z, size, codec = encode_evidence(observation.raw_response)
            prepared.append((observation, z, size, codec))
        with self._transaction():
            run = self._one("SELECT run_id FROM recording_runs WHERE state='RUNNING' ORDER BY started_wall_us DESC LIMIT 1")
            _require(run is not None, "Resolution batch requires an active writer run.")
            request_ids = [self._insert("request_attempts", dict(values, run_id=run["run_id"]))
                           for values in attempt_values]
            for observation, z, size, codec in prepared:
                market = self._one("SELECT * FROM markets WHERE market_id=?", (observation.market_id,))
                _require(market is not None and market["condition_id"] == observation.condition_id
                         and market["lifecycle"] in ("EXPIRED", "INVALIDATED"),
                         "Resolution observation has unknown or unexpired condition.")
                payouts_json = observation.raw_payouts_json
                if payouts_json is None and observation.payouts is not None:
                    _require(len(observation.payouts) == 2,
                             "Resolution payout vector must retain two entries.")
                    payouts_json = "[" + ",".join(decimal_text(value) for value in observation.payouts) + "]"
                reported_raw = observation.provenance.get("reported_time_raw")
                if type(reported_raw) is not str or len(reported_raw) > 64:
                    reported_raw = None
                resolution_id = self._insert("resolution_observations", dict(
                    market_id=observation.market_id, request_id=request_ids[-1],
                    condition_id=observation.condition_id, status_raw=observation.raw_status,
                    payouts_raw_json=payouts_json, provenance_json=_json(observation.provenance | (
                        {"wall_clock_movement": {"request_backward": any(
                            a.ended_wall_us < a.started_wall_us for a in requests),
                            "learned_before_request_wall": observation.learned_wall_us < requests[-1].ended_wall_us}}
                        if any(a.ended_wall_us < a.started_wall_us for a in requests)
                        or observation.learned_wall_us < requests[-1].ended_wall_us else {})),
                    reported_resolved_us=observation.reported_us, reported_time_raw=reported_raw,
                    learned_wall_us=observation.learned_wall_us,
                    learned_server_us=observation.learned_server_us,
                    mapped_outcome=observation.mapped_outcome,
                    classification=observation.classification,
                    response_hash=sha256(observation.raw_response).hexdigest(),
                    raw_resolution_z=z, raw_size_bytes=size, codec_version=codec))
                prior_state = market["resolution_state"]
                if observation.classification in ("RESOLVED_DIRECTIONAL", "RESOLVED_OTHER"):
                    selected_id = market["selected_resolution_id"]
                    if selected_id is not None:
                        selected = self._one("SELECT classification,mapped_outcome,payouts_raw_json FROM resolution_observations WHERE resolution_id=?",
                                             (selected_id,))
                        _require(selected is not None and type(selected["payouts_raw_json"]) is str
                                 and len(selected["payouts_raw_json"]) <= 512
                                 and observation.payouts is not None,
                                 "Selected final resolution lacks payout evidence.")
                        try:
                            prior_raw = json.loads(selected["payouts_raw_json"],
                                parse_int=str, parse_float=str, parse_constant=lambda value: value)
                            _require(type(prior_raw) is list and len(prior_raw) == 2,
                                     "Selected final payout vector is invalid.")
                            prior_payouts = tuple(parse_decimal(value, field="selected resolution payout")
                                                  for value in prior_raw)
                            with exact_decimal_context():
                                equivalent_payouts = (tuple(value > 0 for value in prior_payouts)
                                    == tuple(value > 0 for value in observation.payouts)
                                    and prior_payouts[0] * observation.payouts[1]
                                    == prior_payouts[1] * observation.payouts[0])
                        except (MarketDataError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
                            raise RecorderStoreError("fatal_invariant", "Selected final payout evidence is invalid.") from exc
                        conflict = ((selected["classification"], selected["mapped_outcome"]) !=
                            (observation.classification, observation.mapped_outcome)
                            or not equivalent_payouts)
                    else:
                        conflict = False
                    state = "CONFLICT" if prior_state == "CONFLICT" or conflict else observation.classification
                    next_due = None
                    selected_id = selected_id if selected_id is not None else resolution_id
                elif prior_state in ("RESOLVED_DIRECTIONAL", "RESOLVED_OTHER", "CONFLICT"):
                    state, next_due, selected_id = prior_state, None, market["selected_resolution_id"]
                else:
                    state, selected_id = "RETRY_WAIT", None
                    delay = min(30 * (2 ** min(market["resolution_attempts"], 10)), 21_600)
                    learned_us = (observation.learned_server_us if observation.learned_server_us is not None
                                  else observation.learned_wall_us)
                    next_due = max(learned_us,
                                   market["resolution_next_due_us"] or learned_us) + delay * 1_000_000
                self._connection.execute("""UPDATE markets SET resolution_state=?,
                    resolution_next_due_us=?,resolution_attempts=resolution_attempts+1,
                    selected_resolution_id=? WHERE market_id=?""",
                    (state, next_due, selected_id, observation.market_id))

    def save_reference_evidence(self, fetch: ReferenceFetchDraft,
                                reports: tuple[ReferenceReportDraft, ...],
                                associations: tuple) -> None:
        """Persist supplied evidence atomically; this performs no retrieval.

        Draft association hashes select reports from this fetch. Database IDs
        are assigned here; supplied IDs must agree with any supplied hash.
        First receipt/fetch and original slot coverage are never updated.
        """
        self._check_writer()
        _require(isinstance(fetch, ReferenceFetchDraft) and fetch.feed_id == CHAINLINK_FEED_ID,
                 "Reference fetch must pin the approved feed.")
        _require(fetch.purpose in ("SLOT_CURRENT", "OPENING", "CLOSING", "REPAIR")
                 and fetch.origin in ("LIVE_LATEST", "HISTORICAL_LOOKUP"), "Invalid reference fetch provenance.")
        attempt = fetch.attempt
        routes = {"REFERENCE_LATEST": "/api/v1/reports/latest",
                  "REFERENCE_LOOKUP": "/api/v1/reports", "REFERENCE_PAGE": "/api/v1/reports/page"}
        _require(attempt.operation in routes and attempt.method == "GET"
                 and (attempt.path == routes[attempt.operation] or
                      (attempt.operation == 'REFERENCE_LATEST' and attempt.path == '/ws/v1'))
                 and attempt.selectors == ("feed_id=" + CHAINLINK_FEED_ID,),
                 "Reference request must describe an approved exact-feed read.")
        _require((fetch.origin == "LIVE_LATEST") == (attempt.operation == "REFERENCE_LATEST"),
                 "Latest and historical provenance disagree with the request.")
        for key in ("started_mono_ns", "ended_mono_ns", "started_wall_us", "ended_wall_us"):
            _integer(getattr(attempt, key), key, minimum=0 if key.endswith("ns") else -(2**63))
        _require(attempt.ended_mono_ns >= attempt.started_mono_ns, "Reference request times are reversed.")
        _integer(attempt.attempt_number, "attempt_number", minimum=1)
        _require(type(attempt.raw_complete) is bool, "Reference completeness must be boolean.")
        for name in ("target_us", "received_server_us"):
            if getattr(fetch, name) is not None:
                _integer(getattr(fetch, name), name)
        _require(fetch.raw_response is None or fetch.raw_response == attempt.raw_body,
                 "Reference response differs from actual request evidence.")
        if reports:
            _require(attempt.raw_complete and attempt.result == "success" and attempt.http_status == 200
                     and fetch.raw_response is not None, "Reports require complete successful retrieval evidence.")
        request_blob, request_size, version = encode_evidence(attempt.raw_body)
        request = dict(run_id=fetch.run_id, market_id=fetch.market_id, anchor_id=fetch.anchor_id,
            operation=attempt.operation, attempt_number=attempt.attempt_number,
            method=attempt.method, path=attempt.path, selector_json=_json(attempt.selectors),
            started_wall_us=attempt.started_wall_us, ended_wall_us=attempt.ended_wall_us,
            ended_server_us=fetch.received_server_us,
            start_mono_ns=attempt.started_mono_ns, end_mono_ns=attempt.ended_mono_ns,
            latency_ns=attempt.ended_mono_ns - attempt.started_mono_ns,
            http_status=attempt.http_status, result=attempt.result, error_code=attempt.error_code,
            response_bytes=request_size, response_raw_z=request_blob, raw_size_bytes=request_size,
            codec_version=version, raw_complete=int(attempt.raw_complete))
        fetch_values = dict(feed_id=CHAINLINK_FEED_ID, purpose=fetch.purpose, origin=fetch.origin,
            target_us=fetch.target_us, status=fetch.status, error_code=fetch.error_code,
            response_raw_z=None, raw_size_bytes=None, codec_version=None)
        if fetch.raw_response is not None:
            blob, size, codec = encode_evidence(fetch.raw_response)
            fetch_values.update(response_raw_z=blob, raw_size_bytes=size, codec_version=codec)
        report_values = []
        for report in reports:
            _require(isinstance(report, ReferenceReportDraft) and report.feed_id == CHAINLINK_FEED_ID
                     and type(report.report_schema_version) is int and report.report_schema_version == 2,
                     "Reference report identity/schema is not pinned.")
            blob, size, codec = encode_evidence(report.raw_signed_report)
            _require(report.report_hash == sha256(report.raw_signed_report).hexdigest(),
                     "Reference hash does not match raw signed bytes.")
            _require(report.first_received_wall_us == attempt.ended_wall_us
                     and report.first_received_server_us == fetch.received_server_us,
                     "Report receipt must be the actual retrieval end, independent of source fields.")
            _require(report.decode_status in ("DECODED", "INVALID", "UNVERIFIED")
                     and (report.decode_status == "DECODED" or report.decoded_price is None),
                     "Invalid/unverified reference cannot populate a decoded price.")
            for name in ("valid_from_seconds", "observations_seconds", "expires_at_seconds"):
                value = getattr(report, name)
                if value is not None:
                    _integer(value, name, minimum=0)
                    _require(value <= 253_402_300_799, "Unsupported reference source timestamp.")
            if report.decode_status == "DECODED":
                _require(report.decoded_price is not None and report.decoded_price > 0
                         and report.decoder_revision is not None
                         and all(getattr(report, name) is not None for name in
                                 ("valid_from_seconds", "observations_seconds", "expires_at_seconds"))
                         and report.valid_from_seconds <= report.observations_seconds <= report.expires_at_seconds,
                         "Decoded report requires a positive price and valid source interval.")
            report_values.append(dict(feed_id=CHAINLINK_FEED_ID, report_hash=report.report_hash,
                valid_from_seconds=report.valid_from_seconds, observations_seconds=report.observations_seconds,
                expires_at_seconds=report.expires_at_seconds, decoded_price_text=_decimal(report.decoded_price),
                decoder_revision=report.decoder_revision, decode_status=report.decode_status,
                verification_status=report.verification_status, first_received_wall_us=report.first_received_wall_us,
                first_received_server_us=report.first_received_server_us, raw_report_z=blob,
                raw_size_bytes=size, codec_version=codec))
        with self._transaction():
            self._run(fetch.run_id)
            if fetch.market_id is not None:
                _require(self._one("SELECT market_id FROM markets WHERE market_id=?", (fetch.market_id,)) is not None,
                         "Reference fetch market is unknown.")
            if fetch.anchor_id is not None:
                anchor = self._anchor(fetch.run_id, fetch.anchor_id)
                _require(fetch.received_server_us == self._server_us(anchor, attempt.ended_mono_ns),
                         "Reference receipt contradicts the supplied window-clock anchor.")
                request["started_server_us"] = self._server_us(anchor, attempt.started_mono_ns)
            existing = self._one("SELECT * FROM request_attempts WHERE run_id=? AND operation=? "
                "AND start_mono_ns=? AND end_mono_ns=? AND ended_wall_us=?",
                (fetch.run_id, attempt.operation, attempt.started_mono_ns, attempt.ended_mono_ns, attempt.ended_wall_us))
            if existing is not None:
                _require(self._equal(existing, request), "Repeated reference request changes evidence.")
                request_id = existing["request_id"]
            else:
                request_id = self._insert("request_attempts", request)
            fetch_values["request_id"] = request_id
            existing = self._one("SELECT * FROM reference_fetches WHERE request_id=?", (request_id,))
            if existing is not None:
                _require(self._equal(existing, fetch_values), "Repeated reference fetch changes evidence.")
                fetch_id = existing["fetch_id"]
            else:
                fetch_id = self._insert("reference_fetches", fetch_values)
            report_ids = {}
            for values in report_values:
                existing = self._one("SELECT * FROM reference_reports WHERE feed_id=? AND report_hash=?",
                                     (CHAINLINK_FEED_ID, values["report_hash"]))
                if existing is not None:
                    immutable = {key: value for key, value in values.items() if not key.startswith("first_received_")}
                    _require(self._equal(existing, immutable), "Duplicate signed bytes change decoded evidence.")
                    report_id = existing["report_id"]
                else:
                    report_id = self._insert("reference_reports", values | dict(first_fetch_id=fetch_id))
                report_ids[values["report_hash"]] = report_id
            for association in associations:
                self._save_reference_association(association, fetch, fetch_id, report_ids)

    def save_reference_plan(self, association) -> None:
        """Persist an inert, not-yet-fetched due plan without request evidence."""
        with self._transaction():
            self._validate_reference_target(association)
            _require(association.origin == "NOT_FETCHED" and association.status in ("PENDING", "DISABLED")
                     and association.available_live is False and association.fetch_id is None
                     and association.report_id is None and association.report_hash is None,
                     "Dormant reference plan cannot claim retrieval or availability.")
            for name in ("learned_wall_us", "learned_server_us", "next_due_us"):
                value = getattr(association, name)
                if value is not None:
                    _integer(value, name)
            _integer(association.attempt_count, "reference attempt count", minimum=0)
            values = asdict(association)
            values.pop("report_hash")
            values.update(feed_id=CHAINLINK_FEED_ID, available_live=0)
            self._append_reference_association(values)

    def _validate_reference_target(self, association) -> dict:
        market = self._one("SELECT * FROM markets WHERE market_id=?", (association.market_id,))
        _require(market is not None and association.feed_id == CHAINLINK_FEED_ID,
                 "Reference association market/feed is unknown.")
        _integer(association.target_us, "reference target_us")
        if association.role == "SLOT_CURRENT":
            slot = self._one("SELECT * FROM sample_slots WHERE slot_id=?", (association.slot_id,))
            _require(slot is not None and slot["market_id"] == association.market_id
                     and slot["scheduled_us"] == association.target_us, "Reference association slot/target mismatch.")
        else:
            _require(association.role in ("OPENING", "CLOSING") and association.slot_id is None,
                     "Reference boundary role/slot is invalid.")
            expected = market["window_start_us"] if association.role == "OPENING" else market["window_end_us"]
            _require(association.target_us == expected, "Reference boundary must target exact market start/end.")
        return market

    def _save_reference_association(self, association, fetch, fetch_id, report_ids) -> None:
        market = self._validate_reference_target(association)
        _require(fetch.market_id is None or fetch.market_id == association.market_id,
                 "Reference association belongs to another fetch market.")
        _require(association.origin == fetch.origin and fetch.purpose in (association.role, "REPAIR"),
                 "Reference association contradicts fetch purpose/origin.")
        if fetch.origin == "HISTORICAL_LOOKUP" and fetch.target_us is not None:
            _require(fetch.target_us == association.target_us, "Historical reference target differs from its association.")
        _integer(association.learned_wall_us, "reference learned wall")
        _require(association.learned_wall_us >= fetch.attempt.ended_wall_us,
                 "Association cannot be learned before retrieval.")
        if association.learned_server_us is not None:
            _integer(association.learned_server_us, "reference learned server")
            _require(fetch.received_server_us is not None and association.learned_server_us >= fetch.received_server_us,
                     "Association cannot be learned before server receipt.")
        _integer(association.attempt_count, "reference attempt count", minimum=0)
        if association.next_due_us is not None:
            _integer(association.next_due_us, "reference next due")
        _require(association.fetch_id is None or association.fetch_id == fetch_id,
                 "Association fetch ID disagrees with writer-assigned ID.")
        report_id = association.report_id
        if association.report_hash is not None:
            linked = report_ids.get(association.report_hash)
            _require(linked is not None and (report_id is None or report_id == linked),
                     "Reference report hash/ID is unknown or inconsistent.")
            report_id = linked
        if report_id is not None:
            _require(report_id in report_ids.values(), "Association report was not returned by this fetch.")
        report = self._one("SELECT * FROM reference_reports WHERE report_id=?", (report_id,)) if report_id is not None else None
        _require(association.status != "AVAILABLE" or (report is not None and report["decode_status"] == "DECODED"),
                 "Available reference requires an unambiguous decoded returned report.")
        _require(type(association.available_live) is bool, "Reference live availability must be boolean.")
        if association.available_live:
            _require(fetch.origin == "LIVE_LATEST" and association.status == "AVAILABLE"
                      and report is not None and report["decode_status"] == "DECODED",
                     "Live availability requires structurally decoded latest evidence.")
            receipt = fetch.received_server_us
            _require(receipt is not None and report['observations_seconds'] is not None
                     and report['valid_from_seconds'] is not None and report['expires_at_seconds'] is not None
                     and report['valid_from_seconds']*1_000_000 <= receipt <= report['expires_at_seconds']*1_000_000
                     and 0 <= receipt-report['observations_seconds']*1_000_000 <= 5_000_000,
                     'Live availability requires fresh signed interval and calibrated receipt.')
            if association.role == "SLOT_CURRENT":
                _require(fetch.received_server_us is not None
and (fetch.attempt.path == '/ws/v1' or association.target_us <= fetch.received_server_us)
                          and fetch.received_server_us < min(association.target_us + 2_000_000, market["window_end_us"]),
                         "A latest report outside its slot deadline cannot claim original live coverage.")
        values = asdict(association)
        values.pop("report_hash")
        values.update(feed_id=CHAINLINK_FEED_ID, fetch_id=fetch_id, report_id=report_id,
                      available_live=int(association.available_live))
        self._append_reference_association(values)

    def _append_reference_association(self, values: dict) -> None:
        self._check_writer()
        existing = self._one("SELECT * FROM reference_associations WHERE market_id=? AND role=? "
                             "AND slot_id IS ? AND learned_wall_us=?",
                             (values["market_id"], values["role"], values["slot_id"], values["learned_wall_us"]))
        if existing is not None:
            _require(self._equal(existing, values), "Repeated association changes learned evidence.")
        else:
            self._insert("reference_associations", values)

    def record_event(self, run_id: str, kind: str, *, market_id: str | None = None,
                     started_us: int, ended_us: int | None = None,
                     detail: dict | None = None) -> int:
        self._check_writer()
        detail = {} if detail is None else detail
        _integer(started_us, "started_us")
        if ended_us is not None:
            _integer(ended_us, "ended_us")
            _require(ended_us >= started_us, "Recorder event end precedes its start.")
        _require(kind in ("STARTUP", "SHUTDOWN", "RESTART", "DISCOVERY_GAP",
                          "CLOCK_PAUSE", "IDENTITY_CONFLICT", "STORAGE_FAILURE")
                 and type(detail) is dict, "Invalid recorder event.")
        with self._transaction():
            self._run(run_id)
            if market_id is not None:
                _require(self._one("SELECT market_id FROM markets WHERE market_id=?", (market_id,)) is not None,
                         "Recorder event market is unknown.")
            return self._insert("recorder_events", dict(
                run_id=run_id, market_id=market_id, kind=kind,
                started_us=started_us, ended_us=ended_us, detail_json=_json(detail)))

    def finish_discovery_gap(self, event_id: int, *, run_id: str, ended_us: int) -> None:
        """Fill an open acquisition interval once; diagnostic events cannot close."""
        _integer(ended_us, "gap end")
        with self._transaction():
            self._run(run_id)
            event = self._one("SELECT * FROM recorder_events WHERE event_id=?", (event_id,))
            _require(event is not None and event["run_id"] == run_id
                     and event["kind"] == "DISCOVERY_GAP" and event["ended_us"] is None
                     and json.loads(event["detail_json"]).get("acquisition_gap") is True
                     and ended_us >= event["started_us"], "Discovery gap closure contradicts its start.")
            self._connection.execute("UPDATE recorder_events SET ended_us=? WHERE event_id=?", (ended_us,event_id))

    def reserve_slot(self, market_id: str, target: SlotTarget, *, run_id: str,
                     anchor_id: int, metadata_revision_id: int, started_mono_ns: int,
                     started_server_us: int) -> int:
        with self._transaction():
            self._market_target(market_id, target)
            self._run(run_id)
            self._revision(market_id, metadata_revision_id)
            self._anchor(run_id, anchor_id)
            _integer(started_mono_ns, "start_mono_ns", minimum=0)
            _integer(started_server_us, "started_server_us")
            values = dict(market_id=market_id, slot_index=target.index,
                scheduled_us=target.scheduled_us, run_id=run_id, anchor_id=anchor_id,
                metadata_revision_id=metadata_revision_id, status="IN_PROGRESS",
                started_server_us=started_server_us, start_mono_ns=started_mono_ns,
                delay_us=max(0, started_server_us - target.scheduled_us), reason="",
                quality_json="{}", reference_status="REFERENCE_INCOMPLETE")
            existing = self._one("SELECT * FROM sample_slots WHERE market_id=? AND slot_index=?",
                                 (market_id, target.index))
            if existing is not None:
                _require(self._equal(existing, values), "Reserved/terminal slot cannot be reacquired or changed.")
                return existing["slot_id"]
            return self._insert("sample_slots", values)

    @staticmethod
    def _server_us(anchor: dict, mono_ns: int) -> int:
        return anchor["anchor_server_us"] + (mono_ns - anchor["anchor_mono_ns"]) // 1000

    def _attempt_values(self, attempt, commit: SlotCommit, anchor: dict, slot_id: int) -> dict:
        for key in ("started_mono_ns", "ended_mono_ns", "started_wall_us", "ended_wall_us"):
            _integer(getattr(attempt, key), key, minimum=0 if key.endswith("ns") else -(2**63))
        _require(commit.started_mono_ns <= attempt.started_mono_ns <= attempt.ended_mono_ns <= commit.ended_mono_ns,
                 "Request monotonic times fall outside the slot capture.")
        _integer(attempt.attempt_number, "attempt_number", minimum=1)
        _require(attempt.attempt_number <= 2 and type(attempt.raw_complete) is bool,
                 "Book attempt exceeds retry policy or has invalid evidence completeness.")
        if attempt.operation == "BOOK_BATCH":
            _require(attempt.method == "POST" and attempt.path == "/books" and len(attempt.selectors) == 2
                     and set(attempt.selectors) == {commit.books.up.token_id, commit.books.down.token_id},
                     "Batch attempt must select exactly the two pinned tokens.")
        else:
            _require(attempt.operation == "BOOK_SINGLE" and attempt.method == "GET" and attempt.path == "/book"
                     and len(attempt.selectors) == 1
                     and attempt.selectors[0] in ("token_id=" + commit.books.up.token_id,
                                                  "token_id=" + commit.books.down.token_id),
                     "Book attempt has an unapproved operation or selector.")
        blob, size, version = encode_evidence(attempt.raw_body)
        return dict(run_id=commit.run_id, market_id=commit.market_id, slot_id=slot_id,
            anchor_id=commit.anchor_id, operation=attempt.operation, attempt_number=attempt.attempt_number,
            method=attempt.method, path=attempt.path, selector_json=_json(attempt.selectors),
            started_wall_us=attempt.started_wall_us, ended_wall_us=attempt.ended_wall_us,
            started_server_us=self._server_us(anchor, attempt.started_mono_ns),
            ended_server_us=self._server_us(anchor, attempt.ended_mono_ns),
            start_mono_ns=attempt.started_mono_ns, end_mono_ns=attempt.ended_mono_ns,
            latency_ns=attempt.ended_mono_ns - attempt.started_mono_ns,
            http_status=attempt.http_status, result=attempt.result, error_code=attempt.error_code,
            response_bytes=size, response_raw_z=blob, raw_size_bytes=size,
            codec_version=version, raw_complete=int(attempt.raw_complete))

    @staticmethod
    def _validate_result(result, label: str, token: str, market: dict, attempts, *, target, anchor) -> None:
        _require((result.outcome, result.token_id, result.condition_id) == (label, token, market["condition_id"]),
                 "Outcome result does not match the immutable token/condition mapping.")
        _require(result.status in ("VALID", "UNAVAILABLE", "INVALID", "LATE", "INTERRUPTED"),
                 "Unknown book outcome status.")
        if result.attempt_index is not None:
            _integer(result.attempt_index, "attempt_index", minimum=0)
            _require(result.attempt_index < len(attempts), "Outcome attempt link is out of range.")
            attempt = attempts[result.attempt_index]
            _require((token in attempt.selectors if attempt.operation == "BOOK_BATCH"
                      else attempt.selectors == ("token_id=" + token,)), "Outcome links to another token's attempt.")
        book = result.book
        if book is not None:
            _require((book.token_id, book.condition_id) == (token, market["condition_id"]),
                     "Parsed book does not match its outcome's identity.")
            _integer(_epoch_us(book.source_at), "source_us")
            _require(type(book.source_timestamp_raw) is str
                     and re.fullmatch(r"[0-9]{13}", book.source_timestamp_raw) is not None
                     and int(book.source_timestamp_raw) * 1000 == _epoch_us(book.source_at),
                     "Book source timestamp contradicts its preserved raw timestamp.")
        if result.status == "VALID":
            _require(book is not None and result.attempt_index is not None and result.error_code is None,
                     "Valid book requires parsed source evidence without an error.")
            attempt = attempts[result.attempt_index]
            _require(attempt.raw_complete and attempt.result == "success" and attempt.http_status == 200,
                     "Valid book requires complete successful response evidence.")
            _integer(attempt.ended_mono_ns, "request_end_mono_ns", minimum=0)
            receipt_us = RecorderStore._server_us(anchor, attempt.ended_mono_ns)
            _require(receipt_us < min(target.scheduled_us + 2_000_000, market["window_end_us"]),
                     "Late book evidence cannot populate a usable quote.")
            for field in ("best_bid", "best_ask", "midpoint", "spread", "bid_size", "ask_size"):
                value = getattr(book, field)
                _decimal(value)
                _require(value is None or value >= 0, "Book summary cannot contain negative values.")
                if field in ("best_bid", "best_ask", "midpoint", "spread"):
                    _require(value is None or value <= 1, "Book probability summary is out of range.")
            _require((book.best_bid is None) == (book.bid_size is None)
                     and (book.best_ask is None) == (book.ask_size is None),
                     "Missing book sides cannot have fabricated sizes.")
            try:
                with exact_decimal_context():
                    both = book.best_bid is not None and book.best_ask is not None
                    midpoint = (book.best_bid + book.best_ask) / Decimal(2) if both else None
                    spread = book.best_ask - book.best_bid if both else None
                _require(book.midpoint == midpoint and book.spread == spread,
                         "Book midpoint/spread contradict the preserved quote sides.")
            except DecimalException as exc:
                raise RecorderStoreError("fatal_invariant", "Book summary arithmetic cannot be exact.") from exc

    @staticmethod
    def _book_values(result, slot_id: int, request_ids: list[int]) -> dict:
        book = result.book
        valid = result.status == "VALID" and book is not None
        for field in ("tick_size", "min_order_size"):
            value = result.book_metadata.get(field)
            _decimal(value)
            _require(value is None or value >= 0, "Book metadata cannot be negative.")
        values = dict(slot_id=slot_id, outcome=result.outcome, expected_token_id=result.token_id,
            expected_condition_id=result.condition_id,
            request_id=None if result.attempt_index is None else request_ids[result.attempt_index],
            status=result.status, error_code=result.error_code, quality_json=_json(result.quality),
            source_timestamp_raw=None if book is None else book.source_timestamp_raw,
            source_us=None if book is None else _epoch_us(book.source_at),
            book_hash=result.book_metadata.get("hash"),
            tick_size_text=_decimal(result.book_metadata.get("tick_size")),
            min_order_size_text=_decimal(result.book_metadata.get("min_order_size")),
            bid_level_count=None if book is None else len(book.bids),
            ask_level_count=None if book is None else len(book.asks))
        for column, field in (("best_bid_text", "best_bid"), ("best_ask_text", "best_ask"),
                              ("bid_size_text", "bid_size"), ("ask_size_text", "ask_size"),
                              ("midpoint_text", "midpoint"), ("spread_text", "spread")):
            values[column] = _decimal(getattr(book, field)) if valid else None
        return values

    def finalize_slot(self, slot_id: int, commit: SlotCommit, *, committed_wall_us: int) -> None:
        self._check_writer()
        _integer(slot_id, "slot_id", minimum=1)
        _integer(committed_wall_us, "committed_wall_us")
        market = self._market_target(commit.market_id, commit.target)
        slot = self._one("SELECT * FROM sample_slots WHERE slot_id=?", (slot_id,))
        identity = dict(market_id=commit.market_id, slot_index=commit.target.index,
            scheduled_us=commit.target.scheduled_us, run_id=commit.run_id, anchor_id=commit.anchor_id,
            metadata_revision_id=commit.metadata_revision_id, start_mono_ns=commit.started_mono_ns)
        _require(slot is not None and self._equal(slot, identity)
                 and slot["status"] in ("IN_PROGRESS", "COMPLETE", "PARTIAL", "FAILED"),
                 "Slot finalization conflicts with its reservation or terminal identity.")
        self._run(commit.run_id, running=slot["status"] == "IN_PROGRESS")
        self._revision(commit.market_id, commit.metadata_revision_id)
        anchor = self._anchor(commit.run_id, commit.anchor_id)
        _integer(commit.started_mono_ns, "start_mono_ns", minimum=0)
        _integer(commit.ended_mono_ns, "end_mono_ns", minimum=0)
        _require(commit.ended_mono_ns >= commit.started_mono_ns, "Slot capture times are reversed.")
        for result, label, token in ((commit.books.up, "UP", market["up_token_id"]),
                                      (commit.books.down, "DOWN", market["down_token_id"])):
            self._validate_result(result, label, token, market, commit.books.attempts,
                                  target=commit.target, anchor=anchor)
        reference = commit.reference
        _require(reference.market_id == commit.market_id and reference.role == "SLOT_CURRENT"
                 and reference.target_us == commit.target.scheduled_us
                 and reference.slot_id in (None, slot_id), "Reference association does not target this slot.")
        run_mode = self._run(commit.run_id, running=False)['mode']
        _require(reference.status == ('DISABLED' if run_mode == 'BOOK_ONLY' else 'UNAVAILABLE')
                 and reference.origin == "NOT_FETCHED"
                 and reference.available_live is False and reference.fetch_id is None and reference.report_id is None
                 and reference.attempt_count == 0 and reference.next_due_us is None,
                 "Book-only original reference association must preserve disabled provenance.")
        _integer(reference.learned_wall_us, "learned_wall_us")
        if reference.learned_server_us is not None:
            _integer(reference.learned_server_us, "learned_server_us")
        attempts = [self._attempt_values(attempt, commit, anchor, slot_id) for attempt in commit.books.attempts]
        # Encode and format bounded evidence before acquiring the short write transaction.
        books = [self._book_values(result, slot_id, list(range(len(attempts))))
                 for result in (commit.books.up, commit.books.down)]
        association = asdict(reference) | dict(slot_id=slot_id, feed_id=CHAINLINK_FEED_ID, available_live=0)
        association.pop("report_hash")
        valid_count = sum(result.status == "VALID" for result in (commit.books.up, commit.books.down))
        terminal = dict(status=("FAILED", "PARTIAL", "COMPLETE")[valid_count],
            finished_server_us=self._server_us(anchor, commit.ended_mono_ns), committed_wall_us=committed_wall_us,
            end_mono_ns=commit.ended_mono_ns, reason="BOOKS_CAPTURED" if valid_count else "NO_VALID_BOOKS",
            quality_json="{}", reference_status="REFERENCE_INCOMPLETE")
        with self._transaction():
            if slot["status"] != "IN_PROGRESS":
                self._verify_duplicate(slot, terminal, attempts, books, association)
                return
            request_ids = [self._insert("request_attempts", attempt) for attempt in attempts]
            for book in books:
                if book["request_id"] is not None:
                    book["request_id"] = request_ids[book["request_id"]]
                self._insert("book_observations", book)
            self._insert("reference_associations", association)
            self._connection.execute("UPDATE sample_slots SET " + ",".join(key + "=?" for key in terminal)
                                     + " WHERE slot_id=?", (*terminal.values(), slot_id))
            self._connection.execute("UPDATE recording_runs SET last_committed_wall_us=? WHERE run_id=?",
                                     (committed_wall_us, commit.run_id))

    def _verify_duplicate(self, slot, terminal, attempts, books, association) -> None:
        _require(self._equal(slot, terminal), "Duplicate finalization changes terminal slot content.")
        saved_attempts = self._rows("SELECT * FROM request_attempts WHERE slot_id=? ORDER BY request_id", (slot["slot_id"],))
        _require(len(saved_attempts) == len(attempts) and all(self._equal(old, new)
                 for old, new in zip(saved_attempts, attempts)), "Duplicate finalization changes request evidence.")
        for book in books:
            if book["request_id"] is not None:
                book["request_id"] = saved_attempts[book["request_id"]]["request_id"]
            saved = self._one("SELECT * FROM book_observations WHERE slot_id=? AND outcome=?",
                              (slot["slot_id"], book["outcome"]))
            _require(saved is not None and self._equal(saved, book), "Duplicate finalization changes an outcome.")
        saved_reference = self._one("SELECT * FROM reference_associations WHERE slot_id=? "
                                    "ORDER BY association_id LIMIT 1", (slot["slot_id"],))
        _require(saved_reference is not None and self._equal(saved_reference, association),
                 "Duplicate finalization changes original reference availability.")

    def mark_missed(self, market_id: str, target: SlotTarget, *, run_id: str | None,
                    reason: str, wall_us: int) -> None:
        with self._transaction():
            self._market_target(market_id, target)
            if run_id is not None:
                self._run(run_id)
            _integer(wall_us, "committed_wall_us")
            _require(isinstance(reason, str) and bool(reason), "Missed slot needs an explicit reason.")
            revision = self._one("SELECT revision_id FROM market_revisions WHERE market_id=? "
                                 "ORDER BY observed_us DESC,revision_id DESC LIMIT 1", (market_id,))
            _require(revision is not None, "Missed slot needs a known metadata revision.")
            values = dict(market_id=market_id, slot_index=target.index,
                scheduled_us=target.scheduled_us, run_id=run_id,
                metadata_revision_id=revision["revision_id"], status="MISSED",
                committed_wall_us=wall_us, reason=reason, quality_json="{}",
                reference_status="REFERENCE_INCOMPLETE")
            existing = self._one("SELECT * FROM sample_slots WHERE market_id=? AND slot_index=?", (market_id, target.index))
            if existing is not None:
                _require(self._equal(existing, values), "Missed slot conflicts with existing coverage.")
                return
            self._insert("sample_slots", values)

    def finish_run(self, run_id: str, *, state: str, wall_us: int, reason: str) -> None:
        with self._transaction():
            _require(state in ("STOPPED", "FAILED", "INTERRUPTED"), "Run finish requires a terminal state.")
            _integer(wall_us, "ended_wall_us")
            _require(isinstance(reason, str) and bool(reason), "Run finish requires an explicit reason.")
            run = self._run(run_id, running=False)
            if run["state"] != "RUNNING":
                _require((run["state"], run["ended_wall_us"], run["exit_reason"]) == (state, wall_us, reason),
                         "Terminal run cannot be overwritten.")
                return
            self._connection.execute("UPDATE recording_runs SET state=?,ended_wall_us=?,exit_reason=? WHERE run_id=?",
                                     (state, wall_us, reason, run_id))

    def covered_indices(self, market_id: str) -> set[int]:
        return {row["slot_index"] for row in self._rows(
            "SELECT slot_index FROM sample_slots WHERE market_id=?", (market_id,))}

    def restart_context(self) -> tuple[int | None, tuple[dict, ...]]:
        """Durable wall/window context only; never export old monotonic epochs."""
        last = self._one("SELECT MAX(COALESCE(ended_wall_us,last_committed_wall_us,started_wall_us)) AS wall_us FROM recording_runs")
        markets = self._rows("SELECT market_id,window_start_us,window_end_us,lifecycle FROM markets ORDER BY window_start_us")
        return last["wall_us"], tuple(markets)

    def paused_revision_id(self, revision: MarketRevision) -> int:
        """Validate rediscovery without reviving an immutable invalidated identity."""
        market = revision.market
        known = self._one("SELECT * FROM markets WHERE market_id=?", (market.market_id,))
        identity = dict(condition_id=market.condition_id, event_id=market.event_id,
            window_start_us=_epoch_us(market.window_start), window_end_us=_epoch_us(market.window_end),
            up_token_id=market.up.token_id, down_token_id=market.down.token_id,
            up_outcome_index=revision.up_index, down_outcome_index=revision.down_index)
        _require(known is not None and known["lifecycle"] == "INVALIDATED"
                 and self._equal(known, identity), "Invalidated identity cannot be changed on restart.")
        latest = self._one("SELECT revision_id FROM market_revisions WHERE market_id=? ORDER BY revision_id DESC LIMIT 1",
                           (market.market_id,))
        _require(latest is not None, "Invalidated identity lacks metadata evidence.")
        return latest["revision_id"]

    def paused_revision(self, market_id: str) -> tuple[MarketRevision, int]:
        """Return persisted immutable identity for paused coverage, never a candidate mapping."""
        from .resolution import _revision_from_row
        known = self._one("SELECT * FROM markets WHERE market_id=?", (market_id,))
        _require(known is not None and known["lifecycle"] == "INVALIDATED",
                 "Paused coverage requires a known invalidated market.")
        revision = replace(_revision_from_row(known), capture_ready=False)
        return revision, self.paused_revision_id(revision)

    def save_control_attempts(self, run_id: str, attempts: tuple) -> None:
        """Persist buffered calibration/discovery/metadata attempts after I/O."""
        self._check_writer()
        prepared = []
        for attempt in attempts:
            _require(attempt.method == "GET" and attempt.operation in (
                "CLOB_TIME", "CLOB_CONFIG", "GAMMA_EVENTS", "GAMMA_EVENT", "GAMMA_MARKET"),
                "Control attempt must be a public metadata/time GET.")
            for name in ("started_wall_us", "ended_wall_us", "started_mono_ns", "ended_mono_ns"):
                _integer(getattr(attempt, name), name)
            _require(attempt.ended_mono_ns >= attempt.started_mono_ns,
                     "Control attempt timing is reversed.")
            blob, size, codec = encode_evidence(attempt.raw_body)
            prepared.append(dict(run_id=run_id, operation=attempt.operation,
                attempt_number=attempt.attempt_number, method=attempt.method, path=attempt.path,
                selector_json=_json(attempt.selectors), started_wall_us=attempt.started_wall_us,
                ended_wall_us=attempt.ended_wall_us, start_mono_ns=attempt.started_mono_ns,
                end_mono_ns=attempt.ended_mono_ns,
                latency_ns=attempt.ended_mono_ns-attempt.started_mono_ns,
                http_status=attempt.http_status, result=attempt.result, error_code=attempt.error_code,
                response_bytes=size, response_raw_z=blob, raw_size_bytes=size,
                codec_version=codec, raw_complete=int(attempt.raw_complete)))
        if prepared:
            with self._transaction():
                self._run(run_id)
                for values in prepared:
                    self._insert("request_attempts", values)

    def recover_interrupted(self, *, wall_us: int, run_id: str | None = None,
                            finish_runs: bool = True, reason: str = "RESTART_RECOVERY") -> None:
        """Finalize old reservations without projecting across monotonic epochs."""
        self._check_writer()
        _integer(wall_us, "recovery_wall_us")
        with self._transaction():
            slots = self._rows("SELECT * FROM sample_slots WHERE status='IN_PROGRESS'"
                + (" AND run_id=?" if run_id is not None else "") + " ORDER BY slot_id",
                (run_id,) if run_id is not None else ())
            for slot in slots:
                market = self._market_target(slot["market_id"], SlotTarget(slot["slot_index"], slot["scheduled_us"]))
                self._revision(slot["market_id"], slot["metadata_revision_id"])
                _require(not self._rows("SELECT slot_id FROM book_observations WHERE slot_id=?", (slot["slot_id"],))
                         and not self._rows("SELECT association_id FROM reference_associations WHERE slot_id=?", (slot["slot_id"],)),
                         "Unfinished reservation already contains terminal evidence.")
                for label, token in (("UP", market["up_token_id"]), ("DOWN", market["down_token_id"])):
                    self._insert("book_observations", dict(slot_id=slot["slot_id"], outcome=label,
                        expected_token_id=token, expected_condition_id=market["condition_id"],
                        status="INTERRUPTED", error_code="interrupted_before_commit",
                        quality_json='{"interrupted_before_commit":true}'))
                self._insert("reference_associations", dict(market_id=slot["market_id"], feed_id=CHAINLINK_FEED_ID,
                    role="SLOT_CURRENT", slot_id=slot["slot_id"], target_us=slot["scheduled_us"],
                    status="DISABLED", origin="NOT_FETCHED", available_live=0,
                    learned_wall_us=wall_us, learned_server_us=None, attempt_count=0))
                self._connection.execute("""UPDATE sample_slots SET status='INTERRUPTED',
                    committed_wall_us=?,reason=?,reference_status='REFERENCE_INCOMPLETE'
                    WHERE slot_id=?""", (wall_us, reason, slot["slot_id"]))
                self._connection.execute("UPDATE recording_runs SET last_committed_wall_us=? WHERE run_id=?",
                                         (wall_us, slot["run_id"]))
            if finish_runs:
                self._connection.execute("""UPDATE recording_runs SET state='INTERRUPTED',ended_wall_us=?,
                    exit_reason=? WHERE state='RUNNING'""" + (" AND run_id=?" if run_id is not None else ""),
                    (wall_us, reason, run_id) if run_id is not None else (wall_us, reason))

    def save_approximate_observation(
        self, run_id: str, observation: ApproximateObservation
    ) -> int:
        """Insert into approximate_observations. Returns observation_id."""
        self._check_writer()
        _require(isinstance(observation, ApproximateObservation), "Approximate observation record is required.")
        _require(observation.provider and observation.pair and observation.quote_currency,
                 "Approximate observation requires provider, pair, and quote_currency.")
        _require(observation.result in ("success", "error"), "Observation result must be success or error.")
        _require(type(observation.receipt_wall_us) is int and observation.receipt_wall_us >= 0,
                 "Observation receipt_wall_us must be a non-negative integer.")
        if observation.receipt_server_us is not None:
            _require(type(observation.receipt_server_us) is int and observation.receipt_server_us >= 0,
                     "Observation receipt_server_us must be a non-negative integer.")
        if observation.source_timestamp_us is not None:
            _require(type(observation.source_timestamp_us) is int and observation.source_timestamp_us >= 0,
                     "Observation source_timestamp_us must be a non-negative integer.")
        _require(type(observation.raw_payload_size) is int and 0 <= observation.raw_payload_size <= 10_485_760,
                 "Observation raw_payload_size out of bounds.")
        _require(type(observation.raw_payload_hash) is str and 1 <= len(observation.raw_payload_hash) <= 64,
                 "Observation raw_payload_hash must be a non-empty string.")
        _require(type(observation.transport_type) is str and observation.transport_type in ("rest", "ws"),
                 "Observation transport_type must be rest or ws.")
        _require(type(observation.parser_revision) is str and 1 <= len(observation.parser_revision) <= 64,
                 "Observation parser_revision must be a non-empty string.")
        if observation.http_status is not None:
            _require(type(observation.http_status) is int and 100 <= observation.http_status <= 599,
                     "Observation http_status out of range.")
        if observation.error_code is not None:
            _require(type(observation.error_code) is str and 1 <= len(observation.error_code) <= 64,
                     "Observation error_code must be a non-empty string.")

        price_text = decimal_text(observation.price)
        bid_price_text = decimal_text(observation.bid_price) if observation.bid_price is not None else None
        ask_price_text = decimal_text(observation.ask_price) if observation.ask_price is not None else None

        with self._transaction():
            self._run(run_id)
            return self._insert("approximate_observations", dict(
                run_id=run_id,
                provider=observation.provider,
                pair=observation.pair,
                quote_currency=observation.quote_currency,
                price_text=price_text,
                bid_price_text=bid_price_text,
                ask_price_text=ask_price_text,
                source_timestamp_us=observation.source_timestamp_us,
                receipt_wall_us=observation.receipt_wall_us,
                receipt_server_us=observation.receipt_server_us,
                raw_payload_hash=observation.raw_payload_hash,
                raw_payload_size=observation.raw_payload_size,
                transport_type=observation.transport_type,
                parser_revision=observation.parser_revision,
                http_status=observation.http_status,
                result=observation.result,
                error_code=observation.error_code
            ))

    def save_approximate_reference(
        self, run_id: str, report: ApproximateReferenceReport
    ) -> int:
        """Insert into approximate_references. Returns reference_id."""
        self._check_writer()
        _require(isinstance(report, ApproximateReferenceReport), "Approximate reference report is required.")
        _require(report.reference_kind == "APPROXIMATE_SAMPLE_MEAN_60S",
                 "Reference kind must be APPROXIMATE_SAMPLE_MEAN_60S.")
        _require(report.mode == "APPROXIMATE", "Reference mode must be APPROXIMATE.")
        _require(report.verification_status == "UNVERIFIED",
                 "Approximate reference verification_status must be UNVERIFIED.")
        _require(type(report.window_start_us) is int and report.window_start_us >= 0,
                 "Reference window_start_us must be a non-negative integer.")
        _require(type(report.window_end_us) is int and report.window_end_us >= report.window_start_us,
                 "Reference window_end_us must be >= window_start_us.")
        _require(type(report.observation_count) is int and report.observation_count >= 0,
                 "Reference observation_count must be a non-negative integer.")
        _require(type(report.source_provider) is str, "Reference source_provider must be a string.")
        _require(type(report.was_fallback) is bool, "Reference was_fallback must be a boolean.")
        _require(type(report.receipt_wall_us) is int and report.receipt_wall_us >= 0,
                 "Reference receipt_wall_us must be a non-negative integer.")
        if report.receipt_server_us is not None:
            _require(type(report.receipt_server_us) is int and report.receipt_server_us >= 0,
                     "Reference receipt_server_us must be a non-negative integer.")

        value_text = decimal_text(report.value) if report.value is not None else None
        diagnostics_json = _json(report.diagnostics) if report.diagnostics else "[]"

        with self._transaction():
            self._run(run_id)
            return self._insert("approximate_references", dict(
                run_id=run_id,
                mode=report.mode,
                reference_kind=report.reference_kind,
                value_text=value_text,
                window_start_us=report.window_start_us,
                window_end_us=report.window_end_us,
                observation_count=report.observation_count,
                source_provider=report.source_provider,
                was_fallback=int(report.was_fallback),
                verification_status=report.verification_status,
                receipt_wall_us=report.receipt_wall_us,
                receipt_server_us=report.receipt_server_us,
                diagnostics_json=diagnostics_json
            ))
