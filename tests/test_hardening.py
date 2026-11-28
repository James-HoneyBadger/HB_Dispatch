# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import hashlib, json, os, tempfile, unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from hb_engine.dispatch import Archive
from hb_engine.documents import ExtractionUnavailable
from hb_engine.mime_index import html_text
from hb_engine.backup import backup, restore, validate_snapshot

class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.a=Archive(self.root/'archive')
    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links are not available')

    def test_html_no_active_content(self):
        h=self.a.ingest(b'Subject: test\r\nContent-Type: text/html; charset=utf-8\r\n\r\n'
                        b'<p>uniqueinvoice</p><script>secretcode</script><img src="https://invalid.example">')
        self.assertEqual(self.a.find('uniqueinvoice')[0][0],h)
        self.assertEqual(self.a.find('secretcode'),[])
        self.assertNotIn('https://',self.a.details(h)['body'])
        self.assertEqual(html_text('<style>x</style>visible'),'visible')

    def test_attachments_are_indexed_by_name_and_text(self):
        m=EmailMessage(); m['Subject']='Receipt'; m.set_content('body')
        m.add_attachment(b'raretransaction',maintype='text',subtype='plain',filename='statement.txt')
        m.add_attachment(b'opaque',maintype='application',subtype='pdf',filename='invoice.pdf')
        h=self.a.ingest(m.as_bytes())
        self.assertEqual(self.a.find('raretransaction')[0][0],h)
        self.assertEqual(self.a.find('invoice')[0][0],h)
        self.assertEqual([row[:2] for row in self.a.attachments(h)],
                         [('statement.txt','text/plain'),('invoice.pdf','application/pdf')])
        self.assertEqual(self.a.verify(),[])

    def test_binary_email_attachments_are_text_indexed(self):
        m=EmailMessage(); m['Subject']='Documents'; m.set_content('body')
        m.add_attachment(b'%PDF fake',maintype='application',subtype='pdf',filename='invoice.pdf')
        m.add_attachment(b'PK fake',maintype='application',subtype='octet-stream',filename='report.docx')
        with patch('hb_engine.documents._extract_pdf',return_value=('pdftransaction note',[],'pypdf')), \
             patch('hb_engine.documents._extract_docx',return_value=('docxtransaction note',[],'python-docx')):
            h=self.a.ingest(m.as_bytes())
        self.assertEqual(self.a.find('pdftransaction')[0][0],h)
        self.assertEqual(self.a.find('docxtransaction')[0][0],h)
        self.assertEqual(self.a.verify(),[])

    def test_unavailable_binary_attachment_extractor_does_not_reject_message(self):
        m=EmailMessage(); m['Subject']='Scanned'; m.set_content('body')
        m.add_attachment(b'%PDF fake',maintype='application',subtype='pdf',filename='scan.pdf')
        with patch('hb_engine.documents._extract_pdf',side_effect=ExtractionUnavailable('missing pypdf')):
            h=self.a.ingest(m.as_bytes())
        self.assertEqual(self.a.find('scan')[0][0],h)
        self.assertEqual(self.a.verify(),[])

    def test_reindex_retries_attachments_after_optional_extractor_install(self):
        m=EmailMessage(); m['Subject']='Scanned'; m.set_content('body')
        m.add_attachment(b'%PDF fake',maintype='application',subtype='pdf',filename='scan.pdf')
        with patch('hb_engine.documents._extract_pdf',side_effect=ExtractionUnavailable('missing pypdf')):
            h=self.a.ingest(m.as_bytes())
        self.assertEqual(self.a.find('laterextract'),[])
        with patch('hb_engine.documents.binary_extractors_available',return_value=True), \
             patch('hb_engine.documents._extract_pdf',return_value=('laterextract',[],'pypdf')):
            self.assertEqual(self.a.reindex(),1)
        self.assertEqual(self.a.find('laterextract')[0][0],h)

    def test_reindex_retries_older_binary_attachment_extraction(self):
        m=EmailMessage(); m['Subject']='Invoice'; m.set_content('body')
        m.add_attachment(b'%PDF fake',maintype='application',subtype='pdf',filename='invoice.pdf')
        with patch('hb_engine.documents._extract_pdf',return_value=('upgradedtransaction',[],'pypdf')):
            h=self.a.ingest(m.as_bytes())
        with self.a.db:
            self.a.db.execute('UPDATE extraction_versions SET version=2 WHERE hash=?',(h,))
            self.a.db.execute('UPDATE search SET body=replace(body,?,?)',('upgradedtransaction',''))
        self.assertEqual(self.a.find('upgradedtransaction'),[])
        with patch('hb_engine.documents._extract_pdf',return_value=('upgradedtransaction',[],'pypdf')):
            self.assertEqual(self.a.reindex(),1)
        self.assertEqual(self.a.find('upgradedtransaction')[0][0],h)

    def test_reindex_upgrades_existing_messages(self):
        h=self.a.ingest(b'Content-Type: text/html\r\n\r\n<p>historicword</p>')
        with self.a.db:
            self.a.db.execute('DELETE FROM extraction_versions')
            self.a.db.execute('UPDATE messages SET body=""')
            self.a.db.execute('UPDATE search SET body=""')
        self.assertEqual(self.a.reindex(),1)
        self.assertEqual(self.a.reindex(),0)
        self.assertEqual(self.a.find('historicword')[0][0],h)
        self.assertEqual(self.a.verify(),[])

    def test_reindex_repairs_duplicate_and_missing_search_rows(self):
        first=self.a.ingest(b'Subject: first\r\n\r\nfirstword')
        second=self.a.ingest(b'Subject: second\r\n\r\nsecondword')
        with self.a.db:
            self.a.db.execute('INSERT INTO search SELECT hash,subject,sender,body FROM search WHERE hash=?',(first,))
            self.a.db.execute('DELETE FROM search WHERE hash=?',(second,))
        self.assertEqual(self.a.reindex(),2)
        self.assertEqual(self.a.health()['index_duplicate_rows'],0)
        self.assertEqual(self.a.health()['index_missing'],[])

    def test_backup_includes_the_catalog_and_source(self):
        import json
        from hb_engine.facebook import import_files
        from hb_engine.catalog import Catalog
        export=self.root/'posts.json'
        export.write_text(json.dumps({'your_posts':[
            {'timestamp':1700000000,'data':[{'post':'Bullion receipt'}]}]}))
        import_files([str(export)],self.a.root)
        snapshot=self.root/'snapshot'; restored=self.root/'restored'
        self.assertTrue(backup(self.a.root,snapshot)['verified'])
        self.assertTrue((snapshot/'catalog.sqlite3').is_file())
        manifest=json.loads((snapshot/'backup-manifest.json').read_text())
        self.assertIn('catalog.sqlite3',manifest['files'])
        self.assertTrue(restore(snapshot,restored)['verified'])
        catalog=Catalog(restored)
        try:
            self.assertEqual(catalog.stats()['records'],1)
            self.assertEqual(len(catalog.search_records('bullion')),1)
        finally:
            catalog.close()

    def test_snapshot_rejects_catalog_index_drift(self):
        import json, sqlite3
        from hb_engine.facebook import import_files
        from hb_engine.backup import digest
        export=self.root/'posts.json'
        export.write_text(json.dumps({'your_posts':[
            {'timestamp':1700000000,'data':[{'post':'Bullion receipt'}]}]}))
        import_files([str(export)],self.a.root)
        snapshot=self.root/'snapshot'
        self.assertTrue(backup(self.a.root,snapshot)['verified'])
        conn=sqlite3.connect(snapshot/'catalog.sqlite3')
        try:
            conn.execute('DELETE FROM record_search')
            conn.commit()
        finally:
            conn.close()
        manifest_path=snapshot/'backup-manifest.json'
        manifest=json.loads(manifest_path.read_text())
        manifest['files']['catalog.sqlite3']=digest(snapshot/'catalog.sqlite3')
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): validate_snapshot(snapshot)

    def test_snapshot_rejects_catalog_index_content_drift(self):
        import json, sqlite3
        from hb_engine.facebook import import_files
        from hb_engine.backup import digest
        export=self.root/'posts.json'
        export.write_text(json.dumps({'your_posts':[
            {'timestamp':1700000000,'data':[{'post':'Bullion receipt'}]}]}))
        import_files([str(export)],self.a.root)
        snapshot=self.root/'snapshot'
        self.assertTrue(backup(self.a.root,snapshot)['verified'])
        conn=sqlite3.connect(snapshot/'catalog.sqlite3')
        try:
            conn.execute("UPDATE record_search SET body='rewritten text'")
            conn.commit()
        finally:
            conn.close()
        manifest_path=snapshot/'backup-manifest.json'
        manifest=json.loads(manifest_path.read_text())
        manifest['files']['catalog.sqlite3']=digest(snapshot/'catalog.sqlite3')
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): validate_snapshot(snapshot)

    def test_backup_and_restore_round_trip(self):
        h=self.a.ingest(b'Subject: preserved\r\n\r\nhello')
        snapshot=self.root/'snapshot'; restored=self.root/'restored'
        self.assertTrue(backup(self.a.root,snapshot)['verified'])
        self.assertTrue(restore(snapshot,restored)['verified'])
        copy=Archive(restored)
        try:
            self.assertEqual(copy.verify(),[])
            self.assertEqual(copy.find('preserved')[0][0],h)
        finally:
            copy.db.close()
        with self.assertRaises(FileExistsError): restore(snapshot,restored)
        (snapshot/'raw'/(h+'.eml')).write_bytes(b'changed')
        with self.assertRaises(ValueError): validate_snapshot(snapshot)

    def test_backup_refuses_altered_raw(self):
        h=self.a.ingest(b'Subject: test\r\n\r\nhello')
        (self.a.raw/(h+'.eml')).write_bytes(b'bad')
        with self.assertRaises(ValueError): backup(self.a.root,self.root/'bad')
        self.assertFalse((self.root/'bad').exists())

    def test_restore_pauses_schedule_profiles(self):
        self.a.ingest(b'Subject: one')
        (self.a.root/'schedule.json').write_text(json.dumps({'sample':{'enabled':True}}))
        snapshot=self.root/'snapshot'; restored=self.root/'restored'
        backup(self.a.root,snapshot); restore(snapshot,restored)
        self.assertFalse(json.loads((restored/'schedule.json').read_text())['sample']['enabled'])
        self.assertTrue(validate_snapshot(restored)['verified'])

    def test_snapshot_normalizes_wal_and_rejects_unexpected_directory(self):
        self.a.db.execute('PRAGMA journal_mode=WAL')
        self.a.ingest(b'Subject: wal test')
        snapshot=self.root/'snapshot'
        self.assertTrue(backup(self.a.root,snapshot)['verified'])
        self.assertFalse((snapshot/'archive.sqlite3-wal').exists())
        (snapshot/'unexpected').mkdir()
        with self.assertRaises(ValueError): validate_snapshot(snapshot)

    def test_health_reconciles_missing_index_row(self):
        h=self.a.ingest(b'Subject: one\r\n\r\nbody')
        self.assertTrue(self.a.health()['sqlite_ok'])
        self.assertEqual(self.a.health()['index_missing'],[])
        with self.a.db: self.a.db.execute('DELETE FROM search WHERE hash=?',(h,))
        self.assertEqual(self.a.health()['index_missing'],[h])
        self.assertEqual(self.a.reindex(),1)
        self.assertEqual(self.a.health()['index_missing'],[])
        self.assertEqual(self.a.verify(),[])

    def test_ingest_is_refused_while_sync_holds_the_lock(self):
        from hb_engine.sync_lock import LOCK_SUPPORTED, sync_lock, SyncBusy
        if not LOCK_SUPPORTED: self.skipTest('archive locking is unsupported here')
        with sync_lock(self.a.root):
            with self.assertRaises(SyncBusy): self.a.ingest(b'Subject: busy')
        self.assertEqual(self.a.stats()['messages'],0)

    def test_recover_orphaned_raw_original(self):
        raw=b'Subject: recovered\r\n\r\nbody'; h=hashlib.sha256(raw).hexdigest()
        (self.a.raw/(h+'.eml')).write_bytes(raw)
        self.assertEqual(self.a.health()['raw_unindexed'],[h])
        self.assertEqual(self.a.recover_orphans(),[h])
        self.assertEqual(self.a.recover_orphans(),[])
        self.assertEqual(self.a.health()['raw_unindexed'],[])
        self.assertEqual(self.a.stats(),{'messages':1,'origins':0})

    def test_recover_orphans_ignores_altered_files(self):
        h=hashlib.sha256(b'Subject: real').hexdigest()
        (self.a.raw/(h+'.eml')).write_bytes(b'Subject: altered')
        self.assertEqual(self.a.recover_orphans(),[])
        self.assertEqual(self.a.stats()['messages'],0)

    def test_mixed_inline_and_alternative_prefers_one_branch(self):
        m=EmailMessage(); m.set_content('firstinline'); m.make_mixed()
        alternative=EmailMessage(); alternative.set_content('secondinline')
        alternative.add_alternative('<p>duplicatedhtml</p>',subtype='html')
        m.attach(alternative)
        h=self.a.ingest(m.as_bytes())
        self.assertEqual(self.a.find('firstinline')[0][0],h)
        self.assertEqual(self.a.find('secondinline')[0][0],h)
        self.assertEqual(self.a.find('duplicatedhtml'),[])

    def test_symlinked_raw_original_is_never_read(self):
        h=self.a.ingest(b'Subject: one')
        source=self.a.raw/(h+'.eml'); target=self.root/'outside'
        source.rename(target); self.symlink(source,target)
        self.assertEqual(self.a.verify(),[h])
        with self.assertRaises(ValueError): self.a.export([h],self.root/'export')

    def test_snapshot_rejects_symlink_entry(self):
        self.a.ingest(b'Subject: one')
        snapshot=self.root/'snap'; backup(self.a.root,snapshot)
        self.symlink(snapshot/'bad',self.root)
        with self.assertRaises(ValueError): validate_snapshot(snapshot)

if __name__=='__main__':
    unittest.main()
