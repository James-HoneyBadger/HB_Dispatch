# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Packaging smoke helper: packaged-module IPC checks against the source CLI."""
import importlib.util
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from hb_engine.dispatch import Archive

ROOT = Path(__file__).resolve().parent.parent


def load_build_engine():
    spec = importlib.util.spec_from_file_location(
        'build_engine_smoke', ROOT / 'scripts' / 'build_engine.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildEngineSmokeTests(unittest.TestCase):
    def test_packaging_manifests_match_the_engine_version(self):
        from hb_engine.dispatch import __version__
        package = json.loads((ROOT / 'frontend' / 'package.json').read_text(encoding='utf-8'))
        tauri = json.loads((ROOT / 'src-tauri' / 'tauri.conf.json').read_text(encoding='utf-8'))
        cargo = tomllib.loads((ROOT / 'src-tauri' / 'Cargo.toml').read_text(encoding='utf-8'))
        lock = tomllib.loads((ROOT / 'src-tauri' / 'Cargo.lock').read_text(encoding='utf-8'))
        locked = next(entry['version'] for entry in lock['package']
                      if entry['name'] == 'hb-continuum')
        self.assertEqual({package['version'], tauri['version'],
                          cargo['package']['version'], locked}, {__version__})

    def test_packaging_collects_optional_sqlcipher_binding(self):
        build_engine = load_build_engine()
        with mock.patch.object(build_engine.importlib.util, 'find_spec',
                               side_effect=lambda name: object()
                               if name == 'sqlcipher3' else None):
            command = build_engine.pyinstaller_command()
        self.assertIn('--collect-all', command)
        self.assertIn('sqlcipher3', command)

    def test_build_smoke_expects_the_engine_version(self):
        from hb_engine.dispatch import __version__
        self.assertEqual(load_build_engine().ENGINE_VERSION, __version__)

    def test_ipc_smoke_finds_advanced_results_and_relationship_edge(self):
        build_engine = load_build_engine()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root / 'archive')
            archive.ingest(
                b'From: smoke@example.invalid\r\nSubject: Smoke\r\n'
                b'Date: Mon, 01 Jan 2024 10:00:00 +0000\r\n'
                b'Message-ID: <smoke-1@example.invalid>\r\n\r\nsmokeword')
            archive.ingest(
                b'From: reply@example.invalid\r\nSubject: Re: Smoke\r\n'
                b'Date: Mon, 01 Jan 2024 11:00:00 +0000\r\n'
                b'Message-ID: <smoke-2@example.invalid>\r\n'
                b'In-Reply-To: <smoke-1@example.invalid>\r\n\r\nsmokeword')
            archive.db.close()
            result = build_engine._ipc_smoke(
                [sys.executable, str(ROOT / 'dispatch.py')], root / 'archive')
        self.assertEqual(result['advanced']['result']['total'], 2)
        self.assertGreaterEqual(len(result['graph']['result']['edges']), 1)
        self.assertEqual(result['graph']['result']['edges'][0]['threads'], 1)


if __name__ == '__main__':
    unittest.main()
