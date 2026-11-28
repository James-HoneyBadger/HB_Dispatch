# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""User-authored journal entries: revisions, soft deletion and validation."""
import tempfile, unittest
from pathlib import Path
from hb_engine.dispatch import Archive
from hb_engine.research import Research

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.archive=Archive(self.root)
        self.research=Research(self.archive)

    def tearDown(self):
        self.research.close(); self.archive.db.close(); self.tmp.cleanup()

    def test_create_records_first_revision(self):
        entry=self.research.journal_create('First memory','I remember this.','2015-06-01')
        self.assertEqual(entry['revision'],1)
        self.assertEqual(entry['event_date'],'2015-06-01')
        self.assertEqual(len(entry['revisions']),1)
        self.assertEqual(entry['revisions'][0]['body'],'I remember this.')
        listed=self.research.journal_list()
        self.assertEqual(listed[0]['id'],entry['id'])
        self.assertNotIn('body',listed[0])

    def test_update_appends_revision_and_keeps_history(self):
        entry=self.research.journal_create('Title','one')
        second=self.research.journal_update(entry['id'],'Title','two','2016-01-01')
        self.assertEqual(second['revision'],2)
        self.assertEqual([r['revision'] for r in second['revisions']],[2,1])
        self.assertEqual([r['body'] for r in second['revisions']],['two','one'])
        self.assertTrue(second['created_at'])
        self.assertEqual(second['revisions'][0]['recorded_at'],second['updated_at'])

    def test_unchanged_update_does_not_add_a_revision(self):
        entry=self.research.journal_create('Title','same','2020-02-02')
        again=self.research.journal_update(entry['id'],'Title','same','2020-02-02')
        self.assertEqual(again['revision'],1)
        self.assertEqual(len(again['revisions']),1)

    def test_soft_delete_hides_the_entry_but_keeps_revisions(self):
        entry=self.research.journal_create('Gone','kept in history')
        deleted=self.research.journal_delete(entry['id'])
        self.assertIsNotNone(deleted['deleted_at'])
        self.assertEqual(self.research.journal_list(),[])
        self.assertEqual(len(self.research.journal_list(include_deleted=True)),1)
        self.assertEqual(len(self.research.journal_get(entry['id'])['revisions']),1)
        with self.assertRaises(ValueError):
            self.research.journal_get(9999)

    def test_validation_rejects_empty_or_unbounded_input(self):
        with self.assertRaises(ValueError):
            self.research.journal_create('')
        with self.assertRaises(ValueError):
            self.research.journal_create('x'*201)
        with self.assertRaises(ValueError):
            self.research.journal_create('Title','body','not-a-date')
        with self.assertRaises(ValueError):
            self.research.journal_list(limit=0)
        entry=self.research.journal_create('Title')
        with self.assertRaises(ValueError):
            self.research.journal_update(9999,'Title','body')

if __name__=='__main__':
    unittest.main()
