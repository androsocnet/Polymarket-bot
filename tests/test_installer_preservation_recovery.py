"""Scratch-only rehearsal of documented metadata inverse, NOT installer execution."""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'GNU cp metadata rehearsal requires Linux/WSL')
class MetadataRecovery(unittest.TestCase):
    def test_partial_dependency_or_unit_failure_restores_metadata_not_newer_data(self):
        for stage in ('dependencies', 'unit-verification'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                live, saved = root / 'live', root / 'saved'
                live.mkdir(mode=0o750)
                saved.mkdir(mode=0o700)
                original = {'unit': b'old unit\n', 'config': b'MODE=PAPER_ONLY\n',
                            'controller-owner': b'old-sha'}
                modes = {'unit': 0o644, 'config': 0o640, 'controller-owner': 0o600}
                for name, data in original.items():
                    path = live / name
                    path.write_bytes(data)
                    path.chmod(modes[name])
                    shutil.copy2(path, saved / name)
                stop, history = live / 'STOP', live / 'historical-result.json'
                stop.write_bytes(b'keep stopped\n')
                history.write_bytes(b'{"verdict":"FAIL"}\n')
                protected = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                             for p in (stop, history)}
                db = live / 'paper.sqlite'
                with sqlite3.connect(db) as conn:
                    conn.execute('CREATE TABLE history(value)')
                    conn.execute('INSERT INTO history VALUES (1)')
                partial = live / 'partial-release'
                partial.mkdir()
                (partial / 'failure.log').write_text(stage)
                (live / 'controller-owner').write_bytes(b'candidate-sha')
                live.chmod(0o700)
                if stage == 'unit-verification':
                    (live / 'unit').write_bytes(b'partial new unit\n')
                # Newer data must survive recovery, even if it invalidates deployment readiness.
                with sqlite3.connect(db) as conn:
                    conn.execute('INSERT INTO history VALUES (2)')
                db_hash = hashlib.sha256(db.read_bytes()).hexdigest()
                failed = {name: (live / name).read_bytes() for name in original}
                for name in original:
                    target = live / name
                    self.assertFalse(target.is_symlink())
                    self.assertEqual(target.read_bytes(), failed[name])
                    subprocess.run(['cp', '--preserve=all', '--no-dereference', '--',
                                    str(saved / name), str(target)], check=True)
                live.chmod(0o750)  # non-recursive, recorded prior mode only
                for name, data in original.items():
                    self.assertEqual((live / name).read_bytes(), data)
                    self.assertEqual(stat.S_IMODE((live / name).stat().st_mode), modes[name])
                self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o750)
                self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), db_hash)
                with sqlite3.connect(db) as conn:
                    self.assertEqual(conn.execute('SELECT value FROM history').fetchall(), [(1,), (2,)])
                for name, expected in protected.items():
                    path = live / name
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
                self.assertEqual((partial / 'failure.log').read_text(), stage)

    def test_changed_post_failure_identity_blocks_inverse(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'controller-owner'
            target.write_bytes(b'failed-attempt')
            recorded = hashlib.sha256(target.read_bytes()).hexdigest()
            target.write_bytes(b'newer-owner-work')
            with self.assertRaisesRegex(ValueError, 'post-failure identity changed'):
                if hashlib.sha256(target.read_bytes()).hexdigest() != recorded:
                    raise ValueError('post-failure identity changed')
                target.write_bytes(b'old-owner')
            self.assertEqual(target.read_bytes(), b'newer-owner-work')


if __name__ == '__main__':
    unittest.main()
