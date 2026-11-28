# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Versioned, transactional schema migrations for the local databases.

Schema-creating statements are idempotent (`IF NOT EXISTS`), so version 1 is
both the baseline for a brand-new database and the upgrade path for an
existing one. Additive column changes are guarded by their version, which is
applied exactly once. A step may also carry a backfill callable that runs in
the same transaction, so data and schema move together.

Versions are tracked with `PRAGMA user_version`, which SQLite stores in the
database header and updates inside the same transaction as the DDL. Opening
a database written by a newer build raises instead of proceeding, so an older
binary cannot silently damage a newer archive.
"""
import json
from pathlib import Path
from .db import connect as db_connect

from .timestamps import parse_timestamp

class MigrationError(RuntimeError):
    pass

ATTACHMENT_TEXT_FIELDS = ('kind', 'name', 'title', 'uri', 'description', 'url',
                          'link', 'source', 'address', 'reaction', 'actor',
                          'price', 'seller')

def attachment_search_text(attachments):
    """Flatten normalized attachment metadata into searchable text."""
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments or '[]')
        except (TypeError, ValueError):
            return ''
    if not isinstance(attachments, list):
        return ''
    parts = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        for key in ATTACHMENT_TEXT_FIELDS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return ' '.join(dict.fromkeys(parts))[:10_000]

def _catalog_attachment_text(db, root):
    for record_id, raw in db.execute(
            'SELECT record_id, attachments_json FROM records').fetchall():
        db.execute('UPDATE records SET attachments_text=? WHERE record_id=?',
                   (attachment_search_text(raw), record_id))

DATABASES = (('archive', 'archive.sqlite3'),
             ('research', 'research.sqlite3'),
             ('catalog', 'catalog.sqlite3'))

ARCHIVE_V1 = (
    'CREATE TABLE IF NOT EXISTS messages('
    'hash TEXT PRIMARY KEY, subject TEXT, sender TEXT, date TEXT, '
    'message_id TEXT, reply_to TEXT, body TEXT)',
    'CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5('
    'hash UNINDEXED, subject, sender, body)',
    'CREATE INDEX IF NOT EXISTS messages_message_id ON messages(message_id)',
    'CREATE INDEX IF NOT EXISTS messages_reply_to ON messages(reply_to)',
    'CREATE TABLE IF NOT EXISTS origins('
    'account TEXT, folder TEXT, validity TEXT, uid TEXT, '
    'hash TEXT NOT NULL REFERENCES messages(hash), '
    'PRIMARY KEY(account,folder,validity,uid))',
)

ARCHIVE_V2 = (
    'CREATE TABLE IF NOT EXISTS attachment_index('
    'hash TEXT NOT NULL REFERENCES messages(hash), part INTEGER, '
    'filename TEXT, mime TEXT, content TEXT, PRIMARY KEY(hash,part))',
    'CREATE TABLE IF NOT EXISTS extraction_versions('
    'hash TEXT PRIMARY KEY REFERENCES messages(hash), version INTEGER NOT NULL)',
)

def _backfill_archive_timestamps(db, root=None):
    """Populate normalized chronology for rows written before schema 3."""
    updates = []
    for digest, date in db.execute('SELECT hash,date FROM messages'):
        epoch, precision = parse_timestamp(date)
        updates.append((epoch, precision, digest))
    db.executemany('UPDATE messages SET sort_ts=?,date_precision=? WHERE hash=?', updates)

def _backfill_message_sizes(db, root=None):
    """Record original-file sizes so storage totals avoid a filesystem scan."""
    if root is None:
        return
    updates = []
    for digest, in db.execute('SELECT hash FROM messages').fetchall():
        path = Path(root) / 'raw' / (digest + '.eml')
        try:
            size = path.stat().st_size if path.is_file() and not path.is_symlink() else None
        except OSError:
            size = None
        updates.append((size, digest))
    db.executemany('UPDATE messages SET size=? WHERE hash=?', updates)

ARCHIVE_V3 = (
    'ALTER TABLE messages ADD COLUMN sort_ts INTEGER',
    'ALTER TABLE messages ADD COLUMN date_precision TEXT',
    'CREATE INDEX IF NOT EXISTS messages_sort_ts ON messages(sort_ts)',
    _backfill_archive_timestamps,
)

ARCHIVE_V4 = (
    'CREATE TABLE IF NOT EXISTS import_runs('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_key TEXT, '
    'started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, '
    'files_seen INTEGER, records_seen INTEGER, records_new INTEGER, '
    'records_existing INTEGER, records_failed INTEGER, detail TEXT)',
    'CREATE INDEX IF NOT EXISTS import_runs_by_time ON import_runs(started_at DESC)',
)

ARCHIVE_V5 = (
    'ALTER TABLE messages ADD COLUMN size INTEGER',
    _backfill_message_sizes,
)

ARCHIVE_V6 = (
    'CREATE TABLE IF NOT EXISTS source_settings('
    'source TEXT NOT NULL, source_key TEXT NOT NULL, disabled_at TEXT, note TEXT, '
    'PRIMARY KEY(source, source_key))',
    'CREATE TABLE IF NOT EXISTS source_events('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, '
    'source_key TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, detail TEXT)',
    'CREATE INDEX IF NOT EXISTS source_events_by_time ON source_events(at DESC)',
)

ARCHIVE_V7 = (
    'CREATE TABLE IF NOT EXISTS imap_folder_state('
    'account_id TEXT NOT NULL, folder_name TEXT NOT NULL, uidvalidity TEXT, '
    'last_seen_uid INTEGER NOT NULL DEFAULT 0, remote_message_count INTEGER, '
    'local_message_count INTEGER, last_synced_at TEXT, status TEXT NOT NULL, '
    'PRIMARY KEY(account_id, folder_name))',
    'CREATE INDEX IF NOT EXISTS imap_folder_state_by_account '
    'ON imap_folder_state(account_id)',
)

RESEARCH_V1 = (
    'CREATE TABLE IF NOT EXISTS workspaces('
    'name TEXT PRIMARY KEY, description TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS searches('
    'workspace TEXT REFERENCES workspaces(name) ON DELETE CASCADE, '
    'name TEXT, query TEXT NOT NULL, tag TEXT NOT NULL, limit_n INTEGER NOT NULL, '
    'PRIMARY KEY(workspace,name))',
    'CREATE TABLE IF NOT EXISTS tags('
    'hash TEXT, tag TEXT, source TEXT, PRIMARY KEY(hash,tag,source))',
    'CREATE INDEX IF NOT EXISTS tags_by_name ON tags(tag,hash)',
    'CREATE TABLE IF NOT EXISTS rules('
    'name TEXT PRIMARY KEY, field TEXT, operator TEXT, value TEXT, tag TEXT, enabled INTEGER)',
    'CREATE TABLE IF NOT EXISTS fields(name TEXT PRIMARY KEY, type TEXT, choices TEXT)',
    'CREATE TABLE IF NOT EXISTS metadata('
    'hash TEXT, field TEXT REFERENCES fields(name) ON DELETE CASCADE, value TEXT, '
    'PRIMARY KEY(hash,field))',
    'CREATE TABLE IF NOT EXISTS reports('
    'name TEXT PRIMARY KEY, workspace TEXT, search TEXT, columns TEXT, group_by TEXT)',
    'CREATE TABLE IF NOT EXISTS summaries('
    'message_hash TEXT NOT NULL, '
    "summary_type TEXT NOT NULL CHECK (summary_type IN ('message','thread')), "
    'model_name TEXT NOT NULL, prompt_version TEXT NOT NULL, '
    'summary_text TEXT NOT NULL, created_at TEXT NOT NULL, '
    'input_digest TEXT, thread_members TEXT, '
    'PRIMARY KEY (message_hash, summary_type, model_name, prompt_version))',
    'CREATE INDEX IF NOT EXISTS summaries_lookup ON summaries('
    'summary_type, model_name, prompt_version, message_hash)',
)

RESEARCH_V2 = (
    'CREATE TABLE IF NOT EXISTS journal_entries('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL, '
    'event_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'revision INTEGER NOT NULL DEFAULT 1, deleted_at TEXT)',
    'CREATE TABLE IF NOT EXISTS journal_revisions('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE, '
    'revision INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, '
    'event_date TEXT, recorded_at TEXT NOT NULL, UNIQUE(entry_id, revision))',
    'CREATE INDEX IF NOT EXISTS journal_revisions_by_entry '
    'ON journal_revisions(entry_id, revision DESC)',
    'CREATE INDEX IF NOT EXISTS journal_entries_by_date ON journal_entries(event_date DESC)',
)

RESEARCH_V3 = (
    'CREATE TABLE IF NOT EXISTS collections('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL '
    'REFERENCES workspaces(name) ON DELETE CASCADE, name TEXT NOT NULL, '
    "description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
    'updated_at TEXT NOT NULL, UNIQUE(workspace, name))',
    'CREATE TABLE IF NOT EXISTS collection_items('
    'collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE, '
    "item_type TEXT NOT NULL CHECK (item_type IN ('email','catalog')), "
    'item_id TEXT NOT NULL, note TEXT, added_at TEXT NOT NULL, '
    'PRIMARY KEY(collection_id, item_type, item_id))',
    'CREATE INDEX IF NOT EXISTS collection_items_by_id '
    'ON collection_items(item_type, item_id)',
)

RESEARCH_V4 = (
    'CREATE TABLE IF NOT EXISTS report_drafts('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE, '
    "title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', "
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS report_drafts_by_collection '
    'ON report_drafts(collection_id, updated_at DESC)',
)

RESEARCH_V5 = (
    'CREATE TABLE IF NOT EXISTS advanced_queries('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'workspace TEXT NOT NULL REFERENCES workspaces(name) ON DELETE CASCADE, '
    'name TEXT NOT NULL, '
    "description TEXT NOT NULL DEFAULT '', "
    'query_json TEXT NOT NULL, '
    "sources TEXT NOT NULL DEFAULT 'email,facebook', "
    'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
    'UNIQUE(workspace, name))',
    'CREATE INDEX IF NOT EXISTS advanced_queries_by_workspace '
    'ON advanced_queries(workspace, updated_at DESC)',
)

CATALOG_V1 = (
    'CREATE TABLE IF NOT EXISTS source_files('
    'file_hash TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_key TEXT NOT NULL, '
    'path TEXT NOT NULL, size INTEGER NOT NULL, imported_at TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS records('
    'record_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_key TEXT NOT NULL, '
    'record_kind TEXT NOT NULL, actor TEXT, occurred_at TEXT, occurred_precision TEXT, '
    'body TEXT, original_json TEXT, '
    'file_hash TEXT NOT NULL REFERENCES source_files(file_hash), created_at TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS records_by_kind ON records(source_type, record_kind)',
    'CREATE INDEX IF NOT EXISTS records_by_time ON records(occurred_at)',
)

CATALOG_V2 = (
    'CREATE VIRTUAL TABLE IF NOT EXISTS record_search USING fts5(body, actor)',
    'CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN '
    'INSERT INTO record_search(rowid, body, actor) VALUES (new.rowid, new.body, new.actor); END',
    'CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN '
    "INSERT INTO record_search(record_search, rowid, body, actor) VALUES('delete', old.rowid, old.body, old.actor); END",
    'CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN '
    "INSERT INTO record_search(record_search, rowid, body, actor) VALUES('delete', old.rowid, old.body, old.actor); "
    'INSERT INTO record_search(rowid, body, actor) VALUES (new.rowid, new.body, new.actor); END',
    'INSERT INTO record_search(rowid, body, actor) SELECT rowid, body, actor FROM records',
)

CATALOG_V3 = (
    'DROP TRIGGER IF EXISTS records_ad',
    'DROP TRIGGER IF EXISTS records_au',
    'CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN '
    'DELETE FROM record_search WHERE rowid = old.rowid; END',
    'CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN '
    'DELETE FROM record_search WHERE rowid = old.rowid; '
    'INSERT INTO record_search(rowid, body, actor) VALUES (new.rowid, new.body, new.actor); END',
)

CATALOG_V4 = (
    'ALTER TABLE records ADD COLUMN updated_at TEXT',
    "ALTER TABLE records ADD COLUMN revision_kind TEXT",
    'ALTER TABLE records ADD COLUMN revision_key TEXT',
    'ALTER TABLE records ADD COLUMN revision_index INTEGER',
    "ALTER TABLE records ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'",
    'CREATE INDEX IF NOT EXISTS records_revision_key ON records(revision_key)',
)

CATALOG_V5 = (
    "ALTER TABLE records ADD COLUMN attachments_text TEXT NOT NULL DEFAULT ''",
    _catalog_attachment_text,
    'DROP TRIGGER IF EXISTS records_ai',
    'DROP TRIGGER IF EXISTS records_ad',
    'DROP TRIGGER IF EXISTS records_au',
    'DROP TABLE IF EXISTS record_search',
    'CREATE VIRTUAL TABLE record_search USING fts5(body, actor, attachments_text)',
    'CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN '
    'INSERT INTO record_search(rowid, body, actor, attachments_text) '
    'VALUES (new.rowid, new.body, new.actor, new.attachments_text); END',
    'CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN '
    'DELETE FROM record_search WHERE rowid = old.rowid; END',
    'CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN '
    'DELETE FROM record_search WHERE rowid = old.rowid; '
    'INSERT INTO record_search(rowid, body, actor, attachments_text) '
    'VALUES (new.rowid, new.body, new.actor, new.attachments_text); END',
    'INSERT INTO record_search(rowid, body, actor, attachments_text) '
    'SELECT rowid, body, actor, attachments_text FROM records',
)

RESEARCH_V6 = (
    'CREATE TABLE IF NOT EXISTS research_conversations('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'workspace TEXT NOT NULL REFERENCES workspaces(name) ON DELETE CASCADE, '
    'title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS research_turns('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'conversation_id INTEGER NOT NULL REFERENCES research_conversations(id) ON DELETE CASCADE, '
    "role TEXT NOT NULL CHECK(role IN ('user','assistant')), "
    'content TEXT NOT NULL, request_json TEXT, answer_json TEXT, model_name TEXT, '
    'created_at TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS research_turns_by_conversation '
    'ON research_turns(conversation_id, id)',
    'CREATE INDEX IF NOT EXISTS research_conversations_by_workspace '
    'ON research_conversations(workspace, updated_at DESC)',
)

MIGRATIONS = {
    'archive': {1: ARCHIVE_V1, 2: ARCHIVE_V2, 3: ARCHIVE_V3, 4: ARCHIVE_V4,
                5: ARCHIVE_V5, 6: ARCHIVE_V6, 7: ARCHIVE_V7},
    'research': {1: RESEARCH_V1, 2: RESEARCH_V2, 3: RESEARCH_V3, 4: RESEARCH_V4,
                 5: RESEARCH_V5, 6: RESEARCH_V6},
    'catalog': {1: CATALOG_V1, 2: CATALOG_V2, 3: CATALOG_V3, 4: CATALOG_V4,
                5: CATALOG_V5},
}

def target_version(name):
    try:
        return max(MIGRATIONS[name])
    except KeyError:
        raise MigrationError(f'Unknown database {name!r}') from None

def current_version(db):
    return db.execute('PRAGMA user_version').fetchone()[0]

def pending(name, version):
    return [v for v in sorted(MIGRATIONS[name]) if v > version]

def has_schema(db):
    """True when the database already contains user tables (i.e. not brand new)."""
    return db.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0] > 0

def snapshot(db, path):
    """Copy the database through SQLite's backup API before migrating."""
    destination = db_connect(str(path))
    try:
        db.backup(destination)
    finally:
        destination.close()
    return str(path)

def apply(db, name, snapshot_path=None, root=None):
    """Bring `db` up to the newest known version for `name`.

    Returns a dict describing the transition. Pending DDL runs one version per
    transaction together with its `user_version` bump, so an interrupted
    migration leaves the database at the previous version.
    """
    target = target_version(name)
    version = current_version(db)
    if version > target:
        raise MigrationError(
            f'The {name} database was written by a newer version '
            f'(schema {version}, this build supports {target}). '
            'Upgrade the application instead of opening it with an older build.')
    steps = pending(name, version)
    if not steps:
        return {'database': name, 'from': version, 'to': version, 'applied': []}
    if snapshot_path is not None and has_schema(db):
        snapshot(db, snapshot_path)
    for step in steps:
        with db:
            for statement in MIGRATIONS[name][step]:
                if callable(statement):
                    statement(db, root)
                else:
                    db.execute(statement)
            db.execute(f'PRAGMA user_version={step}')
    return {'database': name, 'from': version, 'to': target, 'applied': steps}

def report(root, dry_run=False, snapshot_=True):
    """Inspect (and unless dry_run, migrate) every database under `root`.

    Runs without constructing Archive or Research, so it reports and migrates
    the real on-disk versions instead of whatever those constructors leave.
    """
    root = Path(root)
    result = {}
    for name, filename in DATABASES:
        path = root / filename
        if not path.is_file():
            result[name] = {'present': False, 'target': target_version(name)}
            continue
        conn = db_connect(str(path), timeout=30)
        try:
            version = current_version(conn)
            if dry_run:
                result[name] = {'present': True, 'version': version,
                                'target': target_version(name),
                                'pending': pending(name, version)}
                continue
            backup = path.with_name(f'{path.stem}.v{version}.backup.sqlite3') if snapshot_ else None
            outcome = apply(conn, name, snapshot_path=backup, root=root)
            outcome['present'] = True
            outcome['snapshot'] = str(backup) if backup is not None and backup.exists() else None
            result[name] = outcome
        except MigrationError as exc:
            result[name] = {'present': True, 'error': str(exc)}
        finally:
            conn.close()
    return result
