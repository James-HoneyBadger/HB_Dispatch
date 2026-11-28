# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import sqlite3, tempfile, unittest
from pathlib import Path
from hb_engine.dispatch import Archive
from hb_engine.research import Research
from hb_engine.catalog import Catalog
from hb_engine.migrations import MigrationError, apply, current_version, target_version, MIGRATIONS

LEGACY_ARCHIVE = '''
CREATE TABLE messages(hash TEXT PRIMARY KEY, subject TEXT, sender TEXT, date TEXT, message_id TEXT, reply_to TEXT, body TEXT);
CREATE VIRTUAL TABLE search USING fts5(hash UNINDEXED, subject, sender, body);
CREATE INDEX messages_message_id ON messages(message_id);
CREATE INDEX messages_reply_to ON messages(reply_to);
CREATE TABLE origins(account TEXT, folder TEXT, validity TEXT, uid TEXT, hash TEXT NOT NULL REFERENCES messages(hash), PRIMARY KEY(account,folder,validity,uid));
'''

LEGACY_RESEARCH = '''
CREATE TABLE workspaces(name TEXT PRIMARY KEY, description TEXT NOT NULL);
CREATE TABLE searches(workspace TEXT REFERENCES workspaces(name) ON DELETE CASCADE,name TEXT,query TEXT NOT NULL,tag TEXT NOT NULL,limit_n INTEGER NOT NULL,PRIMARY KEY(workspace,name));
CREATE TABLE tags(hash TEXT,tag TEXT,source TEXT,PRIMARY KEY(hash,tag,source));
CREATE TABLE rules(name TEXT PRIMARY KEY,field TEXT,operator TEXT,value TEXT,tag TEXT,enabled INTEGER);
CREATE TABLE fields(name TEXT PRIMARY KEY,type TEXT,choices TEXT);
CREATE TABLE metadata(hash TEXT,field TEXT REFERENCES fields(name) ON DELETE CASCADE,value TEXT,PRIMARY KEY(hash,field));
CREATE TABLE reports(name TEXT PRIMARY KEY,workspace TEXT,search TEXT,columns TEXT,group_by TEXT);
'''

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def legacy_archive(self):
        db=sqlite3.connect(self.root/'archive.sqlite3')
        db.executescript(LEGACY_ARCHIVE)
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",('a'*64,'Hi','s@e','d','<m>',None,'body'))
        db.execute("INSERT INTO search VALUES(?,?,?,?)",('a'*64,'Hi','s@e','body'))
        db.commit()
        self.assertEqual(current_version(db),0)
        return db

    def test_legacy_archive_upgrades_without_data_loss(self):
        db=self.legacy_archive()
        try:
            result=apply(db,'archive')
            self.assertEqual(result['from'],0)
            self.assertEqual(result['to'],target_version('archive'))
            self.assertEqual(current_version(db),target_version('archive'))
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT hash FROM search WHERE search MATCH 'body'").fetchone()[0],'a'*64)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:
            db.close()

    def test_apply_is_idempotent(self):
        db=self.legacy_archive()
        try:
            apply(db,'archive')
            again=apply(db,'archive')
            self.assertEqual(again['applied'],[])
            self.assertEqual(again['from'],again['to'])
        finally:
            db.close()

    def test_newer_database_is_refused(self):
        db=sqlite3.connect(self.root/'archive.sqlite3')
        try:
            db.execute('PRAGMA user_version=99')
            with self.assertRaises(MigrationError) as caught:
                apply(db,'archive')
            self.assertIn('newer version',str(caught.exception))
            self.assertEqual(current_version(db),99)
        finally:
            db.close()

    def test_unknown_database_name(self):
        db=sqlite3.connect(':memory:')
        try:
            with self.assertRaises(MigrationError):
                apply(db,'nonsense')
        finally:
            db.close()

    def test_snapshot_preserves_pre_migration_state(self):
        db=self.legacy_archive()
        backup=self.root/'pre.sqlite3'
        try:
            apply(db,'archive',snapshot_path=backup)
        finally:
            db.close()
        self.assertTrue(backup.is_file())
        copy=sqlite3.connect(backup)
        try:
            self.assertEqual(copy.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            self.assertEqual(current_version(copy),0)
        finally:
            copy.close()

    def test_no_snapshot_for_brand_new_database(self):
        db=sqlite3.connect(self.root/'fresh.sqlite3')
        backup=self.root/'should-not-exist.sqlite3'
        try:
            apply(db,'archive',snapshot_path=backup)
        finally:
            db.close()
        self.assertFalse(backup.exists())

    def test_legacy_research_upgrades_and_keeps_rows(self):
        db=sqlite3.connect(self.root/'research.sqlite3')
        db.executescript(LEGACY_RESEARCH)
        db.execute("INSERT INTO workspaces VALUES('Metals','desc')")
        db.commit()
        try:
            result=apply(db,'research')
            self.assertEqual(result['to'],target_version('research'))
            self.assertEqual(db.execute('SELECT name FROM workspaces').fetchall(),[('Metals',)])
            self.assertIn('summaries',{r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
        finally:
            db.close()

    def test_archive_object_opens_legacy_database(self):
        self.legacy_archive().close()
        archive=Archive(self.root)
        try:
            self.assertEqual(current_version(archive.db),target_version('archive'))
            self.assertEqual(archive.stats()['messages'],1)
            self.assertEqual(len(archive.find('body')),1)
        finally:
            archive.db.close()

    def test_research_object_upgrades_and_seeds_general(self):
        archive=Archive(self.root)
        legacy=sqlite3.connect(self.root/'research.sqlite3')
        legacy.executescript(LEGACY_RESEARCH)
        legacy.commit();legacy.close()
        research=Research(archive)
        try:
            self.assertEqual(current_version(research.db),target_version('research'))
            self.assertEqual(research.db.execute(
                'SELECT description FROM workspaces WHERE name=?',('General',)).fetchone()[0],
                'Default research workspace')
        finally:
            research.close();archive.db.close()

    def test_catalog_version_registry(self):
        self.assertEqual(target_version('catalog'),5)
        self.assertEqual(sorted(MIGRATIONS['catalog']),[1,2,3,4,5])

    def test_archive_version_registry(self):
        self.assertEqual(target_version('archive'),6)
        self.assertEqual(sorted(MIGRATIONS['archive']),[1,2,3,4,5,6])

    def test_archive_v1_upgrades_to_v2_keeping_rows(self):
        db=sqlite3.connect(self.root/'archive.sqlite3')
        try:
            for statement in MIGRATIONS['archive'][1]:
                db.execute(statement)
            db.execute('PRAGMA user_version=1')
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',
                       ('b'*64,'Subject','s@e','d','<m>',None,'body'))
            db.execute('INSERT INTO search VALUES(?,?,?,?)',('b'*64,'Subject','s@e','body'))
            db.commit()
            result=apply(db,'archive')
            self.assertEqual(result['applied'],[2,3,4,5,6])
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'attachment_index','extraction_versions','import_runs'}<=tables)
            self.assertEqual(db.execute('SELECT count(*) FROM import_runs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT hash FROM search WHERE search MATCH 'body'").fetchone()[0],'b'*64)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:
            db.close()

    def test_opening_a_legacy_archive_snapshots_before_migrating(self):
        db=sqlite3.connect(self.root/'archive.sqlite3')
        try:
            db.executescript(LEGACY_ARCHIVE)
            db.execute('PRAGMA user_version=1')
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                       ('a'*64,'Hi','s@e','d','<m>',None,'body'))
            db.execute("INSERT INTO search VALUES(?,?,?,?)",('a'*64,'Hi','s@e','body'))
            db.commit()
        finally:
            db.close()
        archive=Archive(self.root)
        try:
            self.assertEqual(current_version(archive.db),target_version('archive'))
            snapshots=sorted(self.root.glob('archive.v1.*.backup.sqlite3'))
            self.assertEqual(len(snapshots),1)
            copy=sqlite3.connect(snapshots[0])
            try:
                self.assertEqual(current_version(copy),1)
                self.assertEqual(copy.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            finally:
                copy.close()
            # An up-to-date archive is not snapshotted again.
            archive.db.close()
            archive=Archive(self.root)
            self.assertEqual(len(sorted(self.root.glob('archive.v*.backup.sqlite3'))),1)
        finally:
            archive.db.close()

    def test_research_upgrades_to_journal_schema(self):
        db=sqlite3.connect(self.root/'research.sqlite3')
        try:
            for statement in MIGRATIONS['research'][1]:
                db.execute(statement)
            db.execute('PRAGMA user_version=1')
            db.execute("INSERT INTO workspaces VALUES('Keep','unchanged')")
            db.commit()
            result=apply(db,'research')
            self.assertEqual(result['applied'],list(range(2,target_version('research')+1)))
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'journal_entries','journal_revisions','advanced_queries'}<=tables)
            self.assertEqual(db.execute("SELECT description FROM workspaces WHERE name='Keep'").fetchone()[0],
                             'unchanged')
        finally:
            db.close()

    def test_catalog_upgrades_from_v1(self):
        db=sqlite3.connect(self.root/'catalog.sqlite3')
        for statement in MIGRATIONS['catalog'][1]:
            db.execute(statement)
        db.execute('PRAGMA user_version=1')
        db.execute("INSERT INTO source_files VALUES('h','facebook','posts','p',1,'now')")
        db.execute("INSERT INTO records VALUES('r','facebook','posts','post',NULL,NULL,'second','hello',NULL,'h','now')")
        db.commit();db.close()
        catalog=Catalog(self.root)
        try:
            self.assertEqual(current_version(catalog.db),target_version('catalog'))
            self.assertEqual(len(catalog.search_records('hello')),1)
            record=catalog.record('r')
            self.assertEqual(record['attachments'],[])
            self.assertIsNone(record['revision_index'])
            self.assertIsNone(record['updated_at'])
        finally:
            catalog.close()

    def test_catalog_v4_attachments_become_searchable(self):
        db=sqlite3.connect(self.root/'catalog.sqlite3')
        try:
            for version in (1,2,3,4):
                for statement in MIGRATIONS['catalog'][version]:
                    if callable(statement):
                        statement(db,self.root)
                    else:
                        db.execute(statement)
            db.execute('INSERT INTO source_files VALUES(?,?,?,?,?,?)',
                       ('h','facebook','posts','p',1,'now'))
            db.execute(
                "INSERT INTO records VALUES('r','facebook','posts','post',NULL,NULL,"
                "'second','hello',NULL,'h','now',NULL,NULL,NULL,NULL,?)",
                ('[{"kind": "file", "name": "bullseye.pdf"}]',))
            db.execute('PRAGMA user_version=4')
            db.commit()
        finally:
            db.close()
        catalog=Catalog(self.root)
        try:
            self.assertEqual(current_version(catalog.db),target_version('catalog'))
            self.assertEqual(len(catalog.search_records('bullseye')),1)
            record=catalog.record('r')
            self.assertIn('bullseye.pdf',record['attachments_text'])
        finally:
            catalog.close()

    def test_failed_statement_leaves_previous_version(self):
        db=sqlite3.connect(self.root/'broken.sqlite3')
        original=MIGRATIONS['archive'][1]
        MIGRATIONS['archive'][1]=original+('CREATE TABLE messages(x INTEGER)',)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                apply(db,'archive')
            self.assertEqual(current_version(db),0)
        finally:
            MIGRATIONS['archive'][1]=original
            db.close()

    def test_report_dry_run_does_not_write(self):
        from hb_engine.migrations import report
        self.legacy_archive().close()
        result=report(self.root,dry_run=True)
        self.assertEqual(result['archive']['version'],0)
        self.assertEqual(result['archive']['pending'],sorted(MIGRATIONS['archive']))
        self.assertFalse(result['research']['present'])
        db=sqlite3.connect(self.root/'archive.sqlite3')
        try:
            self.assertEqual(current_version(db),0)
        finally:
            db.close()

    def test_report_migrates_and_records_snapshot(self):
        from hb_engine.migrations import report
        self.legacy_archive().close()
        result=report(self.root)
        self.assertEqual(result['archive']['from'],0)
        self.assertEqual(result['archive']['to'],target_version('archive'))
        self.assertIsNotNone(result['archive']['snapshot'])
        self.assertTrue(Path(result['archive']['snapshot']).is_file())
        again=report(self.root)
        self.assertEqual(again['archive']['applied'],[])

    def test_cli_migrate_reports_legacy_version(self):
        from hb_engine.dispatch import main
        import contextlib, io, json as _json
        self.legacy_archive().close()
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['--archive',str(self.root),'migrate','--dry-run'])
        self.assertEqual(code,0)
        report_doc=_json.loads(out.getvalue())
        self.assertEqual(report_doc['archive']['version'],0)
        self.assertEqual(report_doc['archive']['pending'],sorted(MIGRATIONS['archive']))

if __name__=='__main__':
    unittest.main()
