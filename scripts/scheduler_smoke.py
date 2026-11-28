#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Install, inspect and remove a real scheduler entry, for smoke testing.

Exit codes: 0 = verified or skipped (reason printed), 1 = verified failure.
A skip means this environment cannot host the scheduler (no user session, no
Task Scheduler service); a failure means our install/status/uninstall is wrong.
"""
import sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hb_engine.schedulers import SchedulerError, get_scheduler

SECRET_MARKERS = ('password', 'passwd', 'secret')
SKIP_MARKERS = ('Failed to connect to bus', 'Bootstrap failed', 'Could not find domain',
                'No systemd user session', 'Access is denied', 'cannot connect',
                'Operation not permitted')

def generated_files(scheduler, root):
    candidates = []
    for method in ('service_path', 'timer_path', 'path'):
        getter = getattr(scheduler, method, None)
        if getter is not None:
            candidates.append(getter(root))
    return [p for p in candidates if p is not None]

def main():
    scheduler = get_scheduler()
    print('scheduler:', scheduler.name)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'archive'
        root.mkdir()
        try:
            print('installed:', scheduler.install(root, None, 'daily', None, '03:17'))
        except SchedulerError as exc:
            text = str(exc)
            if any(marker in text for marker in SKIP_MARKERS):
                print('SKIP: environment cannot host a scheduler entry:', text)
                return 0
            print('FAIL: install raised:', text)
            return 1

        status = scheduler.status(root)
        print('status:', status)
        if status in ('Not installed', 'Not supported on this platform'):
            print('FAIL: status does not reflect an installed entry')
            return 1

        for path in generated_files(scheduler, root):
            text = Path(path).read_text(encoding='utf-8', errors='replace').lower()
            found = [m for m in SECRET_MARKERS if m in text]
            if found:
                print(f'FAIL: {path} mentions {found}')
                return 1
        print('no secret markers in generated files')

        scheduler.uninstall(root)
        after = scheduler.status(root)
        print('status after uninstall:', after)
        if after != 'Not installed':
            print('FAIL: entry survived uninstall')
            return 1
        leftover = [str(p) for p in generated_files(scheduler, root) if Path(p).exists()]
        if leftover:
            print('FAIL: files left behind:', leftover)
            return 1
    print('OK: verified')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
