# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import unittest,tempfile, json, sqlite3, io, contextlib
from unittest import mock
from pathlib import Path
from hb_engine.dispatch import Archive, parse_mailbox_list, __version__, main
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
        with mock.patch('hb_engine.ipc.IPCServer.serve'):
            self.assertEqual(main(['--archive', str(root), 'serve']), 0)
        self.assertFalse((root / 'archive.sqlite3').exists())
        self.assertFalse(root.exists())

    def test_sync_missing_uidvalidity(self):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[b'1']); m.response.return_value=('UIDVALIDITY',[None])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertRaises(RuntimeError,self.a.sync,'host','user','pw')
    def test_sync_folder_with_spaces_and_legacy_origin(self):
        account='host|user'
        self.a.ingest(RAW,(account,'"Sent Items"','1','2'))
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[b'2'])
        m.response.return_value=('UIDVALIDITY',[b'1'])
        m.uid.side_effect=[('OK',[b'2 3']),('OK',[(b'3',RAW+b'New message')]),('OK',[b'2 3'])]
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertEqual(self.a.sync('host','user','pw','Sent Items'),1)
            m.select.assert_called_with('"Sent Items"',readonly=True)
            self.assertEqual(self.a.sync('host','user','pw','"Sent Items"'),0)
        self.assertEqual(m.uid.call_count,3)
        self.assertEqual(self.a.stats(),{'messages':2,'origins':2})
        self.assertIsNotNone(self.a.db.execute(
            "SELECT 1 FROM origins WHERE folder=? AND uid=?",('Sent Items','3')).fetchone())
    def test_sync_folder_rejects_control_characters(self):
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL') as connect:
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
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
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
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw')
            self.a.sync('host','user','pw')
        searches=[call.args[2] for call in m.uid.call_args_list
                  if call.args[:2]==('search',None)]
        self.assertEqual(searches,['ALL','ALL'])
        self.assertEqual(self.a.stats()['messages'],2)

    def test_a_uid_at_or_below_the_watermark_is_ignored(self):
        m=self.server([('OK',[b'1 2']),('OK',[(b'1',RAW)]),('OK',[(b'2',RAW+b'Two')]),
                       ('OK',[b'1 2'])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
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
                with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
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
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
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
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
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


class _StubDatabase:
    def close(self):
        return None


class PresetCliTests(unittest.TestCase):
    def test_imap_presets_needs_no_archive_and_creates_nothing(self):
        import tempfile
        from hb_engine import dispatch as dispatch_module
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'missing-archive'
            with mock.patch('sys.stdout'):
                code = dispatch_module.main(['--archive', str(root), 'imap-presets'])
            self.assertEqual(code, 0)
            self.assertFalse(root.exists())


class StubArchive:
    """Stands in for Archive in CLI tests so no database handle is opened."""
    report = None

    def __init__(self, report):
        StubArchive.report = report

    def __call__(self, root):
        return self

    @property
    def db(self):
        return _StubDatabase()

    def sync_mailboxes(self, host, user, password):
        return StubArchive.report


class DiscoveryQuotingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a=Archive(Path(self.tmp.name)/'archive')
    def tearDown(self):
        self.a.db.close()

    def test_a_mailbox_name_containing_quotes_survives_the_round_trip(self):
        seen=[]
        def fake_sync(host,user,password,folder):
            seen.append(folder); return 0
        with mock.patch.object(Archive,'sync',staticmethod(fake_sync)):
            self.a.sync_mailboxes('host','user','pw',folders=['"Projects"'])
        self.assertEqual(seen,['""Projects""'])

    def test_quoted_wrapping_strips_to_the_raw_name_inside_sync(self):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[b'1'])
        m.response.return_value=('UIDVALIDITY',[b'1'])
        m.uid.return_value=('OK',[b''])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw','""Projects""')
        m.select.assert_called_once_with('"\\"Projects\\""',readonly=True)

    def test_all_folders_returns_a_failure_exit_when_any_folder_failed(self):
        from hb_engine import dispatch as dispatch_module
        report={'folders':{'INBOX':{'error':'boom'}},'imported':0,'failed':1}
        stub=StubArchive(report)
        with mock.patch.object(dispatch_module,'Archive',stub), \
             mock.patch('getpass.getpass',return_value='pw'):
            code=dispatch_module.main(['--archive',str(self.a.root),'sync','host',
                                       'user','--all-folders'])
        self.assertEqual(code,1)

    def test_all_folders_returns_success_when_every_folder_succeeded(self):
        from hb_engine import dispatch as dispatch_module
        report={'folders':{'INBOX':{'imported':2}},'imported':2,'failed':0}
        stub=StubArchive(report)
        with mock.patch.object(dispatch_module,'Archive',stub), \
             mock.patch('getpass.getpass',return_value='pw'):
            code=dispatch_module.main(['--archive',str(self.a.root),'sync','host',
                                       'user','--all-folders'])
        self.assertEqual(code,0)


class FolderStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a=Archive(Path(self.tmp.name)/'archive')
    def tearDown(self):
        self.a.db.close()

    def server(self, uids, exists=b'2', validity=b'1'):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[exists])
        m.response.return_value=('UIDVALIDITY',[validity])
        m.uid.side_effect=uids
        return m

    def state(self, folder='INBOX'):
        return self.a.db.execute(
            'SELECT uidvalidity, last_seen_uid, remote_message_count, '
            'local_message_count, status FROM imap_folder_state '
            'WHERE folder_name=?',(folder,)).fetchone()

    def test_initial_sync_records_state(self):
        m=self.server([('OK',[b'1 2']),('OK',[(b'1',RAW)]),('OK',[(b'2',RAW+b'Two')])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.assertEqual(self.a.sync('host','user','pw'),2)
        validity,last,remote,local,status=self.state()
        self.assertEqual((validity,last,remote,local,status),('1',2,2,2,'ok'))

    def test_second_run_uses_the_stored_watermark(self):
        m=self.server([('OK',[b'1']),('OK',[(b'1',RAW)]),
                       ('OK',[b'2']),('OK',[(b'2',RAW+b'Two')])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw'); self.a.sync('host','user','pw')
        searches=[c.args[2] for c in m.uid.call_args_list if c.args[:2]==('search',None)]
        self.assertEqual(searches,['ALL','UID 2:*'])
        self.assertEqual(self.state()[1],2)

    def test_uidvalidity_change_warns_and_rescans(self):
        m=self.server([('OK',[b'1']),('OK',[(b'1',RAW)]),
                       ('OK',[b'1']),('OK',[(b'1',RAW+b'Rewritten')])],
                      validity=b'5')
        m.response.side_effect=[('UIDVALIDITY',[b'1']),('UIDVALIDITY',[b'5'])]
        with self.assertLogs('hb_engine.dispatch',level='WARNING'):
            with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
                self.a.sync('host','user','pw'); self.a.sync('host','user','pw')
        searches=[c.args[2] for c in m.uid.call_args_list if c.args[:2]==('search',None)]
        self.assertEqual(searches,['ALL','ALL'])
        self.assertEqual(self.state()[0],'5')

    def test_interrupted_sync_marks_error_and_resumes(self):
        m=self.server([('OK',[b'1 2']),('OK',[(b'1',RAW)]),('NO',[])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            with self.assertRaises(RuntimeError):
                self.a.sync('host','user','pw')
        self.assertEqual(self.state()[4],'error')
        self.assertEqual(self.state()[1],1)
        resume=self.server([('OK',[b'2']),('OK',[(b'2',RAW+b'Two')])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=resume):
            self.assertEqual(self.a.sync('host','user','pw'),1)
        self.assertEqual(self.state()[1],2)
        self.assertEqual(self.state()[4],'ok')

    def test_health_reports_folder_coverage(self):
        m=self.server([('OK',[b'1']),('OK',[(b'1',RAW)])],exists=b'5')
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw')
        coverage=self.a.health()['folder_coverage']
        self.assertTrue(coverage['available'])
        self.assertEqual(coverage['incomplete'],1)
        folder=coverage['folders'][0]
        self.assertEqual(folder['remote_messages'],5)
        self.assertEqual(folder['local_messages'],1)
        self.assertEqual(folder['missing'],4)
        self.assertFalse(folder['complete'])


class FolderStateHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a=Archive(Path(self.tmp.name)/'archive')
    def tearDown(self):
        self.a.db.close()

    def server(self, uids, exists=b'2', validity=b'1'):
        m=mock.MagicMock(); m.__enter__.return_value=m
        m.select.return_value=('OK',[exists])
        m.response.return_value=('UIDVALIDITY',[validity])
        m.uid.side_effect=uids
        return m

    def test_removing_a_source_clears_its_folder_state(self):
        m=self.server([('OK',[b'1']),('OK',[(b'1',RAW)])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw')
        self.a.source_remove('email','host|user',confirm=True)
        rows=self.a.db.execute('SELECT count(*) FROM imap_folder_state').fetchone()[0]
        self.assertEqual(rows,0)

    def test_local_count_ignores_previous_uidvalidity_epochs(self):
        self.a.ingest(RAW,('host|user','INBOX','1','1'))
        self.a.ingest(RAW+b'old',('host|user','INBOX','1','2'))
        m=self.server([('OK',[b'5']),('OK',[(b'5',RAW+b'new')])],exists=b'5',validity=b'2')
        m.response.side_effect=[('UIDVALIDITY',[b'2'])]
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            self.a.sync('host','user','pw')
        remote,local=self.a.db.execute(
            'SELECT remote_message_count, local_message_count FROM imap_folder_state'
        ).fetchone()
        self.assertEqual((remote,local),(5,1))
        coverage=self.a.health()['folder_coverage']
        self.assertFalse(coverage['folders'][0]['complete'])
        self.assertEqual(coverage['folders'][0]['missing'],4)

    def test_keyboard_interrupt_is_recorded_as_an_error_checkpoint(self):
        m=self.server([('OK',[b'1 2']),('OK',[(b'1',RAW)]),('OK',[(b'2',RAW+b'Two')])])
        with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
            with mock.patch.object(Archive,'_ingest',side_effect=[None,KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt):
                    self.a.sync('host','user','pw')
        status,seen=self.a.db.execute(
            'SELECT status, last_seen_uid FROM imap_folder_state').fetchone()
        self.assertEqual((status,seen),('error',1))
