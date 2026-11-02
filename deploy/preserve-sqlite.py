#!/usr/bin/env python3
"""Per-DB SQLite snapshots, NOT a cross-DB atomic boundary or raw-family archive.
Known writers must be quiet. Fingerprints detect observed changes, not every writer.
Read-only WAL opening can create/change shm; inventory it but exclude reader-lock
metadata from stability checks. Never use immutable=1 on live WAL sources.
Verify only copies into new scratch; no production restore operation.
"""
import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

NAMES = ("db1.sqlite", "db2.sqlite", "db3.sqlite")


def safe_path(path, regular=False):
    if ".." in Path(path).parts:
        raise ValueError("parent path components refused")
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink refused")
    if regular and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("regular file required")
    return path.resolve()


def fingerprint(path, reader_metadata=False, wal=False):
    if not path.exists():
        if path.is_symlink():
            raise ValueError("symlink refused")
        return None
    path = safe_path(path, regular=True)
    def xattrs():
        # Linux exposes POSIX ACLs through system.posix_acl_* xattrs too.
        return {name: os.getxattr(path, name).hex() for name in sorted(os.listxattr(path))}

    attributes = xattrs() if wal else None
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()

    def fields(s):
        return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns,
                s.st_mode, s.st_uid, s.st_gid, s.st_nlink]

    def comparable(s):
        values = fields(s)
        # SQLite WAL opens can change ctime alone; keep explicit metadata checks.
        return values[:4] + values[5:] if wal else values

    if (not reader_metadata and comparable(before) != comparable(after)) or (wal and attributes != xattrs()):
        raise ValueError("file changed while hashing")
    result = {"stat": fields(after), "sha256": digest}
    if wal:
        result["xattrs"] = attributes
    return result


def families(paths):
    return [{suffix: fingerprint(Path(str(path) + suffix), reader_metadata=suffix == "-shm",
                                 wal=suffix == "-wal")
             for suffix in ("", "-wal", "-journal", "-shm")} for path in paths]


def stable(before, after):
    def comparable(family):
        result = {k: v for k, v in family.items() if k != "-shm"}
        wal = result.get("-wal")
        # Legacy six-field manifests lack explicit ownership/link checks: strict.
        if wal is not None and len(wal["stat"]) == 9 and "xattrs" in wal:
            result["-wal"] = {**wal, "stat": wal["stat"][:4] + wal["stat"][5:]}
        return result

    return all(comparable(a) == comparable(b) for a, b in zip(before, after))


def inventory(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity failed")
        schema = [list(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")]
        counts = {row[1]: conn.execute('SELECT count(*) FROM "' +
                  row[1].replace('"', '""') + '"').fetchone()[0]
                  for row in schema if row[0] == "table"}
        return {"schema": schema, "counts": counts}


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def publish(directory, name, value, check=lambda: None):
    temporary, final = directory / ("." + name + ".tmp"), directory / name
    owned = None
    try:
        with create_file(temporary) as stream:
            info = os.fstat(stream.fileno())
            owned = (info.st_dev, info.st_ino)
            stream.write(json.dumps(value, sort_keys=True, allow_nan=False).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        check()
        os.link(temporary, final)  # Atomic, exclusive publication; never replace evidence.
        temporary.unlink()
        sync_dir(directory)
        check()
    except BaseException:
        try:
            info = final.lstat()
            if (info.st_dev, info.st_ino) == owned:
                final.unlink()
                sync_dir(directory)
        except FileNotFoundError:
            pass
        raise


def backup(paths: list[Path], dest: Path, timeout=30):
    if len(paths) != 3:
        raise ValueError("exactly three sources required")
    paths = [safe_path(p, regular=True) for p in paths]
    if len({(p.stat().st_dev, p.stat().st_ino) for p in paths}) != 3:
        raise ValueError("distinct sources required")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("invalid timeout")
    dest = safe_path(dest)
    if any(dest == p or p in dest.parents for p in paths):
        raise ValueError("destination overlaps source")
    dest.mkdir(mode=0o700)
    deadline = time.monotonic() + timeout
    sync_dir(dest.parent)

    def check(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError("snapshot deadline exceeded")

    before = families(paths)
    entries = []
    for path, name in zip(paths, NAMES):
        check()
        target = dest / name
        with create_file(target):
            pass
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as source:
            with closing(sqlite3.connect(target, timeout=0)) as copy:
                source.backup(copy, pages=256, progress=check, sleep=0.05)
                copy.execute("PRAGMA journal_mode=DELETE")  # Standalone copy, no WAL sidecars.
        check()
        info = inventory(target)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        entries.append({"name": name, "sha256": fingerprint(target)["sha256"], "inventory": info})
    after = families(paths)  # All three rechecked after LAST snapshot, not per-copy only.
    check()
    if not stable(before, after):
        raise ValueError("source data changed")
    sync_dir(dest)
    publish(dest, "COMPLETE.json", {"version": 1, "complete": True, "snapshots": entries,
            "sources": [str(p) for p in paths], "before": before, "after": after}, check)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def valid_fingerprint(value):
    if not isinstance(value, dict) or set(value) not in ({"stat", "sha256"}, {"stat", "sha256", "xattrs"}):
        return False
    if "xattrs" in value and (not isinstance(value["xattrs"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) and len(v) % 2 == 0 and
            all(c in "0123456789abcdef" for c in v) for k, v in value["xattrs"].items())):
        return False
    fields, digest = value["stat"], value["sha256"]
    return (isinstance(fields, list) and len(fields) in (6, 9) and
            all(type(n) is int for n in fields) and all(n >= 0 for n in fields[:3] + fields[6:]) and
            stat.S_ISREG(fields[5]) and isinstance(digest, str) and len(digest) == 64 and
            all(c in "0123456789abcdef" for c in digest))


def verify(bundle: Path, scratch: Path):
    bundle, scratch = safe_path(bundle), safe_path(scratch)
    if bundle == scratch or bundle in scratch.parents:
        raise ValueError("scratch overlaps bundle")
    scratch.mkdir(mode=0o700)
    sync_dir(scratch.parent)
    manifest_path = safe_path(bundle / "COMPLETE.json", regular=True)
    manifest = json.loads(manifest_path.read_text(), object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON")))
    if not isinstance(manifest, dict) or set(manifest) != {"version", "complete", "snapshots", "sources", "before", "after"}:
        raise ValueError("invalid manifest")
    entries = manifest["snapshots"]
    if type(manifest["version"]) is not int or manifest["version"] != 1 or manifest["complete"] is not True:
        raise ValueError("incomplete manifest")
    if not isinstance(entries, list) or len(entries) != 3:
        raise ValueError("three snapshots required")
    if not all(isinstance(manifest[key], list) and len(manifest[key]) == 3
               for key in ("sources", "before", "after")):
        raise ValueError("invalid source inventory")
    if not all(isinstance(p, str) and Path(p).is_absolute() and ".." not in Path(p).parts and
               str(Path(p)) == p for p in manifest["sources"]) or len(set(manifest["sources"])) != 3:
        raise ValueError("invalid source paths")
    if not all(isinstance(f, dict) and set(f) == {"", "-wal", "-journal", "-shm"}
               for f in manifest["before"] + manifest["after"]):
        raise ValueError("unstable source inventory")
    for phase in ("before", "after"):
        families = manifest[phase]
        if not all(valid_fingerprint(f[""]) and all(v is None or valid_fingerprint(v)
                   for v in f.values()) for f in families):
            raise ValueError("invalid source fingerprint")
        if len({tuple(f[""]["stat"][:2]) for f in families}) != 3:
            raise ValueError("aliased source fingerprints")
    if not stable(manifest["before"], manifest["after"]):
        raise ValueError("unstable source inventory")
    if {p.name for p in bundle.iterdir()} != {*NAMES, "COMPLETE.json"}:
        raise ValueError("unexpected bundle contents")
    for entry, name in zip(entries, NAMES):
        if not isinstance(entry, dict) or set(entry) != {"name", "sha256", "inventory"} or entry["name"] != name:
            raise ValueError("invalid snapshot entry")
        source = safe_path(bundle / name, regular=True)
        target = scratch / name
        with source.open("rb") as original, create_file(target) as copied:
            while chunk := original.read(1024 * 1024):
                copied.write(chunk)
            copied.flush()
            os.fsync(copied.fileno())
        if fingerprint(target)["sha256"] != entry["sha256"] or inventory(target) != entry["inventory"]:
            raise ValueError("snapshot verification failed")
    sync_dir(scratch)
    publish(scratch, "VERIFIED.json", {"version": 1, "verified": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("databases", nargs=3, type=Path)
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("scratch", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            backup(args.databases, args.output)
        else:
            verify(args.bundle, args.scratch)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Preservation failed; incomplete output must not be used.\n")
    print("Preservation completed." if args.command == "backup" else "Verification completed.")


if __name__ == "__main__":
    main()
