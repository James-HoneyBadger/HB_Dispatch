# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Source enable/disable, removal previews and safe removal semantics."""
import json, tempfile, unittest
from pathlib import Path
from hb_engine.dispatch import Archive
from hb_engine.facebook import import_files as facebook_import

def message(subject, body, sender='a@example.invalid'):
    return (f'From: {sender}\r\nSubject: {subject}\r\n'
            f'Message-ID: <{subject}@example.invalid>\r\n\r\n{body}').encode()

class SourceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root)

    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def test_disable_and_enable_are_recorded_without_touching_records(self):
        digest=self.a.ingest(message('keep','body'))
        self.assertTrue(self.a.source_enabled('email','imap.example|me'))
        self.a.source_set_enabled('email','imap.example|me',False,note='paused')
        self.assertFalse(self.a.source_enabled('email','imap.example|me'))
        self.assertEqual(self.a.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
        self.assertEqual(self.a.source_events()[0][3],'disabled')
        self.assertEqual(self.a.source_events()[0][5],'paused')
        self.a.source_set_enabled('email','imap.example|me',True)
        self.assertTrue(self.a.source_enabled('email','imap.example|me'))
        self.assertEqual(self.a.source_events()[0][3],'enabled')
        self.assertEqual(self.a.verify(),[])
        self.assertEqual(self.a.db.execute('SELECT hash FROM messages').fetchone()[0],digest)

    def test_sync_refuses_a_disabled_account(self):
        self.a.source_set_enabled('email','imap.example|me',False)
        with self.assertRaises(ValueError):
            self.a.sync('imap.example','me','secret')

    def test_validation_rejects_unknown_sources_and_keys(self):
        with self.assertRaises(ValueError):
            self.a.source_set_enabled('cloud','x',False)
        with self.assertRaises(ValueError):
            self.a.source_set_enabled('email','',False)
        with self.assertRaises(ValueError):
            self.a.source_removal_preview('email','unknown|me')

class EmailRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root)
        self.shared_raw=message('Shared','sharedword')
        self.unique_raw=message('Unique','uniqueword')
        self.eml_only_raw=message('EmlOnly','emlword')
        self.shared=self.a.ingest(self.shared_raw)
        self.unique=self.a.ingest(self.unique_raw)
        self.eml_only=self.a.ingest(self.eml_only_raw)
        with self.a.db:
            self.a.db.execute('INSERT INTO origins VALUES(?,?,?,?,?)',
                              ('imap.a|me','INBOX','1','1',self.shared))
            self.a.db.execute('INSERT INTO origins VALUES(?,?,?,?,?)',
                              ('imap.b|me','INBOX','1','1',self.shared))
            self.a.db.execute('INSERT INTO origins VALUES(?,?,?,?,?)',
                              ('imap.a|me','Sent','1','2',self.unique))

    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def test_preview_explains_shared_and_unique_records(self):
        preview=self.a.source_removal_preview('email','imap.a|me')
        self.assertEqual(preview['origins_removed'],2)
        self.assertEqual(preview['records_removed'],1)
        self.assertEqual(preview['records_retained'],1)
        self.assertEqual(preview['raw_bytes_freed'],len(self.unique_raw))
        self.assertTrue(any('retained' in line for line in preview['explanation']))
        self.assertTrue(any('.eml' in line for line in preview['explanation']))

    def test_removal_requires_confirmation(self):
        with self.assertRaises(ValueError):
            self.a.source_remove('email','imap.a|me')
        self.assertEqual(self.a.db.execute('SELECT count(*) FROM origins').fetchone()[0],3)

    def test_removal_keeps_shared_and_eml_only_messages(self):
        result=self.a.source_remove('email','imap.a|me',confirm=True)
        self.assertTrue(result['removed'])
        self.assertEqual(result['raw_files_removed'],1)
        remaining={h for h, in self.a.db.execute('SELECT hash FROM messages')}
        self.assertEqual(remaining,{self.shared,self.eml_only})
        # The shared message still has its other account's origin.
        self.assertEqual(self.a.db.execute(
            'SELECT account FROM origins WHERE hash=?',(self.shared,)).fetchall(),
            [('imap.b|me',)])
        self.assertFalse((self.a.raw/(self.unique+'.eml')).exists())
        self.assertTrue((self.a.raw/(self.shared+'.eml')).exists())
        self.assertEqual(self.a.find('uniqueword'),[])
        self.assertEqual(self.a.find('sharedword')[0][0],self.shared)
        self.assertEqual(self.a.verify(),[])
        self.assertEqual(self.a.source_events()[0][3],'removed')

class SourceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root)

    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def test_email_verification_is_scoped_and_detects_damage(self):
        first_raw=message('First','firstword'); second_raw=message('Second','secondword')
        first=self.a.ingest(first_raw); second=self.a.ingest(second_raw)
        with self.a.db:
            self.a.db.execute('INSERT INTO origins VALUES(?,?,?,?,?)',
                              ('imap.a|me','INBOX','1','1',first))
            self.a.db.execute('INSERT INTO origins VALUES(?,?,?,?,?)',
                              ('imap.b|me','INBOX','1','1',second))
        clean=self.a.verify_source('email','imap.a|me')
        self.assertEqual((clean['checked'],clean['ok'],clean['bad'],clean['missing']),(1,1,[],[]))
        (self.a.raw/(first+'.eml')).write_bytes(b'altered')
        damaged=self.a.verify_source('email','imap.a|me')
        self.assertEqual(damaged['bad'],[first])
        self.assertEqual(damaged['ok'],0)
        # The other account is unaffected by damage to the first.
        self.assertEqual(self.a.verify_source('email','imap.b|me')['ok'],1)
        (self.a.raw/(second+'.eml')).unlink()
        missing=self.a.verify_source('email','imap.b|me')
        self.assertEqual(missing['missing'],[second])

    def test_catalog_verification_detects_altered_and_missing_export(self):
        export=self.root/'posts.json'
        export.write_text(json.dumps({'your_posts':[
            {'timestamp':1700000000,'data':[{'post':'Bullion note'}]}]}))
        facebook_import([str(export)],self.root)
        clean=self.a.verify_source('catalog','facebook')
        self.assertEqual((clean['checked'],clean['ok']),(1,1))
        export.write_text('{}')
        altered=self.a.verify_source('catalog','facebook')
        self.assertEqual(altered['bad'],[str(export)])
        export.unlink()
        missing=self.a.verify_source('catalog','facebook')
        self.assertEqual(missing['missing'],[str(export)])

    def test_verify_rejects_unknown_sources(self):
        with self.assertRaises(ValueError):
            self.a.verify_source('email','unknown|me')
        with self.assertRaises(ValueError):
            self.a.verify_source('catalog','nope')

class CatalogRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root)
        self.export=self.root/'posts.json'
        self.export.write_text(json.dumps({'your_posts':[
            {'timestamp':1700000000,'data':[{'post':'Bullion note'}]}]}))
        facebook_import([str(self.export)],self.root)

    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def test_preview_and_removal_delete_catalog_records_only(self):
        preview=self.a.source_removal_preview('catalog','facebook')
        self.assertEqual(preview['catalog_records_removed'],1)
        self.assertEqual(preview['catalog_files_removed'],1)
        self.assertGreater(preview['catalog_bytes'],0)
        self.assertTrue(self.export.exists())
        result=self.a.source_remove('catalog','facebook',confirm=True)
        self.assertTrue(result['removed'])
        from hb_engine.catalog import Catalog
        from hb_engine.unified import search_unified
        catalog=Catalog(self.root)
        try:
            self.assertEqual(catalog.stats(),{'files':0,'records':0})
        finally:
            catalog.close()
        self.assertEqual(search_unified(self.root,'bullion'),[])
        self.assertTrue(self.export.exists())
        self.assertEqual(self.a.source_events()[0][3],'removed')

if __name__=='__main__':
    unittest.main()
