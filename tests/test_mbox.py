# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import hashlib, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

from dispatch import Archive
from hb_engine import mbox

FIXTURE = """From ada.example@example.invalid Tue Mar 14 09:15:00 2023
From: Ada Example <ada.example@example.invalid>
To: Grace Example <grace.example@example.invalid>
Subject: Fictional invoice QS-1042
Date: Tue, 14 Mar 2023 09:15:00 +0000
Message-ID: <mbox-fixture-1@example.invalid>
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

This message is fictional. The invoice number is QS-1042.
>From a line that must not split the message.

From grace.example@example.invalid Wed Mar 15 10:00:00 2023
From: Grace Example <grace.example@example.invalid>
Subject: Re: Fictional invoice QS-1042
Date: Wed, 15 Mar 2023 10:00:00 +0000
Message-ID: <mbox-fixture-2@example.invalid>
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Acknowledged, entirely fictional. Grace Example.
"""


class MboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.box = self.root / 'fixture.mbox'
        self.box.write_text(FIXTURE, encoding='utf-8')
        self.archive = Archive(self.root / 'archive')

    def tearDown(self):
        self.archive.db.close()


class PreviewTests(MboxCase):
    def test_preview_reports_counts_and_dates(self):
        report = mbox.preview(self.box)
        self.assertEqual(report['messages'], 2)
        self.assertEqual(report['malformed'], 0)
        self.assertEqual(report['duplicate_messages'], 0)
        self.assertLess(report['date_first'], report['date_last'])
        self.assertEqual(report['name'], 'fixture.mbox')
        self.assertIn('not byte-identical', report['note'])

    def test_missing_file_is_rejected(self):
        with self.assertRaises(mbox.MboxError):
            mbox.preview(self.root / 'absent.mbox')

    def test_symbolic_link_is_refused(self):
        link = self.root / 'link.mbox'
        link.symlink_to(self.box)
        with self.assertRaises(mbox.MboxError):
            mbox.preview(link)


MALFORMED = """From broken.example@example.invalid Thu Mar 16 11:00:00 2023
From: Broken Example <broken.example@example.invalid>
Subject: Unclosed multipart boundary
Date: Thu, 16 Mar 2023 11:00:00 +0000
Content-Type: multipart/mixed; boundary="never-closed"

--never-closed
X-Note: truncated
"""


class ParsingDefectTests(MboxCase):
    def setUp(self):
        super().setUp()
        self.broken = self.root / 'broken.mbox'
        self.broken.write_text(MALFORMED, encoding='utf-8')

    def test_preview_reports_parser_defects(self):
        report = mbox.preview(self.broken)
        self.assertEqual(report['messages'], 1)
        self.assertEqual(report['malformed'], 0)
        self.assertTrue(report['warnings'], 'defects must be surfaced')
        self.assertIn('defect', report['warnings'][0]['warning'])

    def test_import_keeps_the_message_and_reports_the_defect(self):
        result = mbox.import_mbox(self.archive, self.broken)
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['warnings'][0]['index'], 0)


class ProvenanceCollisionTests(MboxCase):
    def test_same_name_different_content_in_separate_directories(self):
        first = self.root / 'a'
        second = self.root / 'b'
        first.mkdir()
        second.mkdir()
        (first / 'Inbox.mbox').write_text(FIXTURE, encoding='utf-8')
        (second / 'Inbox.mbox').write_text(FIXTURE.replace('QS-1042', 'QS-9999'),
                                           encoding='utf-8')
        one = mbox.import_mbox(self.archive, first / 'Inbox.mbox')
        two = mbox.import_mbox(self.archive, second / 'Inbox.mbox')
        self.assertEqual(one['failed'], 0)
        self.assertEqual(two['failed'], 0)
        self.assertEqual(one['imported'], 2)
        self.assertEqual(two['imported'], 2)
        accounts = {row[0] for row in self.archive.db.execute(
            'SELECT account FROM origins').fetchall()}
        self.assertEqual(len(accounts), 2)

    def test_changed_export_at_the_same_path_is_accepted(self):
        first = mbox.import_mbox(self.archive, self.box)
        self.assertEqual(first['imported'], 2)
        extra = FIXTURE + """
From third.example@example.invalid Thu Mar 16 12:00:00 2023
From: Third Example <third.example@example.invalid>
Subject: One more fictional invoice
Date: Thu, 16 Mar 2023 12:00:00 +0000

A third fictional message for QS-1042.
"""
        self.box.write_text(extra, encoding='utf-8')
        second = mbox.import_mbox(self.archive, self.box)
        self.assertEqual(second['failed'], 0)
        self.assertEqual(second['imported'], 1)
        self.assertEqual(second['existing'], 2)
        self.assertEqual(self.archive.stats()['messages'], 3)


class ImportTests(MboxCase):
    def test_import_preserves_provenance_and_is_searchable(self):
        result = mbox.import_mbox(self.archive, self.box)
        self.assertEqual(result['imported'], 2)
        self.assertEqual(result['failed'], 0)
        hits = self.archive.find('invoice')
        self.assertEqual(len(hits), 2)
        origins = self.archive.db.execute(
            'SELECT account, folder, uid FROM origins ORDER BY CAST(uid AS INTEGER)').fetchall()
        accounts = [row[0] for row in origins]
        self.assertEqual(len(set(accounts)), 1)
        self.assertTrue(accounts[0].startswith('mbox:fixture.mbox#'))
        self.assertEqual([row[1] for row in origins], ['mbox'] * 2)
        self.assertEqual([row[2] for row in origins], ['0', '1'])

    def test_reimport_is_idempotent(self):
        mbox.import_mbox(self.archive, self.box)
        again = mbox.import_mbox(self.archive, self.box)
        self.assertEqual(again['imported'], 0)
        self.assertEqual(again['existing'], 2)
        self.assertEqual(self.archive.stats()['messages'], 2)

    def test_source_file_is_not_modified(self):
        before = hashlib.sha256(self.box.read_bytes()).hexdigest()
        mbox.import_mbox(self.archive, self.box)
        self.assertEqual(hashlib.sha256(self.box.read_bytes()).hexdigest(), before)

    def test_malformed_message_is_reported_without_aborting(self):
        real_get = mbox.mailbox.mbox.get_message

        def flaky(box, key):
            if key == list(box.keys())[1]:
                raise ValueError('synthetic parse failure')
            return real_get(box, key)

        with mock.patch.object(mbox.mailbox.mbox, 'get_message', flaky):
            result = mbox.import_mbox(self.archive, self.box)
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['errors'][0]['index'], 1)
        self.assertIn('synthetic parse failure', result['errors'][0]['error'])

    def test_cancellation_keeps_what_was_already_imported(self):
        cancel = threading.Event()
        seen = []

        def progress(done, imported, existing, failed):
            seen.append(done)
            cancel.set()

        result = mbox.import_mbox(self.archive, self.box, progress=progress,
                                  cancel_event=cancel)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['imported'], 1)
        self.assertEqual(seen, [1])
        self.assertEqual(self.archive.stats()['messages'], 1)


if __name__ == '__main__':
    unittest.main()


class ExportTests(MboxCase):
    def setUp(self):
        super().setUp()
        self.hashes = []
        for index, body in enumerate((
                b'From: a@example.invalid\r\nSubject: First\r\nDate: Tue, 14 Mar 2023 09:15:00 +0000\r\n\r\nPlain body about invoices',
                b'From: b@example.invalid\r\nSubject: Second\r\nDate: Wed, 15 Mar 2023 10:00:00 +0000\r\n\r\nFrom the beginning of this line',
        )):
            self.hashes.append(self.archive.ingest(body))

    def test_export_writes_stored_bytes_verbatim_then_reimports(self):
        out = self.root / 'out.mbox'
        result = mbox.export_mbox(self.archive, out, hashes=[self.hashes[0]])
        self.assertEqual(result['messages'], 1)
        self.assertEqual(result['quoted_lines'], 0)
        stored = (self.archive.raw / f'{self.hashes[0]}.eml').read_bytes()
        payload = out.read_bytes()
        self.assertTrue(payload.startswith(b'From '))
        self.assertIn(stored, payload, 'stored bytes must appear verbatim')

        second = Archive(self.root / 'second')
        try:
            imported = mbox.import_mbox(second, out)
            self.assertEqual(imported['imported'], 1)
            self.assertEqual(second.stats()['messages'], 1)
            row = second.db.execute(
                'SELECT subject, sender FROM messages').fetchone()
            self.assertEqual(row[0], 'First')
            self.assertEqual(row[1], 'a@example.invalid')
        finally:
            second.db.close()

    def test_body_line_starting_with_from_is_quoted(self):
        out = self.root / 'quoted.mbox'
        result = mbox.export_mbox(self.archive, out, hashes=[self.hashes[1]])
        self.assertEqual(result['quoted_lines'], 1)
        self.assertIn(b'>From the beginning', out.read_bytes())

    def test_existing_destination_is_refused(self):
        out = self.root / 'exists.mbox'
        out.write_text('do not clobber')
        with self.assertRaises(mbox.MboxExportError):
            mbox.export_mbox(self.archive, out, hashes=[self.hashes[0]])
        self.assertEqual(out.read_text(), 'do not clobber')

    def test_empty_selection_is_refused(self):
        with self.assertRaises(mbox.MboxExportError):
            mbox.export_mbox(self.archive, self.root / 'none.mbox',
                             hashes=['0' * 64])

    def test_cancellation_leaves_no_partial_file(self):
        cancel = threading.Event()
        cancel.set()
        out = self.root / 'cancelled.mbox'
        result = mbox.export_mbox(self.archive, out, cancel_event=cancel)
        self.assertTrue(result['cancelled'])
        self.assertFalse(out.exists())
        self.assertFalse((self.root / 'cancelled.mbox.partial').exists())

    def test_account_selection_exports_only_that_source(self):
        mbox.import_mbox(self.archive, self.box)
        account = mbox.source_account(self.box)[0]
        out = self.root / 'account.mbox'
        result = mbox.export_mbox(self.archive, out, account=account)
        self.assertEqual(result['messages'], 2)
