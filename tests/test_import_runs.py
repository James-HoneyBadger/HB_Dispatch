# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Import runs: counts, idempotency, cancellation, failures and sync history."""
import os, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
from hb_engine.dispatch import Archive

def message(subject='imported'):
    return (f'From: a@example.invalid\r\nSubject: {subject}\r\n\r\nbody').encode()

class ImportRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root)
        self.inbox=self.root/'inbox'; self.inbox.mkdir()

    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def test_run_records_new_then_existing_counts(self):
        (self.inbox/'one.eml').write_bytes(message())
        first=self.a.import_files([str(self.inbox)])
        self.assertEqual((first['files_seen'],first['imported'],first['existing']),(1,1,0))
        second=self.a.import_files([str(self.inbox)])
        self.assertEqual((second['imported'],second['existing']),(0,1))
        history=self.a.import_history()
        self.assertEqual([row[5] for row in history],['ok','ok'])
        self.assertEqual(history[0][7],1)   # records_seen
        self.assertEqual(history[0][8],0)   # records_new (already present)

    def test_cancelled_run_keeps_its_history(self):
        for index in range(3):
            (self.inbox/f'{index}.eml').write_bytes(message(f'm{index}'))
        event=threading.Event(); event.set()
        result=self.a.import_files([str(self.inbox)],cancel_event=event)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['imported'],0)
        row=self.a.import_history()[0]
        self.assertEqual(row[5],'cancelled')
        self.assertEqual(row[8],0)

    def test_missing_path_and_symlink_are_reported_not_fatal(self):
        good=self.inbox/'good.eml'; good.write_bytes(message('good'))
        link=self.inbox/'broken.eml'
        try:
            link.symlink_to(self.inbox/'missing-target.eml')
            links=True
        except (OSError, NotImplementedError):
            links=False
        result=self.a.import_files([str(good),str(self.inbox/'nope')])
        self.assertEqual(result['imported'],1)
        self.assertEqual(len(result['errors']),1)
        self.assertIn('not found',result['errors'][0]['error'])
        if links:
            result=self.a.import_files([str(self.inbox)])
            self.assertEqual(result['imported'],0)
            self.assertEqual(len(result['errors']),1)
            self.assertEqual(result['existing'],1)

    def test_nested_folders_and_duplicate_paths_are_imported_once(self):
        nested = self.inbox / 'nested' / 'deeper'
        nested.mkdir(parents=True)
        (self.inbox / 'top.eml').write_bytes(message('top'))
        (nested / 'lower.EML').write_bytes(message('lower'))
        result = self.a.import_files([str(self.inbox), str(nested / 'lower.EML')])
        self.assertEqual(result['files_seen'], 2)
        self.assertEqual(result['imported'], 2)
        self.assertEqual(result['errors'], [])

    def test_sync_records_a_run(self):
        with mock.patch.object(Archive,'_sync',return_value=2):
            self.assertEqual(self.a.sync('imap.example','me','secret','INBOX'),2)
        row=self.a.import_history()[0]
        self.assertEqual((row[1],row[2],row[5],row[8]),('imap','imap.example|me|INBOX','ok',2))

    def test_failed_sync_records_an_error_run(self):
        with mock.patch.object(Archive,'_sync',side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.a.sync('imap.example','me','secret','INBOX')
        row=self.a.import_history()[0]
        self.assertEqual(row[5],'error')
        self.assertEqual(row[11],'RuntimeError')

if __name__=='__main__':
    unittest.main()
