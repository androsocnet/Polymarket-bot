"""Offline cleanup regression; never invokes systemd."""
import errno
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('probe', Path(__file__).with_name('check_watchdog_cgroup_reuse.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class CleanupTests(unittest.TestCase):
    def test_write_failure_does_not_skip_kill_stop_reset_or_close(self):
        calls = []
        def command(*args):
            calls.append(args)
            if 'reset-failed' in args:
                raise TimeoutError('reset failed')
        with patch.object(probe, 'run', side_effect=command), \
             patch.object(probe.os, 'write', side_effect=OSError(errno.EIO, 'write failed')), \
             patch.object(probe.os, 'close') as close:
            errors = probe.cleanup('wd-proof-test.service', (11, 12, 13))
        self.assertEqual([args[1] for args in calls], ['kill', 'stop', 'reset-failed'])
        self.assertEqual([call.args[0] for call in close.call_args_list], [11, 12, 13])
        self.assertEqual(len(errors), 2)


if __name__ == '__main__':
    unittest.main()
