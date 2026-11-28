# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Deterministic entity registry across archive sources.

Addresses and display names are extracted by parsing headers; no model is
involved. Exact address matches cluster automatically as heuristic aliases, and
a human can later verify, merge or split identities. Mentions link an entity to
an immutable record, so an entity never alters the record it references.
"""

import email.utils
from datetime import datetime, timezone

ENTITY_TYPES = ('person', 'org', 'account', 'topic')
ALIAS_TYPES = ('email', 'name', 'handle', 'phone')
CONFIDENCE = ('heuristic', 'verified')
ROLES = ('sender', 'recipient', 'author', 'participant', 'mentioned')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_sender(value: str) -> tuple[str, str]:
    """Return (display_name, address) from a header value, tolerating junk."""
    name, address = email.utils.parseaddr(value or '')
    address = (address or '').strip().lower()
    name = ' '.join((name or '').replace('"', ' ').split())
    return name, address


def _entity_for(db, address: str, name: str) -> int:
    row = db.execute(
        'SELECT entity_id FROM entity_aliases WHERE alias_value=? AND alias_type=?',
        (address, 'email')).fetchone()
    if row:
        return int(row[0])
    canonical = name or address
    db.execute('INSERT OR IGNORE INTO entities(canonical_name, entity_type, '
               'created_at) VALUES(?,?,?)', (canonical, 'person', _now()))
    entity_id = int(db.execute(
        'SELECT entity_id FROM entities WHERE canonical_name=? AND entity_type=?',
        (canonical, 'person')).fetchone()[0])
    db.execute('INSERT OR IGNORE INTO entity_aliases(entity_id, alias_value, '
               'alias_type, confidence) VALUES(?,?,?,?)',
               (entity_id, address, 'email', 'heuristic'))
    if name and name != address:
        db.execute('INSERT OR IGNORE INTO entity_aliases(entity_id, alias_value, '
                   'alias_type, confidence) VALUES(?,?,?,?)',
                   (entity_id, name, 'name', 'heuristic'))
    return entity_id


def build(db, progress=None, cancel_event=None) -> dict:
    """Populate entities, aliases and mentions from stored messages."""
    result = {'entities': 0, 'aliases': 0, 'mentions': 0, 'skipped': 0}
    rows = db.execute(
        'SELECT hash, sender, sort_ts FROM messages ORDER BY rowid').fetchall()
    with db:
        for index, (digest, sender, stamp) in enumerate(rows, 1):
            if cancel_event is not None and cancel_event.is_set():
                result['cancelled'] = True
                break
            name, address = parse_sender(sender)
            if not address or '@' not in address:
                result['skipped'] += 1
                continue
            entity_id = _entity_for(db, address, name)
            db.execute('INSERT OR IGNORE INTO entity_mentions(entity_id, '
                       'source_type, record_id, role, timestamp) '
                       'VALUES(?,?,?,?,?)',
                       (entity_id, 'email', digest, 'sender', stamp))
            result['mentions'] += 1
            if progress is not None:
                progress(index, len(rows))
    result['entities'] = db.execute('SELECT count(*) FROM entities').fetchone()[0]
    result['aliases'] = db.execute('SELECT count(*) FROM entity_aliases').fetchone()[0]
    return result


def timeline(db, entity_id: int, limit: int = 200) -> list[dict]:
    rows = db.execute(
        'SELECT m.source_type, m.record_id, m.role, m.timestamp, '
        'msg.subject, msg.sender, msg.date FROM entity_mentions m '
        'LEFT JOIN messages msg ON msg.hash=m.record_id '
        'WHERE m.entity_id=? ORDER BY m.timestamp DESC, m.record_id ASC LIMIT ?',
        (entity_id, limit)).fetchall()
    return [{'source_type': r[0], 'record_id': r[1], 'role': r[2],
             'timestamp': r[3], 'subject': r[4], 'sender': r[5],
             'date': r[6]} for r in rows]


def top_correspondents(db, entity_id: int, limit: int = 10) -> list[dict]:
    """Other entities sharing an email record with this one, by frequency."""
    rows = db.execute(
        'SELECT e.entity_id, e.canonical_name, count(*) AS shared '
        'FROM entity_mentions mine '
        'JOIN entity_mentions other ON other.record_id=mine.record_id '
        ' AND other.source_type=mine.source_type AND other.entity_id<>mine.entity_id '
        'JOIN entities e ON e.entity_id=other.entity_id '
        'WHERE mine.entity_id=? GROUP BY e.entity_id ORDER BY shared DESC, '
        'e.canonical_name ASC LIMIT ?', (entity_id, limit)).fetchall()
    return [{'entity_id': r[0], 'name': r[1], 'shared_records': r[2]} for r in rows]


def activity_by_year(db, entity_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT strftime('%Y', msg.sort_ts, 'unixepoch') AS year, count(*) "
        "FROM entity_mentions m JOIN messages msg ON msg.hash=m.record_id "
        "WHERE m.entity_id=? AND msg.sort_ts IS NOT NULL "
        "GROUP BY year ORDER BY year", (entity_id,)).fetchall()
    return [{'year': r[0], 'mentions': r[1]} for r in rows if r[0]]


def merge(db, keep_id: int, merge_id: int) -> dict:
    """Fold one entity into another, moving aliases and mentions."""
    if keep_id == merge_id:
        raise ValueError('cannot merge an entity into itself')
    for entity_id in (keep_id, merge_id):
        if not db.execute('SELECT 1 FROM entities WHERE entity_id=?',
                          (entity_id,)).fetchone():
            raise ValueError(f'no such entity: {entity_id}')
    with db:
        db.execute('UPDATE OR IGNORE entity_mentions SET entity_id=? '
                   'WHERE entity_id=?', (keep_id, merge_id))
        db.execute('DELETE FROM entity_mentions WHERE entity_id=?', (merge_id,))
        db.execute('UPDATE OR IGNORE entity_aliases SET entity_id=? WHERE entity_id=?',
                   (keep_id, merge_id))
        db.execute('DELETE FROM entity_aliases WHERE entity_id=?', (merge_id,))
        db.execute('DELETE FROM entities WHERE entity_id=?', (merge_id,))
    return {'kept': keep_id, 'merged': merge_id}


def verify_alias(db, alias_id: int) -> None:
    """Mark an alias as human-verified."""
    with db:
        updated = db.execute(
            "UPDATE entity_aliases SET confidence='verified' WHERE alias_id=?",
            (alias_id,)).rowcount
    if not updated:
        raise ValueError(f'no such alias: {alias_id}')


def split_alias(db, alias_id: int, canonical_name: str) -> int:
    """Move one alias to a new entity, leaving the rest untouched."""
    row = db.execute('SELECT entity_id, alias_value, alias_type FROM entity_aliases '
                     'WHERE alias_id=?', (alias_id,)).fetchone()
    if not row:
        raise ValueError(f'no such alias: {alias_id}')
    with db:
        db.execute('INSERT INTO entities(canonical_name, entity_type, created_at) '
                   'VALUES(?,?,?)', (canonical_name, 'person', _now()))
        new_id = int(db.execute(
            'SELECT entity_id FROM entities WHERE canonical_name=? AND entity_type=?',
            (canonical_name, 'person')).fetchone()[0])
        db.execute('UPDATE entity_aliases SET entity_id=? WHERE alias_id=?',
                   (new_id, alias_id))
    return new_id


def list_entities(db, limit: int = 100) -> list[dict]:
    rows = db.execute(
        'SELECT e.entity_id, e.canonical_name, e.entity_type, '
        'count(DISTINCT m.id) AS mentions FROM entities e '
        'LEFT JOIN entity_mentions m ON m.entity_id=e.entity_id '
        'GROUP BY e.entity_id ORDER BY mentions DESC, e.canonical_name '
        'LIMIT ?', (limit,)).fetchall()
    return [{'entity_id': r[0], 'canonical_name': r[1], 'entity_type': r[2],
             'mentions': r[3]} for r in rows]


def aliases(db, entity_id: int) -> list[dict]:
    rows = db.execute('SELECT alias_id, alias_value, alias_type, confidence '
                      'FROM entity_aliases WHERE entity_id=? ORDER BY alias_id',
                      (entity_id,)).fetchall()
    return [{'alias_id': r[0], 'value': r[1], 'type': r[2],
             'confidence': r[3]} for r in rows]
