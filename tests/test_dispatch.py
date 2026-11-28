# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import unittest,tempfile, json, sqlite3, io, contextlib
from unittest import mock
from pathlib import Path
from dispatch import Archive, parse_mailbox_list, __version__, main
RAW=b'From: sender@example.invalid\r\nSubject: Synthetic invoice\r\nMessage-ID: <test@example.invalid>\r\n\r\nTest evidence only.\r\n'
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.a=Archive(Path(self.tmp.name)/'archive')
    def tearDown(self): self.a.db.close(); self.tmp.cleanup()
    def test_roundtrip(self):
        h=self.a.ingest(RAW,('test','INBOX','1','2')); self.assertEqual(h,self.a.ingest(RAW)); self.assertEqual(len(self.a.find('invoice')),1); self.assertEqual(self.a.verify(),[])
        dest=Path(self.tmp.name)/'export'; self.a.export([h],dest); self.assertEqual((dest/(h+'.eml')).read_bytes(),RAW); self.assertEqual(json.loads((dest/'manifest.json').read_text())[0]['origins'][0],['test','INBOX','1','2'])
    def test_tamper(self):
        h=self.a.ingest(RAW); (self.a.raw/(h+'.eml')).write_bytes(b'changed'); self.assertEqual(self.a.verify(),[h]); self.assertRaises(ValueError,self.a.ingest,RAW)
    def test_origin_conflict(self):
        origin=('test','INBOX','1','2'); self.a.ingest(RAW,origin); self.assertRaises(ValueError,self.a.ingest,RAW+b'changed',origin); self.assertEqual(len(self.a.find('invoice')),1)
    def test_search_bound(self):
        self.a.ingest(RAW); self.assertEqual(self.a.find('nonexistent'),[]); self.assertEqual(len(self.a.find('evidence')),1)
    def test_hostile_charset(self):
        self.a.ingest(b'Content-Type: text/plain; charset=x-does-not-exist\r\n\r\ncaf\xe9 bill\r\n'); self.assertEqual(len(self.a.find('bill')),1); self.assertEqual(self.a.verify(),[])
    def test_fts_garbage(self):
        self.a.ingest(RAW)
        for q in ('a:b','-invoice','"unterminated','OR','*'): self.assertIsInstance(self.a.find(q),list)
        self.assertEqual(len(self.a.find('invoice NOT nonexistent')),1); self.assertEqual(len(self.a.find('invo*')),1)
    def test_serve_missing_archive_does_not_create_one(self):
        root = Path(self.tmp.name) / 'missing'
        with mock.patch('ipc.IPCServer.serve'):
            self.assertEqual(main(['--archive', str(root), 'serve']), 0)
        self.assertFalse((root / 'archive.sqlite3').exists())
        self.assertFalse(root.exists())

    def test_sync_missing_uidvalidity(self):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[b'1']); m.response.return_value=('UIDVALIDITY',[None])
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertRaises(RuntimeError,self.a.sync,'host','user','pw')
    def test_sync_folder_with_spaces_and_legacy_origin(self):
        account='host|user'
        self.a.ingest(RAW,(account,'"Sent Items"','1','2'))
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[b'2'])
        m.response.return_value=('UIDVALIDITY',[b'1'])
        m.uid.side_effect=[('OK',[b'2 3']),('OK',[(b'3',RAW+b'New message')]),('OK',[b'2 3'])]
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertEqual(self.a.sync('host','user','pw','Sent Items'),1)
            m.select.assert_called_with('"Sent Items"',readonly=True)
            self.assertEqual(self.a.sync('host','user','pw','"Sent Items"'),0)
        self.assertEqual(m.uid.call_count,3)
        self.assertEqual(self.a.stats(),{'messages':2,'origins':2})
        self.assertIsNotNone(self.a.db.execute(
            "SELECT 1 FROM origins WHERE folder=? AND uid=?",('Sent Items','3')).fetchone())
    def test_sync_folder_rejects_control_characters(self):
        with mock.patch('dispatch.imaplib.IMAP4_SSL') as connect:
            for folder in ('', 'bad\rname', 'bad\nname', 'bad\x00name'):
                with self.assertRaises(ValueError):
                    self.a.sync('host','user','pw',folder)
            connect.assert_not_called()
    def test_foreign_keys_enforced(self):
        self.assertRaises(sqlite3.IntegrityError,self.a.db.execute,'INSERT INTO origins VALUES(?,?,?,?,?)',('a','f','1','2','0'*64))
    def test_failed_export_cleanup(self):
        h=self.a.ingest(RAW)
        dest=Path(self.tmp.name)/'failed-export'
        (self.a.raw/(h+'.eml')).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.a.export([h],dest)
        self.assertFalse(dest.exists())
    def test_unknown_export_does_not_create_destination(self):
        dest=Path(self.tmp.name)/'unknown-export'
        with self.assertRaises(ValueError):
            self.a.export(['0'*64],dest)
        self.assertFalse(dest.exists())
    def test_workspace_queries(self):
        h=self.a.ingest(RAW,('test','INBOX','1','2'))
        self.assertEqual(self.a.stats(),{'messages':1,'origins':1})
        self.assertEqual(self.a.recent()[0][0],h)
        self.assertEqual(self.a.details(h)['subject'],'Synthetic invoice')
    def test_existing_export_is_not_modified(self):
        h=self.a.ingest(RAW)
        dest=Path(self.tmp.name)/'existing'
        dest.mkdir()
        sentinel=dest/'keep.txt'
        sentinel.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.a.export([h],dest)
        self.assertEqual(sentinel.read_text(),'keep')
    def test_thread_reconstruction(self):
        parent=self.a.ingest(RAW)
        reply=self.a.ingest(b'From: other@example.invalid\r\nMessage-ID: <reply@example.invalid>\r\nIn-Reply-To: <test@example.invalid>\r\n\r\nReply.\r\n')
        unrelated=self.a.ingest(b'Subject: Synthetic invoice\r\n\r\nUnrelated.\r\n')
        for h in (parent,reply):
            self.assertEqual({m['sha256'] for m in self.a.thread(h)},{parent,reply})
        self.assertEqual(len(self.a.thread(unrelated)),1)
        self.assertEqual(dict(self.a.correspondents())['other@example.invalid'],1)
    def test_version_flag(self):
        out=io.StringIO()
        with mock.patch('sys.argv',['dispatch.py','--version']), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as exit:
                main()
        self.assertEqual(exit.exception.code,0)
        self.assertEqual(out.getvalue().strip(),f'dispatch.py {__version__}')
if __name__=='__main__': unittest.main()


class CapabilityReportTests(unittest.TestCase):
    def test_capabilities_reports_each_optional_dependency(self):
        from hb_engine.dispatch import capabilities
        report = capabilities()
        for name in ('sqlcipher3', 'pypdf', 'docx', 'keyring', 'cryptography'):
            entry = report['optional_dependencies'][name]
            self.assertIn('available', entry)
            self.assertIn('version', entry)
        self.assertIsInstance(report['encryption'], bool)
        self.assertIsInstance(report['document_extraction'], bool)
        self.assertIsInstance(report['os_credential_store'], bool)


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a=Archive(Path(self.tmp.name)/'archive')
    def tearDown(self):
        self.a.db.close()

    def server(self, uids, validity=b'1'):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[b'1'])
        m.response.return_value=('UIDVALIDITY',[validity])
        m.uid.side_effect=uids
        return m

    def test_second_run_asks_the_server_for_newer_uids_only(self):
        m=self.server([('OK',[b'1 2']),
                       ('OK',[(b'1',RAW)]),('OK',[(b'2',RAW+b'Two')]),
                       ('OK',[b'3']),
                       ('OK',[(b'3',RAW+b'Three')])])
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertEqual(self.a.sync('host','user','pw'),2)
            self.assertEqual(self.a.sync('host','user','pw'),1)
        searches=[call.args[2] for call in m.uid.call_args_list
                  if call.args[:2]==('search',None)]
        self.assertEqual(searches,['ALL','UID 3:*'])
        self.assertEqual(self.a.stats()['messages'],3)

    def test_uidvalidity_change_forces_a_full_search(self):
        m=self.server([('OK',[b'1']),('OK',[(b'1',RAW)]),
                       ('OK',[b'1']),('OK',[(b'1',RAW+b'Rewritten')])],
                      validity=b'9')
        m.response.side_effect=[('UIDVALIDITY',[b'1']),('UIDVALIDITY',[b'9'])]
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw')
            self.a.sync('host','user','pw')
        searches=[call.args[2] for call in m.uid.call_args_list
                  if call.args[:2]==('search',None)]
        self.assertEqual(searches,['ALL','ALL'])
        self.assertEqual(self.a.stats()['messages'],2)

    def test_a_uid_at_or_below_the_watermark_is_ignored(self):
        m=self.server([('OK',[b'1 2']),('OK',[(b'1',RAW)]),('OK',[(b'2',RAW+b'Two')]),
                       ('OK',[b'1 2'])])
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw')
            self.assertEqual(self.a.sync('host','user','pw'),0)
        self.assertEqual(self.a.stats()['messages'],2)
        self.assertEqual(m.uid.call_count,4)


class SyncWatermarkBoundaryTests(unittest.TestCase):
    def test_highest_possible_uid_is_not_incremented_past_the_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            ar=Archive(Path(tmp)/'archive')
            try:
                ar.ingest(RAW,('host|user','INBOX','1','4294967295'))
                m=mock.MagicMock(); m.__enter__.return_value=m
                m.select.return_value=('OK',[b'1'])
                m.response.return_value=('UIDVALIDITY',[b'1'])
                m.uid.return_value=('OK',[b'4294967295'])
                with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
                    self.assertEqual(ar.sync('host','user','pw'),0)
                m.uid.assert_called_once_with('search',None,'UID 4294967295:*')
            finally:
                ar.db.close()


class MailboxDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a=Archive(Path(self.tmp.name)/'archive')
    def tearDown(self):
        self.a.db.close()

    def test_mailboxes_skips_noselect_entries(self):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.list.return_value=('OK',[b'(\\HasNoChildren) "/" "INBOX"',
                                   b'(\\Noselect) "/" "[Gmail]"',
                                   b'(\\HasNoChildren) "/" "Sent Items"'])
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertEqual(self.a.mailboxes('host','user','pw'),
                             ['INBOX','Sent Items'])

    def test_sync_mailboxes_reports_each_folder_and_continues_after_a_failure(self):
        discovered=self.a.mailboxes
        with mock.patch.object(Archive,'mailboxes',staticmethod(lambda *a: ['INBOX','Broken'])), \
             mock.patch.object(Archive,'sync',autospec=True) as sync:
            sync.side_effect=[3, RuntimeError('boom')]
            result=self.a.sync_mailboxes('host','user','pw')
        self.assertEqual(result['folders']['INBOX'],{'imported':3})
        self.assertIn('boom',result['folders']['Broken']['error'])
        self.assertEqual(result['imported'],3)
        self.assertEqual(result['failed'],1)
        self.assertEqual(sync.call_count,2)

    def test_gmail_password_spaces_are_removed_before_listing(self):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.list.return_value=('OK',[b'(\\HasNoChildren) "/" "INBOX"'])
        with mock.patch('dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.mailboxes('imap.gmail.com','user','abcd efgh ijkl mnop')
        m.login.assert_called_once_with('user','abcdefghijklmnop')

    def test_literal_mailbox_names_are_unpacked(self):
        line=(b'(\\HasNoChildren) "/" {10}',b'Sent Items')
        self.assertEqual(parse_mailbox_list([line]),['Sent Items'])

    def test_quoted_names_unescape_backslashes(self):
        line=b'(\\HasNoChildren) "/" "Projects\\\\2026"'
        self.assertEqual(parse_mailbox_list([line]),['Projects\\2026'])

    def test_explicit_empty_selection_does_not_sync_everything(self):
        with mock.patch.object(Archive,'mailboxes',autospec=True) as discover, \
             mock.patch.object(Archive,'sync',autospec=True) as sync:
            with self.assertRaises(ValueError):
                self.a.sync_mailboxes('host','user','pw',folders=[])
        discover.assert_not_called()
        sync.assert_not_called()

    def test_sync_mailboxes_rejects_an_empty_server_listing(self):
        with mock.patch.object(Archive,'mailboxes',staticmethod(lambda *a: [])):
            with self.assertRaises(ValueError):
                self.a.sync_mailboxes('host','user','pw')
