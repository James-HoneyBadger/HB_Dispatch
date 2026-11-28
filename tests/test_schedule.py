# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from hb_engine.dispatch import Archive
from hb_engine import db, encryption
from hb_engine import schedule
from hb_engine.schedule import profile,due,tick,write_json
import hb_engine.schedule
from hb_engine.sync_lock import LOCK_SUPPORTED, sync_lock

class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.now=datetime(2026,11,26,12,0,tzinfo=timezone.utc)
        self.p=profile('mail','imap.gmail.com','user@example.invalid',['[Gmail]/All Mail'],minutes=30)
    def tearDown(self): self.tmp.cleanup()
    def test_interval(self):
        self.assertTrue(due(self.p,{},self.now))
        state={'last_attempt':self.now.isoformat()}
        self.assertFalse(due(self.p,state,self.now+timedelta(minutes=29)))
        self.assertTrue(due(self.p,state,self.now+timedelta(minutes=30)))
        self.assertFalse(due(dict(self.p,enabled=False),{},self.now))
    def test_daily(self):
        p=profile('mail','imap.example.invalid','user',['INBOX'],daily='12:00')
        local=datetime.now().astimezone().replace(hour=12,minute=0,second=0,microsecond=0)
        self.assertFalse(due(p,{},local-timedelta(minutes=1)))
        self.assertTrue(due(p,{},local))
        self.assertFalse(due(p,{'last_attempt':local.isoformat()},local+timedelta(minutes=1)))
    def test_validation(self):
        for kw in ({'minutes':1},{'daily':'25:00'},{'minutes':30,'daily':'12:00'},{}):
            with self.assertRaises(ValueError):profile('a','host','user',['INBOX'],**kw)
    def test_run_and_skip(self):
        write_json(self.root/'schedule.json',{'mail':self.p})
        with mock.patch.object(Archive,'sync',return_value=7) as s:
            result=tick(self.root,now=self.now,get_password=lambda p:'fake-secret')
            self.assertEqual(result['mail']['imported'],7)
            self.assertEqual(result['mail']['status'],'ok')
            s.assert_called_once_with('imap.gmail.com','user@example.invalid','fake-secret','[Gmail]/All Mail')
            tick(self.root,now=self.now,get_password=lambda p:'fake-secret')
            self.assertEqual(s.call_count,1)
        self.assertNotIn('fake-secret',(self.root/'schedule-status.json').read_text())
    def test_failure_isolated(self):
        write_json(self.root/'schedule.json',{'mail':self.p,'other':dict(self.p,name='other',user='other')})
        def pw(p):
            if p['name']=='mail': raise RuntimeError('secret should not be logged')
            return 'synthetic'
        with mock.patch.object(Archive,'sync',return_value=1):
            r=tick(self.root,now=self.now,get_password=pw)
        self.assertEqual(r['mail']['status'],'error: RuntimeError')
        self.assertEqual(r['other']['status'],'ok')
        self.assertNotIn('secret should not be logged',(self.root/'schedule-status.json').read_text())
    def test_busy_defers_without_consuming_schedule(self):
        if not LOCK_SUPPORTED:
            self.skipTest('archive locking is unsupported here')
        write_json(self.root/'schedule.json',{'mail':self.p})
        with sync_lock(self.root):
            result=tick(self.root,now=self.now,get_password=lambda p:'fake')
        self.assertEqual(result['mail']['status'],'deferred: archive busy')
        self.assertNotIn('last_attempt',result['mail'])

    def test_disabled_source_is_skipped_without_an_attempt(self):
        write_json(self.root/'schedule.json',{'mail':self.p})
        archive=Archive(self.root)
        try: archive.source_set_enabled('email','imap.gmail.com|user@example.invalid',False)
        finally: archive.db.close()
        with mock.patch.object(Archive,'sync',return_value=1) as sync:
            result=tick(self.root,now=self.now,get_password=lambda p:'fake')
        sync.assert_not_called()
        self.assertEqual(result['mail']['status'],'disabled')
        self.assertNotIn('last_attempt',result['mail'])

    def test_locked_encrypted_archive_defers_without_credentials(self):
        if not db.encryption_available():
            self.skipTest('optional sqlcipher3 binding is not installed')
        encryption.create(self.root, 'synthetic schedule password')
        encryption.lock(self.root)
        write_json(self.root/'schedule.json',{'mail':self.p})
        called=[]
        result=tick(self.root,now=self.now,
                    get_password=lambda p: called.append(p) or 'should-not-be-used')
        self.assertEqual(result['mail']['status'],'deferred: encrypted archive locked')
        self.assertEqual(called,[])
        self.assertNotIn('should-not-be-used',
                         (self.root/'schedule-status.json').read_text())

    def test_tick_without_flock_runs_unescorted(self):
        write_json(self.root/'schedule.json',{'mail':self.p})
        with mock.patch.object(schedule,'HAS_FLOCK',False), \
             mock.patch.object(Archive,'sync',return_value=2) as sync:
            result=tick(self.root,now=self.now,get_password=lambda p:'fake')
        self.assertEqual(result['mail']['status'],'ok')
        self.assertEqual(result['mail']['imported'],2)
        sync.assert_called_once()
    def test_legacy_gmail_folder(self):
        ar=Archive(self.root)
        try:
            ar.ingest(b'Subject: test\r\n\r\nbody',('imap.gmail.com|user','"[Gmail]/All Mail"','7','42'))
            m=mock.MagicMock();m.__enter__.return_value=m
            m.select.return_value=('OK',[b'1']);m.response.return_value=('UIDVALIDITY',[b'7'])
            m.uid.return_value=('OK',[b'42'])
            with mock.patch('hb_engine.dispatch.imaplib.IMAP4_SSL',return_value=m):
                self.assertEqual(ar.sync('imap.gmail.com','user','abcd efgh ijkl mnop','[Gmail]/All Mail'),0)
            m.login.assert_called_once_with('user','abcdefghijklmnop')
            m.select.assert_called_once_with('"[Gmail]/All Mail"',readonly=True)
            # The legacy origin carries UID 42, so the run asks only for newer UIDs.
            m.uid.assert_called_once_with('search',None,'UID 43:*')
        finally:ar.db.close()
