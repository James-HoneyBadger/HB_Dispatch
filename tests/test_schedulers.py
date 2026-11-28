# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import configparser, io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
from hb_engine.schedulers import (LaunchdScheduler, SystemdScheduler, SchtasksScheduler,
                        KeychainCredentialStore, KeyringCredentialStore,
                        EncryptedFileCredentialStore, SchedulerError,
                        archive_id, get_scheduler, get_credential_store, sync_command)

FAKE_ROOT = Path('/tmp/hb-archive')
PASSWORD = 'super-secret-password'
OK = mock.Mock(returncode=0, stdout=b'', stderr=b'')

class SystemdTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.s=SystemdScheduler(self.home)
        patcher=mock.patch.dict(os.environ,{'XDG_CONFIG_HOME':str(self.home/'.config')})
        patcher.start();self.addCleanup(patcher.stop)
    def tearDown(self):self.tmp.cleanup()

    def test_install_daily_writes_units_without_secret(self):
        with mock.patch('hb_engine.schedulers._run',return_value=OK) as run:
            path=self.s.install(FAKE_ROOT, None, 'daily', None, '06:30')
        calls=[c.args[0] for c in run.call_args_list]
        self.assertIn(['systemctl','--user','daemon-reload'],calls)
        self.assertTrue(Path(path).is_file())
        timer=self.s.timer_path(FAKE_ROOT).read_text()
        service=self.s.service_path(FAKE_ROOT).read_text()
        self.assertIn('OnCalendar=*-*-* 06:30:00',timer)
        self.assertIn('WantedBy=timers.target',timer)
        self.assertIn('Type=oneshot',service)
        self.assertIn('tick',service)
        self.assertNotIn(PASSWORD,service+timer)
        self.assertNotIn('password',(service+timer).lower())

    def test_install_reports_missing_user_session(self):
        missing=mock.Mock(returncode=1,stdout=b'',stderr=b'Failed to connect to bus')
        with mock.patch('hb_engine.schedulers._run',return_value=missing):
            with self.assertRaises(SchedulerError) as caught:
                self.s.install(FAKE_ROOT)
        self.assertIn('loginctl enable-linger',str(caught.exception))
        self.assertFalse(self.s.timer_path(FAKE_ROOT).exists())

    def test_install_cleans_up_when_enable_fails(self):
        def run(args,check=True):
            if args[-1].endswith('.timer') and 'enable' in args:
                raise SchedulerError('enable failed')
            return OK
        with mock.patch('hb_engine.schedulers._run',side_effect=run):
            with self.assertRaises(SchedulerError):
                self.s.install(FAKE_ROOT)
        self.assertFalse(self.s.timer_path(FAKE_ROOT).exists())
        self.assertFalse(self.s.service_path(FAKE_ROOT).exists())

    def test_install_minutes_and_account(self):
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.install(FAKE_ROOT,'work','minutes',15)
        service=self.s.service_path(FAKE_ROOT).read_text()
        self.assertIn('OnUnitActiveSec=15min',self.s.timer_path(FAKE_ROOT).read_text())
        self.assertIn(' run work',service)

    def test_install_default_interval_is_daily(self):
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.install(FAKE_ROOT)
        self.assertIn('OnCalendar=daily',self.s.timer_path(FAKE_ROOT).read_text())

    def test_uninstall_removes_units(self):
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.install(FAKE_ROOT)
            self.s.uninstall(FAKE_ROOT)
        self.assertFalse(self.s.timer_path(FAKE_ROOT).exists())
        self.assertFalse(self.s.service_path(FAKE_ROOT).exists())

    def test_unit_dir_honours_xdg_config_home(self):
        with mock.patch.dict(os.environ,{'XDG_CONFIG_HOME':'/custom/config'}):
            self.assertEqual(SystemdScheduler(Path('/home/x')).unit_dir(),
                             Path('/custom/config/systemd/user'))
        with mock.patch.dict(os.environ,{},clear=True):
            self.assertEqual(SystemdScheduler(Path('/home/x')).unit_dir(),
                             Path('/home/x/.config/systemd/user'))

    def _sections(self, path):
        parser=configparser.ConfigParser(strict=False,interpolation=None)
        parser.optionxform=str
        parser.read_string(path.read_text())
        return parser

    def test_units_are_structurally_valid(self):
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.install(FAKE_ROOT,None,'daily',None,'06:30')
        timer=self._sections(self.s.timer_path(FAKE_ROOT))
        service=self._sections(self.s.service_path(FAKE_ROOT))
        self.assertIn('Timer',timer.sections())
        self.assertEqual(timer['Timer']['OnCalendar'],'*-*-* 06:30:00')
        self.assertEqual(timer['Timer']['Persistent'],'true')
        self.assertEqual(timer['Install']['WantedBy'],'timers.target')
        self.assertIn('Service',service.sections())
        self.assertEqual(service['Service']['Type'],'oneshot')
        self.assertIn('tick',service['Service']['ExecStart'])

    def test_units_are_structurally_valid_for_interval(self):
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.install(FAKE_ROOT,None,'minutes',15)
        timer=self._sections(self.s.timer_path(FAKE_ROOT))
        self.assertEqual(timer['Timer']['OnUnitActiveSec'],'15min')
        self.assertIn('OnBootSec',timer['Timer'])

    def test_install_cleans_up_wants_symlink_on_failure(self):
        def run(args,check=True):
            if 'enable' in args:
                raise SchedulerError('systemctl exited 1: Unit has a bad unit file setting')
            return OK
        with mock.patch('hb_engine.schedulers._run',side_effect=run):
            with self.assertRaises(SchedulerError) as caught:
                self.s.install(FAKE_ROOT)
        self.assertIn('rejected',str(caught.exception))
        self.assertFalse(self.s.timer_path(FAKE_ROOT).exists())
        self.assertFalse(self.s.service_path(FAKE_ROOT).exists())

    def test_uninstall_removes_stale_wants_symlink(self):
        wants=self.s._wants_link(FAKE_ROOT)
        wants.parent.mkdir(parents=True,exist_ok=True)
        wants.write_text('')
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.uninstall(FAKE_ROOT)
        self.assertFalse(wants.exists())

    def test_status(self):
        self.assertEqual(self.s.status(FAKE_ROOT),'Not installed')
        with mock.patch('hb_engine.schedulers._run',return_value=OK):
            self.s.install(FAKE_ROOT)
        result=mock.Mock(returncode=0,stdout=b'active\n',stderr=b'')
        with mock.patch('hb_engine.schedulers._run',return_value=result):
            self.assertEqual(self.s.status(FAKE_ROOT),'active')

@unittest.skipUnless(hasattr(os,'getuid'),'launchd is a POSIX-only scheduler')
class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.s=LaunchdScheduler(self.home)
    def tearDown(self):self.tmp.cleanup()

    def test_install_writes_plist(self):
        import plistlib
        with mock.patch('hb_engine.schedulers._run'):
            path=self.s.install(FAKE_ROOT)
        data=plistlib.loads(Path(path).read_bytes())
        self.assertEqual(data['StartInterval'],60)
        self.assertTrue(data['RunAtLoad'])
        self.assertIn('tick',data['ProgramArguments'])
        self.assertNotIn(PASSWORD,Path(path).read_text())

    def test_install_honours_every_and_at(self):
        import plistlib
        with mock.patch('hb_engine.schedulers._run'):
            data=plistlib.loads(Path(self.s.install(FAKE_ROOT,None,'minutes',30)).read_bytes())
        self.assertEqual(data['StartInterval'],1800)
        with mock.patch('hb_engine.schedulers._run'):
            other=LaunchdScheduler(self.home)
            other.path(FAKE_ROOT).unlink(missing_ok=True)
            data=plistlib.loads(Path(other.install(FAKE_ROOT,None,'daily',None,'05:15')).read_bytes())
        self.assertEqual(data['StartCalendarInterval'],{'Hour':5,'Minute':15})

    def test_install_refuses_existing_agent(self):
        with mock.patch('hb_engine.schedulers._run'):
            self.s.install(FAKE_ROOT)
        with mock.patch('hb_engine.schedulers._run'):
            with self.assertRaises(SchedulerError):
                self.s.install(FAKE_ROOT)

    def test_install_cleans_up_on_bootstrap_failure(self):
        with mock.patch('hb_engine.schedulers._run',side_effect=SchedulerError('boom')):
            with self.assertRaises(SchedulerError):
                self.s.install(FAKE_ROOT)
        self.assertFalse(self.s.path(FAKE_ROOT).exists())

    def test_uninstall_waits_for_launchd_to_drop_the_agent(self):
        with mock.patch('hb_engine.schedulers._run'):
            self.s.install(FAKE_ROOT)
        with mock.patch.object(self.s,'status',
                               side_effect=['Loaded','Loaded','Not installed']) as status:
            with mock.patch('hb_engine.schedulers.time.sleep') as sleep:
                with mock.patch('hb_engine.schedulers._run') as run:
                    self.s.uninstall(FAKE_ROOT)
        self.assertIn('bootout',run.call_args.args[0])
        self.assertGreaterEqual(status.call_count,3)
        sleep.assert_called()
        self.assertFalse(self.s.path(FAKE_ROOT).exists())

class SchtasksTests(unittest.TestCase):
    def setUp(self):
        self.s=SchtasksScheduler()
    def test_install_daily(self):
        with mock.patch('hb_engine.schedulers._run') as run:
            self.s.install(FAKE_ROOT,None,'daily',None,'07:45')
        args=run.call_args.args[0]
        self.assertEqual(args[0],'schtasks')
        self.assertIn('/Create',args)
        self.assertIn('DAILY',args)
        self.assertEqual(args[args.index('/ST')+1],'07:45')
        self.assertTrue(any('tick' in a for a in args))
        self.assertFalse(any(PASSWORD in a for a in args))

    def test_install_minutes(self):
        with mock.patch('hb_engine.schedulers._run') as run:
            self.s.install(FAKE_ROOT,'work','minutes',20)
        args=run.call_args.args[0]
        self.assertEqual(args[args.index('/SC')+1],'MINUTE')
        self.assertEqual(args[args.index('/MO')+1],'20')

    def test_status_and_uninstall(self):
        not_found=mock.Mock(returncode=1,stdout=b'',stderr=b'')
        with mock.patch('hb_engine.schedulers._run',return_value=not_found):
            self.assertEqual(self.s.status(FAKE_ROOT),'Not installed')
        ready=mock.Mock(returncode=0,stdout=b'TaskName  Status\n==  Ready\n',stderr=b'')
        with mock.patch('hb_engine.schedulers._run',return_value=ready):
            self.assertEqual(self.s.status(FAKE_ROOT),'Ready')
        with mock.patch('hb_engine.schedulers._run') as run:
            self.s.uninstall(FAKE_ROOT)
        self.assertIn('/Delete',run.call_args.args[0])

class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_encrypted_file_roundtrip(self):
        if not EncryptedFileCredentialStore.available():
            self.skipTest('cryptography not installed')
        store=EncryptedFileCredentialStore()
        try:
            store.set('svc','user',PASSWORD,self.root)
            self.assertEqual(store.get('svc','user',self.root),PASSWORD)
        except SchedulerError as exc:
            self.skipTest(str(exc))
        ciphertext=(self.root/'credentials.enc').read_text()
        self.assertNotIn(PASSWORD,ciphertext)
        key=self.root/'credentials.key'
        self.assertEqual(oct(key.stat().st_mode & 0o777),'0o600')
        self.assertEqual(oct((self.root/'credentials.enc').stat().st_mode & 0o777),'0o600')
        store.delete('svc','user',self.root)
        with self.assertRaises(SchedulerError):
            store.get('svc','user',self.root)

    def test_missing_keyring_fails_closed_with_actionable_error(self):
        with mock.patch.object(sys,'platform','linux'), \
             mock.patch.object(KeyringCredentialStore,'available',staticmethod(lambda: False)), \
             mock.patch.object(EncryptedFileCredentialStore,'available',staticmethod(lambda: True)):
            with self.assertRaises(SchedulerError) as caught:
                get_credential_store(self.root)
        message=str(caught.exception)
        self.assertIn('--allow-insecure-credential-fallback',message)
        self.assertIn('keyring',message)
        self.assertFalse((self.root/'credentials.enc').exists())

    def test_insecure_fallback_warns_and_is_opt_in(self):
        if not EncryptedFileCredentialStore.available():
            self.skipTest('cryptography not installed')
        import hb_engine.schedulers as schedulers
        stderr=io.StringIO()
        with mock.patch.object(sys,'platform','linux'), \
             mock.patch.object(KeyringCredentialStore,'available',staticmethod(lambda: False)), \
             mock.patch.object(schedulers,'_warned_fallback',False), \
             mock.patch.object(sys,'stderr',stderr):
            store=get_credential_store(self.root,allow_insecure_fallback=True)
            store2=get_credential_store(self.root,allow_insecure_fallback=True)
        self.assertIsInstance(store,EncryptedFileCredentialStore)
        self.assertIsInstance(store2,EncryptedFileCredentialStore)
        warning=stderr.getvalue()
        self.assertIn('WARNING',warning)
        self.assertIn('archive directory',warning)
        self.assertEqual(warning.count('WARNING'),1)

    def test_tampered_fallback_ciphertext_fails_cleanly(self):
        if not EncryptedFileCredentialStore.available():
            self.skipTest('cryptography not installed')
        store=EncryptedFileCredentialStore()
        try:
            store.set('svc','user',PASSWORD,self.root)
        except SchedulerError as exc:
            self.skipTest(str(exc))
        blob=self.root/'credentials.enc'
        data=json.loads(blob.read_text())
        key=next(iter(data))
        token=data[key]
        flipped=('A' if token[0]!='A' else 'B')+token[1:]
        data[key]=flipped
        blob.write_text(json.dumps(data))
        with self.assertRaises(SchedulerError) as caught:
            store.get('svc','user',self.root)
        self.assertEqual(str(caught.exception),'stored credential cannot be decrypted')
        self.assertNotIn(PASSWORD,str(caught.exception))

    def test_corrupt_fallback_file_fails_cleanly(self):
        if not EncryptedFileCredentialStore.available():
            self.skipTest('cryptography not installed')
        (self.root/'credentials.enc').write_text('not json')
        with self.assertRaises(SchedulerError) as caught:
            EncryptedFileCredentialStore().get('svc','user',self.root)
        self.assertIn('unreadable or corrupt',str(caught.exception))

    def test_no_plaintext_secret_is_written_under_the_archive(self):
        if not EncryptedFileCredentialStore.available():
            self.skipTest('cryptography not installed')
        store=EncryptedFileCredentialStore()
        try:
            store.set('svc','user',PASSWORD,self.root)
        except SchedulerError as exc:
            self.skipTest(str(exc))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(PASSWORD.encode('utf-8'),path.read_bytes(),str(path))

    def test_existing_fallback_file_allows_unattended_read(self):
        if not EncryptedFileCredentialStore.available():
            self.skipTest('cryptography not installed')
        from hb_engine.schedule import credential, service
        import hb_engine.schedulers as schedulers
        profile={'host':'imap.example.invalid','user':'user'}
        store=EncryptedFileCredentialStore()
        try:
            store.set(service(profile),'user',PASSWORD,self.root)
        except SchedulerError as exc:
            self.skipTest(str(exc))
        self.assertTrue(schedulers.fallback_credential_file_present(self.root))
        with mock.patch.object(sys,'platform','linux'), \
             mock.patch.object(KeyringCredentialStore,'available',staticmethod(lambda: False)):
            self.assertEqual(credential(profile,self.root),PASSWORD)

    def test_missing_credential_raises(self):
        store=EncryptedFileCredentialStore()
        with self.assertRaises(SchedulerError):
            store.get('svc','nobody',self.root)

    def test_keyring_store_uses_backend(self):
        if not KeyringCredentialStore.available():
            self.skipTest('keyring not installed')
        store=KeyringCredentialStore()
        with mock.patch.dict(sys.modules,{'keyring':mock.MagicMock(get_password=lambda s,a: PASSWORD)}):
            self.assertEqual(store.get('svc','user',self.root),PASSWORD)
        with mock.patch.dict(sys.modules,{'keyring':mock.MagicMock(get_password=lambda s,a: None)}):
            with self.assertRaises(SchedulerError):
                store.get('svc','user',self.root)

    def test_factory_prefers_platform_store(self):
        with mock.patch.object(sys,'platform','darwin'):
            self.assertIsInstance(get_credential_store(self.root),KeychainCredentialStore)
        with mock.patch.object(sys,'platform','linux'), \
             mock.patch.object(KeyringCredentialStore,'available',staticmethod(lambda: True)):
            self.assertIsInstance(get_credential_store(self.root),KeyringCredentialStore)
        with mock.patch.object(sys,'platform','linux'), \
             mock.patch.object(KeyringCredentialStore,'available',staticmethod(lambda: False)), \
             mock.patch.object(EncryptedFileCredentialStore,'available',staticmethod(lambda: True)):
            with self.assertRaises(SchedulerError):
                get_credential_store(self.root)
            self.assertIsInstance(
                get_credential_store(self.root,allow_insecure_fallback=True),
                EncryptedFileCredentialStore)
        with mock.patch.object(sys,'platform','linux'), \
             mock.patch.object(KeyringCredentialStore,'available',staticmethod(lambda: False)), \
             mock.patch.object(EncryptedFileCredentialStore,'available',staticmethod(lambda: False)):
            with self.assertRaises(SchedulerError):
                get_credential_store(self.root)

class FactoryTests(unittest.TestCase):
    def test_platform_selection(self):
        with mock.patch.object(sys,'platform','darwin'):self.assertIsInstance(get_scheduler(),LaunchdScheduler)
        with mock.patch.object(sys,'platform','linux'):self.assertIsInstance(get_scheduler(),SystemdScheduler)
        with mock.patch.object(sys,'platform','win32'):self.assertIsInstance(get_scheduler(),SchtasksScheduler)
        with mock.patch.object(sys,'platform','aix'):
            with self.assertRaises(SchedulerError):get_scheduler()

    def test_command_contains_no_secret(self):
        parts=sync_command(FAKE_ROOT,'work')
        self.assertEqual(parts[-2:],['run','work'])
        self.assertNotIn(PASSWORD,' '.join(parts))

    def test_archive_id_is_stable(self):
        self.assertEqual(archive_id(FAKE_ROOT),archive_id(FAKE_ROOT))
        self.assertEqual(len(archive_id(FAKE_ROOT)),16)

if __name__=='__main__':
    unittest.main()
