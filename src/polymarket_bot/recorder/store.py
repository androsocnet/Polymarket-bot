"""One recorder connection and OS writer ownership; no generic SQL interface."""
import os
from pathlib import Path
import re
import sqlite3
import zlib


MAX_EVIDENCE_BYTES = 10 * 1024 * 1024


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
        match = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX) ([a-z_]+) ", normalized)
        if match is None:
            raise RecorderStoreError("invalid_schema", "Invalid bundled schema definition.")
        definitions[(match[1].lower(), match[2])] = normalized
    return definitions


class RecorderStore:
    """Own the canonical database lock until close, including commit gaps."""

    def __init__(self, path: Path, lock_file, connection: sqlite3.Connection):
        self.path = path
        self._lock_file = lock_file
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "RecorderStore":
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

            script = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
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
                connection.executescript("BEGIN IMMEDIATE;\n" + script + "\nPRAGMA user_version=1;\nCOMMIT;")
            else:
                cls._verify_existing(connection, script)
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
    def _verify_existing(connection: sqlite3.Connection, script: str) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            raise RecorderStoreError("unknown_schema", "Existing recorder database schema version is unsupported.")
        actual = {(kind, name): " ".join(sql.split()) for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if actual != _schema_contract(script):
            raise RecorderStoreError("invalid_schema", "Existing recorder database does not match schema version 1.")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RecorderStoreError("database_integrity", "Recorder database integrity check failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RecorderStoreError("database_integrity", "Recorder database foreign-key integrity check failed.")

    def close(self) -> None:
        if self._lock_file is None:
            return
        # Failed connection closure (for example a wrong thread) retains ownership.
        self._connection.close()
        self._lock_file.close()
        self._lock_file = None
