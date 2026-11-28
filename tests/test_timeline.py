# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Normalized chronology: timestamp parsing, migration backfill and ordering."""
import sqlite3, tempfile, unittest
from pathlib import Path
from hb_engine.dispatch import Archive, timeline_rows, correspondent_counts
from hb_engine.migrations import MIGRATIONS, apply, current_version
from hb_engine.timestamps import parse_timestamp

def message(date, body='body', sender='s@example.invalid'):
    return (f'Subject: test\r\nFrom: {sender}\r\nDate: {date}\r\n\r\n{body}').encode()

class TimestampTests(unittest.TestCase):
    def test_rfc_and_iso_dates_normalize(self):
        epoch, precision = parse_timestamp('Tue, 02 Jan 2024 03:04:05 +0000')
        self.assertEqual(precision, 'second')
        self.assertEqual(epoch, 1704164645)
        self.assertEqual(parse_timestamp('2024-01-02')[0], 1704153600)
        self.assertEqual(parse_timestamp('2024-01-02')[1], 'day')
        self.assertEqual(parse_timestamp('2024-01-02T03:04:05Z')[0], 1704164645)

    def test_unknown_dates_are_not_guessed(self):
        for value in ('', '   ', 'not a date', None, 5):
            self.assertEqual(parse_timestamp(value), (None, None))

class TimelineOrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root)

    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def add(self, date, body, sender='s@example.invalid'):
        digest=self.a.ingest(message(date, body, sender))
        return digest

    def test_ingest_records_a_normalized_timestamp_and_size(self):
        raw=message('Tue, 02 Jan 2024 03:04:05 +0000','dated')
        digest=self.a.ingest(raw)
        row=self.a.db.execute('SELECT sort_ts,date_precision,size FROM messages WHERE hash=?',(digest,)).fetchone()
        self.assertEqual(row,(1704164645,'second',len(raw)))
        unknown=self.add('not a date','undated')
        self.assertEqual(self.a.db.execute('SELECT sort_ts FROM messages WHERE hash=?',(unknown,)).fetchone()[0],None)

    def test_ordering_is_chronological_with_unknown_dates_last(self):
        newest=self.add('Wed, 03 Jan 2024 00:00:00 +0000','newest')
        unknown=self.add('garbage','unknown')
        oldest=self.add('Mon, 01 Jan 2024 00:00:00 +0000','oldest')
        asc=[h for h,_,_,_ in self.a.timeline()]
        self.assertEqual(asc,[oldest,newest,unknown])
        desc=[h for h,_,_,_ in self.a.timeline(order='desc')]
        self.assertEqual(desc,[newest,oldest,unknown])

    def test_pagination_does_not_repeat_or_drop_rows(self):
        hashes=[self.add(f'Mon, 0{day} Jan 2024 00:00:00 +0000',f'day{day}') for day in range(1,6)]
        seen=[]
        for offset in range(0,6,2):
            seen.extend(h for h,_,_,_ in self.a.timeline(limit=2,offset=offset))
        self.assertEqual(seen,hashes)

    def test_sender_filter_and_date_range(self):
        first=self.add('Mon, 01 Jan 2024 00:00:00 +0000','one',sender='a@example.invalid')
        second=self.add('Tue, 02 Jan 2024 00:00:00 +0000','two',sender='b@example.invalid')
        self.assertEqual([h for h,_,_,_ in self.a.timeline(sender='a@example.invalid')],[first])
        self.assertEqual([h for h,_,_,_ in self.a.timeline(after=1704153600)],[second])

    def test_disabled_accounts_are_hidden_on_request(self):
        disabled=self.add('Mon, 01 Jan 2024 00:00:00 +0000','one',sender='a@example.invalid')
        enabled=self.add('Tue, 02 Jan 2024 00:00:00 +0000','two',sender='b@example.invalid')
        self.a.db.executemany('INSERT INTO origins VALUES(?,?,?,?,?)',[
            ('a@example.invalid','INBOX','1','1',disabled),
            ('b@example.invalid','INBOX','1','1',enabled)])
        self.a.db.commit()
        self.a.source_set_enabled('email','a@example.invalid',False)
        self.assertEqual([h for h,_,_,_ in timeline_rows(self.a.db, exclude_disabled=True)],
                         [enabled])
        self.assertEqual(timeline_rows(self.a.db)[0][0],disabled)
        self.assertEqual(correspondent_counts(self.a.db, exclude_disabled=True),
                         [('b@example.invalid',1)])
        self.assertEqual(self.a.timeline(exclude_disabled=True)[0][0],enabled)

    def test_legacy_schema_is_ordered_in_memory(self):
        db=sqlite3.connect(self.root/'legacy.sqlite3')
        try:
            for statement in MIGRATIONS['archive'][1]:
                db.execute(statement)
            db.execute('PRAGMA user_version=1')
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                       ('b'*64,'new','s','Wed, 03 Jan 2024 00:00:00 +0000','<m>',None,'body'))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                       ('a'*64,'old','s','Mon, 01 Jan 2024 00:00:00 +0000','<n>',None,'body'))
            db.commit()
            rows=timeline_rows(db)
            self.assertEqual([row[0] for row in rows],[('a'*64),('b'*64)])
        finally:
            db.close()

class BackfillMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_three_backfills_existing_dates(self):
        db=sqlite3.connect(self.root/'archive.sqlite3')
        try:
            for statement in MIGRATIONS['archive'][1]:
                db.execute(statement)
            db.execute('PRAGMA user_version=1')
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                       ('a'*64,'Hi','s','Tue, 02 Jan 2024 03:04:05 +0000','<m>',None,'body'))
            db.commit()
            apply(db,'archive')
            self.assertEqual(current_version(db),6)
            self.assertEqual(db.execute('SELECT sort_ts,date_precision FROM messages').fetchone(),
                             (1704164645,'second'))
        finally:
            db.close()

if __name__ == '__main__':
    unittest.main()
