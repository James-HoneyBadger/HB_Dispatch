# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Real-process tests for the cross-platform archive write lock."""
import os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from hb_engine.sync_lock import LOCK_SUPPORTED, SyncBusy, file_lock, sync_lock

ROOT = Path(__file__).resolve().parent.parent

HOLDER = (
    'import sys\n'
    'from hb_engine.sync_lock import sync_lock\n'
    'with sync_lock(sys.argv[1]):\n'
    '    print("locked", flush=True)\n'
    '    sys.stdin.read()\n'
)

@unittest.skipUnless(LOCK_SUPPORTED, 'archive locking is unsupported here')
class CrossProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def holder(self):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.Popen(
            [sys.executable, '-c', HOLDER, str(self.root)],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

    def close(self, child):
        for pipe in (child.stdin, child.stdout, child.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    def wait_until_free(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with file_lock(self.root / 'sync.lock'):
                    return True
            except SyncBusy:
                time.sleep(0.05)
        return False

    def test_second_process_is_refused_and_release_is_detected(self):
        child = self.holder()
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(SyncBusy):
                with sync_lock(self.root):
                    self.fail('the lock was granted while another process held it')
            child.stdin.close()
            child.wait(timeout=10)
            self.assertTrue(self.wait_until_free(), 'the lock was not released')
            with sync_lock(self.root):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            self.close(child)

    def test_crashed_holder_does_not_leave_a_stale_lock(self):
        child = self.holder()
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            child.kill()
            child.wait(timeout=10)
        finally:
            self.close(child)
        self.assertTrue(self.wait_until_free(), 'a killed holder left a stale lock')

if __name__ == '__main__':
    unittest.main()
