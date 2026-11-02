"""Synthetic-only preservation checks; removing validation/publication gates must fail."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / "deploy" / "preserve-sqlite.py"


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.is_file(), "preservation helper missing")
        spec = importlib.util.spec_from_file_location("preserve_sqlite", HELPER)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = [self.root / f"source {i}#.sqlite" for i in range(3)]
        self.connections = []
        for i, path in enumerate(self.paths):
            conn = sqlite3.connect(path)
            self.connections.append(conn)
            self.addCleanup(conn.close)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute('CREATE TABLE "odd table" (value TEXT)')
            conn.execute('INSERT INTO "odd table" VALUES (?)', (f"row-{i}",))
            conn.commit()
        self.bundle = self.root / "bundle"
        self.scratch = self.root / "scratch"

    def complete(self):
        return self.bundle / "COMPLETE.json"

    def test_wal_three_sources_restore_and_restricted_permissions(self):
        before = [p.read_bytes() for p in self.paths]
        self.helper.backup(self.paths, self.bundle)
        manifest = json.loads(self.complete().read_text())
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["snapshots"]), 3)
        self.helper.verify(self.bundle, self.scratch)
        self.assertTrue((self.scratch / "VERIFIED.json").is_file())
        for i in range(3):
            with sqlite3.connect(self.scratch / f"db{i + 1}.sqlite") as conn:
                self.assertEqual(conn.execute('SELECT * FROM "odd table"').fetchall(), [(f"row-{i}",)])
        self.assertEqual(before, [p.read_bytes() for p in self.paths])
        if os.name == "posix":
            for directory in (self.bundle, self.scratch):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                for file in directory.iterdir():
                    if file.is_file():
                        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_missing_duplicate_alias_and_wrong_source_count_refused(self):
        missing = self.root / "missing.sqlite"
        alias = self.root / "alias.sqlite"
        os.link(self.paths[0], alias)
        for paths in (self.paths[:2], self.paths + [self.paths[0]],
                      [self.paths[0], self.paths[0], self.paths[2]],
                      [self.paths[0], alias, self.paths[2]],
                      [missing, *self.paths[1:]]):
            with self.subTest(paths=paths), self.assertRaises((ValueError, OSError)):
                self.helper.backup(paths, self.bundle)
            self.assertFalse(self.bundle.exists())
        self.assertFalse(missing.exists())

    def test_symlink_sources_and_output_ancestors_refused(self):
        link = self.root / "link.sqlite"
        link.symlink_to(self.paths[0])
        with self.assertRaises((ValueError, OSError)):
            self.helper.backup([link, *self.paths[1:]], self.bundle)
        directory = self.root / "link-dir"
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.helper.backup(self.paths, directory / "bundle")
        self.assertFalse(self.bundle.exists())

    def test_existing_destination_and_scratch_are_never_overwritten(self):
        self.bundle.mkdir()
        marker = self.bundle / "marker"
        marker.write_bytes(b"keep")
        with self.assertRaises((ValueError, OSError)):
            self.helper.backup(self.paths, self.bundle)
        self.assertEqual(marker.read_bytes(), b"keep")
        marker.unlink()
        self.bundle.rmdir()
        self.helper.backup(self.paths, self.bundle)
        self.scratch.mkdir()
        with self.assertRaises((ValueError, OSError)):
            self.helper.verify(self.bundle, self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_timeout_and_interruption_never_publish_complete(self):
        for exception in (TimeoutError("deadline"), KeyboardInterrupt()):
            destination = self.root / type(exception).__name__
            # Boundary injection: real connection setup, progress clock interruption.
            with patch.object(self.helper.time, "monotonic", side_effect=[0, exception]):
                with self.assertRaises(type(exception)):
                    self.helper.backup(self.paths, destination)
            self.assertTrue(destination.is_dir())
            self.assertFalse((destination / "COMPLETE.json").exists())
        with self.assertRaises(TimeoutError):
            self.helper.backup(self.paths, self.bundle, timeout=0)
        self.assertFalse(self.complete().exists())

    def test_progress_deadline_stops_real_backup(self):
        # First call starts deadline, second allows opening, progress exceeds it.
        with patch.object(self.helper.time, "monotonic", side_effect=[0, 0, 31]):
            with self.assertRaises(TimeoutError):
                self.helper.backup(self.paths, self.bundle)
        self.assertFalse(self.complete().exists())

    def test_corrupt_or_incomplete_bundle_never_verified(self):
        self.helper.backup(self.paths, self.bundle)
        snapshot = self.bundle / "db1.sqlite"
        snapshot.write_bytes(b"not sqlite")
        with self.assertRaises((ValueError, OSError, sqlite3.Error)):
            self.helper.verify(self.bundle, self.scratch)
        self.assertFalse((self.scratch / "VERIFIED.json").exists())
        self.complete().unlink()
        with self.assertRaises((ValueError, OSError)):
            self.helper.verify(self.bundle, self.root / "scratch2")

    def test_manifest_filename_traversal_and_extra_snapshots_refused(self):
        self.helper.backup(self.paths, self.bundle)
        original = self.complete().read_text()
        for name in ("../source 0#.sqlite", "/tmp/untrusted.sqlite", "db2.sqlite"):
            manifest = json.loads(original)
            manifest["snapshots"][0]["name"] = name
            self.complete().write_text(json.dumps(manifest))
            scratch = self.root / ("scratch" + str(len(list(self.root.iterdir()))))
            with self.assertRaises(ValueError):
                self.helper.verify(self.bundle, scratch)
            self.assertFalse((scratch / "VERIFIED.json").exists())
        self.complete().write_text(original)
        (self.bundle / "db4.sqlite").write_bytes(b"unexpected")
        with self.assertRaises(ValueError):
            self.helper.verify(self.bundle, self.scratch)

    def test_mutation_of_first_source_during_last_snapshot_fails_closed(self):
        real_connect = self.helper.sqlite3.connect
        source_opens = 0

        def connect(*args, **kwargs):
            nonlocal source_opens
            conn = real_connect(*args, **kwargs)
            if args[0] in {p.as_uri() + "?mode=ro" for p in self.paths}:
                source_opens += 1
                if source_opens == 3:
                    self.connections[0].execute('INSERT INTO "odd table" VALUES ("late")')
                    self.connections[0].commit()
            return conn

        with patch.object(self.helper.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(ValueError):
                self.helper.backup(self.paths, self.bundle)
        self.assertFalse(self.complete().exists())

    def test_benign_wal_reader_ctime_bump_does_not_fail_backup(self):
        # Some SQLite readers/libc stacks (reproduced on Python 3.12.3, SQLite
        # 3.40.1) move WAL st_ctime_ns with mtime/size/sha256 untouched and no
        # committed write. That is reader interference, not source mutation.
        real_connect = self.helper.sqlite3.connect
        source_opens = 0

        def connect(*args, **kwargs):
            nonlocal source_opens
            conn = real_connect(*args, **kwargs)
            if args[0] in {p.as_uri() + "?mode=ro" for p in self.paths}:
                source_opens += 1
                if source_opens == 3:
                    wal = Path(str(self.paths[0]) + "-wal")
                    meta = wal.stat()
                    os.utime(wal, ns=(meta.st_atime_ns, meta.st_mtime_ns))  # ctime bumps, mtime kept
            return conn

        with patch.object(self.helper.sqlite3, "connect", side_effect=connect):
            self.helper.backup(self.paths, self.bundle)
        self.assertTrue(self.complete().exists())
        self.assertEqual(source_opens, 3)
        self.helper.verify(self.bundle, self.scratch)
        self.assertTrue((self.scratch / "VERIFIED.json").exists())

    def test_wal_ctime_tolerance_keeps_all_other_fingerprint_checks(self):
        import copy
        before = self.helper.families(self.paths)
        for suffix in ("", "-wal", "-journal"):
            baseline = copy.deepcopy(before)
            if suffix == "-journal":
                baseline[0][suffix] = copy.deepcopy(before[0]["-wal"])
            for field in range(9):
                with self.subTest(suffix=suffix, field=field):
                    after = copy.deepcopy(baseline)
                    after[0][suffix]["stat"][field] += 1
                    self.assertEqual(self.helper.stable(baseline, after),
                                     suffix == "-wal" and field == 4)
            after = copy.deepcopy(baseline)
            after[0][suffix]["sha256"] = "0" * 64
            self.assertFalse(self.helper.stable(baseline, after))
            after = copy.deepcopy(baseline)
            after[0][suffix] = None
            self.assertFalse(self.helper.stable(baseline, after))
        after = copy.deepcopy(before)
        after[0]["-journal"] = copy.deepcopy(before[0]["-wal"])
        self.assertFalse(self.helper.stable(before, after))

    def test_legacy_manifest_and_malformed_wal_fingerprints(self):
        import copy
        self.helper.backup(self.paths, self.bundle)
        original = json.loads(self.complete().read_text())
        legacy = copy.deepcopy(original)
        for phase in ("before", "after"):
            for family in legacy[phase]:
                for value in family.values():
                    if value is not None:
                        value["stat"] = value["stat"][:6]
                        value.pop("xattrs", None)
        # A successful legacy capture required identical WAL ctime endpoints.
        for before, after in zip(legacy["before"], legacy["after"]):
            after["-wal"]["stat"][4] = before["-wal"]["stat"][4]
        self.complete().write_text(json.dumps(legacy))
        self.helper.verify(self.bundle, self.root / "legacy")
        legacy["after"][0]["-wal"]["stat"][4] += 1
        self.complete().write_text(json.dumps(legacy))
        with self.assertRaises(ValueError):
            self.helper.verify(self.bundle, self.root / "legacy-ctime")
        for index, invalid in enumerate(({}, [], 1, {"stat": None, "sha256": "0" * 64})):
            with self.subTest(invalid=invalid):
                manifest = copy.deepcopy(original)
                manifest["before"][0]["-wal"] = invalid
                self.complete().write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    self.helper.verify(self.bundle, self.root / f"invalid-wal-{index}")

    def test_wal_metadata_read_failure_blocks_publication(self):
        for operation in ("listxattr", "getxattr"):
            with self.subTest(operation=operation):
                wal = Path(str(self.paths[0]) + "-wal")
                os.setxattr(wal, "user.preservation", b"readable")
                bundle = self.root / operation
                with patch.object(self.helper.os, operation, side_effect=PermissionError("denied")):
                    with self.assertRaises(PermissionError):
                        self.helper.backup(self.paths, bundle)
                self.assertFalse((bundle / "COMPLETE.json").exists())

    def test_wal_xattr_or_acl_change_during_backup_fails_closed(self):
        import struct
        for attribute, value in (
            ("user.preservation", b"changed"),
            ("system.posix_acl_access", struct.pack("<I", 2) + b"".join(
                struct.pack("<HHI", tag, permissions, identity)
                for tag, permissions, identity in (
                    (1, 6, 0xffffffff), (2, 6, 12345), (4, 6, 0xffffffff),
                    (16, 6, 0xffffffff), (32, 0, 0xffffffff)))),
        ):
            with self.subTest(attribute=attribute):
                wal = Path(str(self.paths[0]) + "-wal")
                wal.chmod(0o660)
                real_inventory = self.helper.inventory
                calls = 0

                def inspect(path):
                    nonlocal calls
                    result = real_inventory(path)
                    calls += 1
                    if calls == 3:
                        meta = wal.stat()
                        os.setxattr(wal, attribute, value)
                        self.assertEqual(wal.stat().st_mode, meta.st_mode)
                        self.assertEqual(wal.stat().st_mtime_ns, meta.st_mtime_ns)
                    return result

                bundle = self.root / attribute.replace(".", "-")
                with patch.object(self.helper, "inventory", side_effect=inspect):
                    with self.assertRaises(ValueError):
                        self.helper.backup(self.paths, bundle)
                self.assertFalse((bundle / "COMPLETE.json").exists())
                os.removexattr(wal, attribute)

    def test_wal_permission_change_during_backup_fails_closed(self):
        real_inventory = self.helper.inventory
        calls = 0

        def inspect(path):
            nonlocal calls
            result = real_inventory(path)
            calls += 1
            if calls == 3:
                wal = Path(str(self.paths[0]) + "-wal")
                wal.chmod(stat.S_IMODE(wal.stat().st_mode) ^ stat.S_IXUSR)
            return result

        with patch.object(self.helper, "inventory", side_effect=inspect):
            with self.assertRaises(ValueError):
                self.helper.backup(self.paths, self.bundle)
        self.assertFalse(self.complete().exists())

    def test_verify_rejects_rehashed_corruption_and_inventory_mismatch(self):
        self.helper.backup(self.paths, self.bundle)
        manifest = json.loads(self.complete().read_text())
        manifest["snapshots"][0]["inventory"]["counts"]["odd table"] = 99
        self.complete().write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.helper.verify(self.bundle, self.root / "wrong-counts")
        snapshot = self.bundle / "db1.sqlite"
        snapshot.write_bytes(b"corrupt but matching hash")
        manifest["snapshots"][0]["sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        self.complete().write_text(json.dumps(manifest))
        with self.assertRaises((ValueError, sqlite3.Error)):
            self.helper.verify(self.bundle, self.root / "bad-integrity")
        self.assertFalse((self.root / "bad-integrity" / "VERIFIED.json").exists())

    def test_mutation_after_last_snapshot_and_sidecar_symlink_refused(self):
        real_inventory = self.helper.inventory
        calls = 0

        def inspect(path):
            nonlocal calls
            result = real_inventory(path)
            calls += 1
            if calls == 3:
                self.connections[0].execute('INSERT INTO "odd table" VALUES ("after last")')
                self.connections[0].commit()
            return result

        with patch.object(self.helper, "inventory", side_effect=inspect):
            with self.assertRaises(ValueError):
                self.helper.backup(self.paths, self.bundle)
        self.assertFalse(self.complete().exists())
        journal = Path(str(self.paths[1]) + "-journal")
        journal.symlink_to(self.paths[0])
        with self.assertRaises(ValueError):
            self.helper.backup(self.paths, self.root / "sidecar-link")

    def test_publication_fsync_failure_leaves_no_complete(self):
        real_sync = self.helper.sync_dir

        def sync(path):
            if path == self.bundle and self.complete().exists():
                raise OSError("injected publication directory fsync failure")
            return real_sync(path)

        with patch.object(self.helper, "sync_dir", side_effect=sync):
            with self.assertRaises(OSError):
                self.helper.backup(self.paths, self.bundle)
        self.assertFalse(self.complete().exists())

    def test_interruption_after_real_link_removes_owned_marker(self):
        real_link = self.helper.os.link

        def link(*args, **kwargs):
            real_link(*args, **kwargs)
            raise KeyboardInterrupt()

        with patch.object(self.helper.os, "link", side_effect=link):
            with self.assertRaises(KeyboardInterrupt):
                self.helper.backup(self.paths, self.bundle)
        self.assertFalse(self.complete().exists())

    def test_parent_components_refused_before_symlink_normalization(self):
        other = self.root / "other"
        other.mkdir()
        link = self.root / "link"
        link.symlink_to(other, target_is_directory=True)
        tricky = link / ".." / self.paths[0].name
        for i, value in enumerate((tricky, str(tricky))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.helper.backup([value, *self.paths[1:]], self.root / f"bad-source{i}")
        with self.assertRaises(ValueError):
            self.helper.backup(self.paths, link / ".." / "bad-output")
        self.helper.backup(self.paths, self.bundle)
        with self.assertRaises(ValueError):
            self.helper.verify(link / ".." / "bundle", self.scratch)
        with self.assertRaises(ValueError):
            self.helper.verify(self.bundle, str(link / ".." / "scratch"))

    def test_impossible_manifest_provenance_refused_without_source_access(self):
        self.helper.backup(self.paths, self.bundle)
        original = self.complete().read_text()
        for case in ("duplicates", "null", "identity", "bad-hash", "bad-stat", "boolean-stat"):
            manifest = json.loads(original)
            if case == "duplicates":
                manifest["sources"] = ["/invented/source"] * 3
            else:
                for phase in ("before", "after"):
                    if case == "null":
                        manifest[phase] = [{key: None for key in family} for family in manifest[phase]]
                    elif case == "identity":
                        manifest[phase][1][""]["stat"][:2] = manifest[phase][0][""]["stat"][:2]
                    elif case == "bad-hash":
                        manifest[phase][0][""]["sha256"] = "not a digest"
                    elif case == "bad-stat":
                        manifest[phase][0][""]["stat"] = [1]
                    else:
                        manifest[phase][0][""]["stat"][0] = True
            self.complete().write_text(json.dumps(manifest))
            scratch = self.root / case
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    self.helper.verify(self.bundle, scratch)
                self.assertFalse((scratch / "VERIFIED.json").exists())

    def test_deadline_after_directory_durability_never_publishes_success(self):
        real_sync = self.helper.sync_dir
        for after_publication in (False, True):
            destination = self.root / str(after_publication)
            now = [0]

            def sync(path):
                real_sync(path)
                if path == destination and (destination / "COMPLETE.json").exists() == after_publication:
                    now[0] = 31

            with patch.object(self.helper.time, "monotonic", side_effect=lambda: now[0]):
                with patch.object(self.helper, "sync_dir", side_effect=sync):
                    with self.assertRaises(TimeoutError):
                        self.helper.backup(self.paths, destination)
            self.assertFalse((destination / "COMPLETE.json").exists())

    def test_new_directory_parent_fsync_failure_blocks_both_markers(self):
        real_sync = self.helper.sync_dir

        def sync(path):
            if path == self.root:
                raise OSError("parent durability failure")
            return real_sync(path)

        with patch.object(self.helper, "sync_dir", side_effect=sync):
            with self.assertRaises(OSError):
                self.helper.backup(self.paths, self.root / "bad-parent")
        self.assertFalse((self.root / "bad-parent" / "COMPLETE.json").exists())
        self.helper.backup(self.paths, self.bundle)
        with patch.object(self.helper, "sync_dir", side_effect=sync):
            with self.assertRaises(OSError):
                self.helper.verify(self.bundle, self.scratch)
        self.assertFalse((self.scratch / "VERIFIED.json").exists())

    def test_unbounded_timeout_inputs_refused(self):
        for timeout in (-1, float("inf"), float("nan"), True, "30"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.helper.backup(self.paths, self.bundle, timeout=timeout)
            self.assertFalse(self.bundle.exists())

    def test_stop_and_historical_files_untouched(self):
        files = [self.root / "STOP", self.root / "historical.sqlite"]
        for file in files:
            file.write_bytes(b"historical evidence\x00")
            file.chmod(0o400)
        before = [(f.read_bytes(), f.stat()) for f in files]
        self.helper.backup(self.paths, self.bundle)
        self.helper.verify(self.bundle, self.scratch)
        for file, (data, info) in zip(files, before):
            self.assertEqual(file.read_bytes(), data)
            after = file.stat()
            self.assertEqual((after.st_mode, after.st_mtime_ns, after.st_ctime_ns),
                             (info.st_mode, info.st_mtime_ns, info.st_ctime_ns))


if __name__ == "__main__":
    unittest.main()
