# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Root-level modules are thin entry points; implementations live in hb_engine."""

import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINTS = ('dispatch.py', 'desktop.py')


class RootLayoutTests(unittest.TestCase):
    def test_only_entry_points_remain_at_the_root(self):
        modules = sorted(path.name for path in ROOT.glob('*.py'))
        self.assertEqual(modules, sorted(ENTRY_POINTS))

    def test_entry_points_are_thin(self):
        for name in ENTRY_POINTS:
            lines = (ROOT / name).read_text(encoding='utf-8').splitlines()
            self.assertLessEqual(len(lines), 20, f'{name} should only launch the engine')

    def test_entry_points_import_from_the_engine_package(self):
        self.assertIn('hb_engine', (ROOT / 'dispatch.py').read_text(encoding='utf-8'))
        self.assertIn('hb_engine.tk.app', (ROOT / 'desktop.py').read_text(encoding='utf-8'))

    def test_engine_modules_import_without_the_root_package(self):
        for module in ('dispatch', 'ipc', 'catalog', 'backup', 'search_worker'):
            with self.subTest(module=module):
                self.assertTrue(importlib.import_module(f'hb_engine.{module}'))


if __name__ == '__main__':
    unittest.main()
