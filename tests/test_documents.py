# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import json, tempfile, unittest
from unittest import mock
from pathlib import Path

from hb_engine.dispatch import Archive
from hb_engine.documents import preview, import_files
from hb_engine.intelligence import Intelligence
from hb_engine.unified import search_unified
from hb_engine.advanced_search import search_advanced


class DocumentImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)
        self.sources = self.root / 'documents'
        self.sources.mkdir()
        (self.sources / 'notes.txt').write_text(
            'The cobalt project began with a handwritten note.', encoding='utf-8')
        (self.sources / 'brief.md').write_text(
            '# Cobalt brief\n\nThe cobalt brief records the first supplier meeting.',
            encoding='utf-8')
        (self.sources / 'ledger.csv').write_text(
            'date,item,amount\n2020-01-01,cobalt sample,50\n', encoding='utf-8')
        (self.sources / 'metadata.json').write_text(
            json.dumps({'project': 'cobalt', 'stage': 'prototype'}), encoding='utf-8')
        (self.sources / 'broken.json').write_text('{not json', encoding='utf-8')
        (self.sources / 'unsupported.bin').write_bytes(b'\x00\x01')
        (self.sources / 'fake.pdf').write_bytes(b'not a real pdf')

    def tearDown(self):
        self.archive.db.close()
        self.tmp.cleanup()

    def test_preview_reports_supported_and_optional_extractors(self):
        with mock.patch('hb_engine.documents.extractor_name',
                        side_effect=lambda extension: None if extension == '.pdf' else 'builtin'):
            result = preview([str(self.sources)])
        self.assertEqual(result['files_discovered'], 7)
        self.assertGreaterEqual(result['unsupported_files'], 1)
        self.assertTrue(any('fake.pdf' in warning for warning in result['extractor_warnings']))
        pdf = next(item for item in result['files'] if item['label'] == 'fake.pdf')
        self.assertFalse(pdf['supported'])
        self.assertIsNone(pdf['extractor'])
        labels = {item['label'] for item in result['files']}
        self.assertIn('notes.txt', labels)
        self.assertIn('unsupported.bin', labels)

    def test_import_indexes_text_markdown_csv_and_json(self):
        result = import_files([str(self.sources)], self.root)
        self.assertEqual(result['records_imported'], 4)
        self.assertGreater(result['characters_indexed'], 0)
        self.assertEqual(result['extraction_failures'], 2)  # invalid JSON and fake PDF
        self.assertEqual(result['files_skipped'], 0)
        hits = search_unified(self.root, 'cobalt', sources=('document',))
        self.assertGreaterEqual(len(hits), 4)
        self.assertEqual({item['source'] for item in hits}, {'document'})
        advanced = search_advanced(
            self.root, {'type': 'rule', 'kind': 'text', 'value': 'supplier'},
            sources=('facebook',))
        self.assertTrue(any(item['source'] == 'document' for item in advanced['items']))

    def test_reimport_skips_unchanged_and_preserves_revisions(self):
        first = import_files([str(self.sources / 'notes.txt')], self.root)
        self.assertEqual(first['records_imported'], 1)
        second = import_files([str(self.sources / 'notes.txt')], self.root)
        self.assertEqual(second['files_skipped'], 1)
        self.assertEqual(second['records_imported'], 0)
        (self.sources / 'notes.txt').write_text(
            'The cobalt project was renamed after the second review.', encoding='utf-8')
        third = import_files([str(self.sources / 'notes.txt')], self.root)
        self.assertEqual(third['records_imported'], 1)
        rows = self.archive.db.execute(
            'SELECT 1 FROM messages').fetchall()
        self.assertEqual(rows, [])
        from hb_engine.catalog import Catalog
        catalog = Catalog(self.root)
        try:
            revisions = catalog.db.execute(
                'SELECT body,revision_key,revision_index FROM records '
                "WHERE source_type='document' ORDER BY revision_index").fetchall()
        finally:
            catalog.close()
        self.assertEqual([row[2] for row in revisions], [1, 2])
        self.assertEqual(len({row[1] for row in revisions}), 1)
        self.assertIn('renamed', revisions[-1][0])

    def test_import_history_is_recorded(self):
        import_files([str(self.sources / 'notes.txt')], self.root)
        runs = self.archive.import_history()
        self.assertTrue(runs)
        self.assertEqual(runs[0][1], 'document')
        self.assertEqual(runs[0][8], 1)  # records_new

    def test_documents_participate_in_grounded_retrieval(self):
        import_files([str(self.sources / 'notes.txt')], self.root)
        result = Intelligence(self.root, prefs={'ai_model': 'fake'}).retrieve({
            'question': 'cobalt project handwritten note',
            'sources': ['document'],
        })
        self.assertTrue(result['evidence'])
        evidence = result['evidence'][0]
        self.assertEqual(evidence['source'], 'document')
        self.assertTrue(evidence['source_location'].startswith('catalog.sqlite3#records/'))

    def test_import_closes_catalog_if_archive_cannot_open(self):
        with mock.patch('hb_engine.catalog.Catalog') as catalog_cls, \
             mock.patch('hb_engine.dispatch.Archive', side_effect=OSError('locked')):
            catalog_cls.return_value = mock.Mock()
            with self.assertRaises(OSError):
                import_files([], self.root)
            catalog_cls.return_value.close.assert_called_once()

    def test_missing_paths_and_unsupported_are_reported(self):
        result = import_files([str(self.sources / 'missing.txt')], self.root)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['records_imported'], 0)


if __name__ == '__main__':
    unittest.main()
