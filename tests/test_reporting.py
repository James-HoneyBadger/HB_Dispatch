# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Archive report assembly, rendering and exclusive writing."""
import json, tempfile, unittest
from pathlib import Path
from hb_engine.reporting import (build_archive_report, render, render_html, render_markdown,
                       write_report)

SOURCES = {
    'email_accounts': [
        {'account': 'imap.example|me', 'messages': 12, 'origins': 15, 'folders': 3,
         'earliest': '2015-01-02', 'latest': '2024-03-04',
         'last_import': {'status': 'ok', 'at': '2026-11-27T10:00:00+00:00', 'records_new': 4}},
    ],
    'catalog_sources': [
        {'source_type': 'facebook', 'records': 8, 'files': 2, 'bytes': 2048,
         'earliest': '2023-11-14T00:00:00+00:00', 'latest': '2024-01-01T00:00:00+00:00'},
    ],
    'catalog': {'files': 2, 'records': 8},
    'storage': {'raw_bytes': 100, 'database_bytes': 50, 'total_bytes': 150},
    'history_available': True,
}
DASHBOARD = {
    'archive': {'messages': 12, 'origins': 15, 'undated': 1,
                'earliest': '2015-01-02', 'latest': '2024-03-04'},
    'catalog': {'files': 2, 'records': 8},
    'by_year': [{'year': 2023, 'count': 5}, {'year': 2024, 'count': 6}],
    'storage': SOURCES['storage'],
}
HEALTH = {'sqlite_ok': True, 'foreign_key_errors': 0, 'messages': 12, 'undated': 1,
          'index_missing': [], 'index_extra': [], 'index_duplicate_rows': 0,
          'raw_missing': [], 'raw_unindexed': [], 'raw_invalid': ['a' * 64]}
HISTORY = {'available': True, 'runs': [
    {'source': 'eml', 'source_key': '/tmp/inbox', 'status': 'ok', 'started_at': 'a',
     'finished_at': 'b', 'records_new': 3, 'records_existing': 0, 'records_failed': 0}]}

class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.report=build_archive_report(SOURCES, DASHBOARD, HEALTH, HISTORY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_and_health_are_derived_from_real_data(self):
        self.assertEqual(self.report['summary']['messages'],12)
        self.assertEqual(self.report['summary']['storage_bytes'],150)
        self.assertEqual(self.report['health']['raw_invalid'],1)
        self.assertFalse(self.report['health']['consistent'])
        self.assertEqual(len(self.report['imports']),1)
        self.assertTrue(any('not proof' in item for item in self.report['limitations']))

    def test_markdown_contains_every_section(self):
        text=render_markdown(self.report)
        for heading in ('# HB Continuum archive report','## Summary','## Records by year',
                        '## Email accounts','## Catalog sources','## Consistency',
                        '## Recent imports','## Limitations'):
            self.assertIn(heading,text)
        self.assertIn('imap.example\\|me',text)
        self.assertIn('| 2023 | 5 |',text)

    def test_catalog_health_feeds_report_consistency(self):
        catalog={'available':True,'sqlite_ok':True,'foreign_key_errors':0,'records':8,
                 'source_files':2,'index_rows':8,'index_missing_rows':0,
                 'index_extra_rows':0,'index_duplicate_rows':0,
                 'dangling_file_references':0,'invalid_attachments':0}
        health=dict(HEALTH, raw_invalid=[], catalog=catalog)
        report=build_archive_report(SOURCES,DASHBOARD,health,HISTORY)
        self.assertTrue(report['health']['consistent'])
        self.assertTrue(report['health']['catalog']['consistent'])
        health['catalog']=dict(catalog, index_missing_rows=1)
        report=build_archive_report(SOURCES,DASHBOARD,health,HISTORY)
        self.assertFalse(report['health']['consistent'])
        self.assertFalse(report['health']['catalog']['consistent'])
        self.assertIn('Missing index rows', render_markdown(report))

    def test_json_round_trips(self):
        payload=json.loads(render(self.report,'json'))
        self.assertEqual(payload['summary']['messages'],12)
        self.assertEqual(payload['email_accounts'][0]['account'],'imap.example|me')

    def test_html_escapes_untrusted_values(self):
        report=dict(self.report)
        report['title']='<script>alert(1)</script>'
        report['email_accounts']=[{'account':'<b>x</b>','messages':1,'origins':1,
                                   'folders':1,'earliest':None,'latest':None,
                                   'last_import':None}]
        text=render_html(report)
        self.assertNotIn('<script>alert(1)</script>',text)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',text)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;',text)

    def test_write_refuses_to_replace_and_checks_extension(self):
        destination=self.root/'report.md'
        result=write_report(self.report,'markdown',destination)
        self.assertEqual(result['format'],'markdown')
        self.assertTrue(destination.read_text().startswith('# HB Continuum archive report'))
        with self.assertRaises(FileExistsError):
            write_report(self.report,'markdown',destination)
        with self.assertRaises(ValueError):
            write_report(self.report,'json',self.root/'report.md')
        with self.assertRaises(ValueError):
            render(self.report,'pdf')

if __name__=='__main__':
    unittest.main()
