# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Versioned, newline-delimited JSON IPC for the desktop shell.

One JSON object per line. Requests carry a protocol_version, request_id,
method, and params. Responses carry the same protocol_version and
request_id with either result or error. Long-running jobs emit progress
events and a terminal job_complete or job_error event.
"""
import hashlib, json, shutil, sqlite3, sys, threading, uuid
from .db import connect as db_connect
from .db import session_key
from pathlib import Path

PROTOCOL_VERSION = 1

def _error(request_id, code, message):
    return {'protocol_version': PROTOCOL_VERSION, 'request_id': request_id,
            'error': {'code': code, 'message': message}}

def _result(request_id, result):
    return {'protocol_version': PROTOCOL_VERSION, 'request_id': request_id, 'result': result}

def _event(kind, **payload):
    return {'protocol_version': PROTOCOL_VERSION, 'event': kind, **payload}

def _archive_status(root):
    root = Path(root).expanduser()
    return {
        'path': str(root),
        'exists': ((root / 'archive.sqlite3').is_file()
                   and not (root / 'archive.sqlite3').is_symlink()),
        'databases': {
            name: (root / name).is_file()
            for name in ('archive.sqlite3', 'catalog.sqlite3', 'research.sqlite3')
        },
    }


def _create_archive(root):
    """Create a new plaintext archive directory after an explicit UI request."""
    from .catalog import Catalog
    from .dispatch import Archive
    from .migrations import MigrationError
    from .research import Research
    from .sync_lock import SyncBusy

    candidate = Path(root).expanduser()
    if candidate.is_symlink():
        raise IPCError('ARCHIVE_PATH', 'The archive location must not be a symbolic link.')
    path = candidate.resolve()
    if path.exists() and not path.is_dir():
        raise IPCError('ARCHIVE_PATH', 'The archive location is not a directory.')
    names = ('archive.sqlite3', 'catalog.sqlite3', 'research.sqlite3')
    if path.is_dir() and any((path / name).is_symlink() for name in names):
        raise IPCError('ARCHIVE_PATH', 'Archive database paths must not be symbolic links.')
    if path.is_dir() and ((path / '.hb-encryption.json').is_file()
                          or (path / '.hb-encryption.json').is_symlink()):
        raise IPCError('ARCHIVE_EXISTS',
                       'That folder contains encryption metadata. Open it as an existing '
                       'archive or choose an empty folder.')
    if path.is_dir() and any((path / name).exists() for name in names):
        raise IPCError('ARCHIVE_EXISTS',
                       'That folder already contains HB Continuum databases. '
                       'Choose an empty folder or open the existing archive.')
    created = not path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = None
    try:
        archive = Archive(path)
        catalog = Catalog(path)
        catalog.close()
        research = Research(archive)
        research.close()
    except (ValueError, OSError, sqlite3.Error, MigrationError, SyncBusy) as exc:
        if created:
            shutil.rmtree(path, ignore_errors=True)
        else:
            for name in names:
                try:
                    (path / name).unlink()
                except FileNotFoundError:
                    pass
        raise IPCError('ARCHIVE_CREATE_FAILED',
                       'Could not create the archive: ' + str(exc)) from None
    finally:
        if archive is not None:
            archive.db.close()
    return _archive_status(path)


def _archive_stats(root):
    path = Path(root) / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = db_connect(str(path), timeout=30)
    conn.execute('PRAGMA query_only=ON')
    try:
        messages = conn.execute('SELECT count(*) FROM messages').fetchone()[0]
        origins = conn.execute('SELECT count(*) FROM origins').fetchone()[0]
    finally:
        conn.close()
    return {'messages': messages, 'origins': origins}

def _valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

def _ro_connect(path):
    conn = db_connect(str(path), timeout=30)
    conn.execute('PRAGMA query_only=ON')
    return conn

def _hide_disabled_sources(root):
    """Read the display preference; an unreadable file leaves defaults in force."""
    from .preferences import load as load_prefs
    try:
        return bool(load_prefs(root).get('hide_disabled_sources'))
    except (OSError, ValueError):
        return False

def _attachments(conn, digest):
    """Indexed attachment metadata; legacy archives without the table yield none."""
    try:
        rows = conn.execute(
            'SELECT filename,mime,length(content) FROM attachment_index WHERE hash=? ORDER BY part',
            (digest,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{'filename': name or '', 'mime': kind or '', 'characters': size or 0}
            for name, kind, size in rows]

def _message(root, digest):
    path = Path(root) / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        row = conn.execute(
            'SELECT subject,sender,date,message_id,reply_to,body FROM messages WHERE hash=?',
            (digest,)).fetchone()
        attachments = _attachments(conn, digest)
    finally:
        conn.close()
    if row is None:
        raise IPCError('NOT_FOUND', 'message not found')
    return {'hash': digest, 'subject': row[0] or '', 'sender': row[1] or '',
            'date': row[2] or '', 'message_id': row[3] or '', 'reply_to': row[4] or '',
            'body': row[5] or '', 'attachments': attachments}

def _thread_hashes(conn, digest):
    row = conn.execute('SELECT message_id,reply_to FROM messages WHERE hash=?', (digest,)).fetchone()
    if row is None:
        raise IPCError('NOT_FOUND', 'message not found')
    visited = {digest}
    frontier = {value for value in row if value}
    identifiers = set()
    while frontier:
        value = frontier.pop()
        if value in identifiers:
            continue
        identifiers.add(value)
        for member, mid, parent in conn.execute(
                'SELECT hash,message_id,reply_to FROM messages WHERE message_id=? OR reply_to=?',
                (value, value)):
            visited.add(member)
            frontier.update(v for v in (mid, parent) if v and v not in identifiers)
    return sorted(visited)

def _thread(root, digest):
    path = Path(root) / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        members = _thread_hashes(conn, digest)
        messages = []
        for member in members:
            row = conn.execute(
                'SELECT subject,sender,date,body FROM messages WHERE hash=?', (member,)).fetchone()
            messages.append({'hash': member, 'subject': row[0] or '', 'sender': row[1] or '',
                             'date': row[2] or '', 'body': row[3] or '',
                             'attachments': _attachments(conn, member)})
    finally:
        conn.close()
    return {'hash': digest, 'messages': messages}

def _record(root, record_id):
    path = Path(root) / 'catalog.sqlite3'
    if not path.is_file():
        raise IPCError('CATALOG_NOT_AVAILABLE', 'The selected archive has no catalog.sqlite3')
    conn = _ro_connect(path)
    try:
        try:
            row = conn.execute(
                'SELECT record_id,source_type,source_key,record_kind,actor,occurred_at,'
                'occurred_precision,body,original_json,updated_at,revision_kind,'
                'revision_key,revision_index,attachments_json '
                'FROM records WHERE record_id=?', (record_id,)).fetchone()
        except sqlite3.OperationalError:
            old = conn.execute(
                'SELECT record_id,source_type,source_key,record_kind,actor,occurred_at,'
                'body,original_json FROM records WHERE record_id=?',
                (record_id,)).fetchone()
            row = ((old[0], old[1], old[2], old[3], old[4], old[5], None,
                    old[6], old[7], None, None, None, None, '[]')
                   if old else None)
        revisions = []
        if row is not None and row[11]:
            try:
                keys = ('record_id', 'revision_index', 'revision_kind', 'occurred_at',
                        'updated_at', 'actor', 'body')
                revision_rows = conn.execute(
                    'SELECT record_id,revision_index,revision_kind,occurred_at,'
                    'updated_at,actor,substr(body,1,200) FROM records '
                    'WHERE revision_key=? ORDER BY revision_index IS NULL, '
                    'revision_index, COALESCE(updated_at,occurred_at,\'\') LIMIT 100',
                    (row[11],)).fetchall()
                latest_id = revision_rows[-1][0] if revision_rows else None
                for values in revision_rows:
                    item = dict(zip(keys, values))
                    item['current'] = item['record_id'] == latest_id
                    revisions.append(item)
            except sqlite3.OperationalError:
                revisions = []
    finally:
        conn.close()
    if row is None:
        raise IPCError('NOT_FOUND', 'record not found')
    try:
        attachments = json.loads(row[13] or '[]')
    except (TypeError, ValueError):
        attachments = []
    return {'record_id': row[0], 'source_type': row[1], 'source_key': row[2],
            'record_kind': row[3], 'actor': row[4] or '', 'occurred_at': row[5] or '',
            'occurred_precision': row[6] or '', 'body': row[7] or '',
            'original_json': row[8] or '', 'updated_at': row[9] or '',
            'revision_kind': row[10] or '', 'revision_key': row[11] or '',
            'revision_index': row[12], 'revisions': revisions,
            'attachments': attachments if isinstance(attachments, list) else []}

def _verify(root):
    root = Path(root)
    path = root / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        hashes = [h for h, in conn.execute('SELECT hash FROM messages')]
    finally:
        conn.close()
    bad = []
    for h in hashes:
        if not _valid_hash(h):
            bad.append(h)
            continue
        raw = root / 'raw' / (h + '.eml')
        try:
            if raw.is_symlink() or not raw.is_file():
                bad.append(h)
                continue
            with raw.open('rb') as f:
                if hashlib.file_digest(f, 'sha256').hexdigest() != h:
                    bad.append(h)
        except OSError:
            bad.append(h)
    return {'checked': len(hashes), 'bad': bad}

def _health(root):
    """Read-only consistency report; never migrates or writes to the archive."""
    from .dispatch import diagnose
    from .catalog import catalog_health
    from .sync_lock import sync_lock, SyncBusy
    base = Path(root)
    path = base / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        with sync_lock(base):
            report = diagnose(conn, base / 'raw')
            report['catalog'] = catalog_health(base)
            return report
    except SyncBusy:
        raise IPCError('ARCHIVE_BUSY', 'another archive operation is running') from None
    finally:
        conn.close()

def _encryption_status(root):
    from .encryption import status
    return status(root)


def _encryption_unlock(root, params):
    from .db import EncryptionUnavailable
    from .encryption import EncryptionError, unlock
    password = params.get('password')
    if not isinstance(password, str) or not password:
        raise IPCError('INVALID_PARAMS', 'password must be a non-empty string')
    try:
        return unlock(root, password)
    except EncryptionUnavailable:
        raise IPCError('ENCRYPTION_UNAVAILABLE',
                       'this engine build does not include sqlcipher3') from None
    except EncryptionError:
        raise IPCError('UNLOCK_FAILED',
                       'incorrect password or unreadable encrypted archive') from None


def _encryption_lock(root):
    from .encryption import lock
    return lock(root)


def _entities(root, params):
    """Read-only entity queries plus explicit merge, verify and split actions."""
    from . import entities as entity_module
    action = params.get('action', 'list')
    conn = db_connect(str(Path(root).expanduser() / 'archive.sqlite3'), timeout=30)
    try:
        if action == 'list':
            return {'entities': entity_module.list_entities(conn, int(params.get('limit', 50)))}
        if action == 'aliases':
            return {'aliases': entity_module.aliases(conn, int(params.get('entity_id', 0)))}
        if action == 'timeline':
            return {'timeline': entity_module.timeline(conn, int(params.get('entity_id', 0)),
                                                       int(params.get('limit', 50)))}
        if action == 'correspondents':
            return {'correspondents': entity_module.top_correspondents(
                conn, int(params.get('entity_id', 0)), int(params.get('limit', 10)))}
        if action == 'activity':
            return {'activity': entity_module.activity_by_year(
                conn, int(params.get('entity_id', 0)))}
        if action == 'merge':
            return entity_module.merge(conn, int(params['keep']), int(params['merge']))
        if action == 'verify':
            entity_module.verify_alias(conn, int(params['alias_id']))
            return {'verified': int(params['alias_id'])}
        if action == 'split':
            name = params.get('name')
            if not isinstance(name, str) or not name.strip():
                raise IPCError('INVALID_PARAMS', 'split requires a name')
            return {'entity_id': entity_module.split_alias(conn, int(params['alias_id']), name)}
        raise IPCError('INVALID_PARAMS', f'unknown entity action: {action}')
    except (KeyError, TypeError, ValueError) as exc:
        raise IPCError('INVALID_PARAMS', str(exc)) from None
    finally:
        conn.close()


def _acknowledge_boundary_notice(root):
    """Record that the user accepted the encryption boundary notice.

    Appends one JSON line to ``boundary-notice.log`` in the archive directory.
    That log sits outside the database encryption boundary and holds no
    secrets: a timestamp, a notice identifier and the archive path only.
    """
    import datetime
    path = Path(root).expanduser() / 'boundary-notice.log'
    record = {
        'acknowledged_at': datetime.datetime.now(datetime.timezone.utc)
                                   .isoformat(timespec='seconds'),
        'notice': 'encryption-boundary-v1',
        'archive': str(path.parent),
    }
    try:
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, sort_keys=True) + '\n')
    except OSError as exc:
        raise IPCError('NOTICE_LOG_UNWRITABLE',
                       f'cannot record the acknowledgement: {exc.strerror}') from None
    return {'path': str(path), 'notice': record['notice'],
            'acknowledged_at': record['acknowledged_at']}


def _timeline_sources(value):
    if value is None:
        return ['email']
    if isinstance(value, str):
        value = [part.strip() for part in value.split(',') if part.strip()]
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(source, str) for source in value):
        raise IPCError('INVALID_PARAMS', 'sources must be a list of source names')
    sources = list(dict.fromkeys(value))
    if not sources:
        raise IPCError('INVALID_PARAMS', 'sources must not be empty')
    if any(source not in ('email', 'document') for source in sources):
        raise IPCError('INVALID_PARAMS', 'sources must be email and/or document')
    return sources


def _document_timeline(root, limit, order, after, before, exclude_disabled):
    from .unified import _disabled_sources
    path = Path(root) / 'catalog.sqlite3'
    if not path.is_file():
        return []
    if exclude_disabled and 'document' in _disabled_sources(root)[1]:
        return []
    clauses = ["source_type='document'"]
    params = []
    if after is not None:
        clauses.append("CAST(strftime('%s', occurred_at) AS INTEGER)>=?")
        params.append(after)
    if before is not None:
        clauses.append("CAST(strftime('%s', occurred_at) AS INTEGER)<?")
        params.append(before)
    direction = 'DESC' if order == 'desc' else 'ASC'
    sql = ('SELECT record_id,source_key,record_kind,actor,occurred_at FROM records WHERE '
           + ' AND '.join(clauses) +
           " ORDER BY (strftime('%s', occurred_at) IS NULL), "
           f"CAST(strftime('%s', occurred_at) AS INTEGER) {direction}, record_id ASC LIMIT ?")
    conn = _ro_connect(path)
    try:
        rows = conn.execute(sql, (*params, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [{'source': 'document', 'hash': None, 'record_id': record_id,
             'kind': kind or 'document', 'subject': source_key or '',
             'sender': actor or '', 'date': occurred or ''}
            for record_id, source_key, kind, actor, occurred in rows]


def _timeline(root, sender=None, limit=100, offset=0, order='asc', after=None, before=None,
              sources=None):
    from .dispatch import timeline_rows
    from .timestamps import parse_timestamp
    base = Path(root)
    path = base / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    if sender is not None and not isinstance(sender, str):
        raise IPCError('INVALID_PARAMS', 'sender must be a string')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise IPCError('INVALID_PARAMS', 'limit must be an integer between 1 and 5000')
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000_000:
        raise IPCError('INVALID_PARAMS', 'offset must be a non-negative integer')
    if order not in ('asc', 'desc'):
        raise IPCError('INVALID_PARAMS', 'order must be asc or desc')
    for name, value in (('after', after), ('before', before)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise IPCError('INVALID_PARAMS', f'{name} must be a UTC epoch integer')
    selected = _timeline_sources(sources)
    if 'document' in selected and offset + limit > 5000:
        raise IPCError('INVALID_PARAMS',
                       'merged timeline pages are limited to 5000 entries')
    exclude_disabled = _hide_disabled_sources(base)
    entries = []
    if 'email' in selected:
        conn = _ro_connect(path)
        try:
            email = timeline_rows(
                conn, sender, limit if 'document' not in selected else limit + offset,
                0 if 'document' in selected else offset, order, after, before,
                exclude_disabled=exclude_disabled)
        finally:
            conn.close()
        entries = [{'source': 'email', 'hash': h, 'record_id': None, 'kind': 'message',
                    'subject': s or '', 'sender': f or '', 'date': d or ''}
                   for h, s, f, d in email]
    if 'document' in selected and sender is None:
        entries.extend(_document_timeline(base, limit + offset, order, after, before,
                                          exclude_disabled))
    if 'document' in selected:
        known = []
        unknown = []
        for entry in entries:
            epoch, _ = parse_timestamp(entry['date'])
            order_id = entry['record_id'] or entry['hash'] or ''
            key = (epoch, entry['source'], order_id)
            if epoch is None:
                unknown.append(((entry['source'], order_id), entry))
            else:
                known.append((key, entry))
        if order == 'desc':
            known.sort(key=lambda item: (-item[0][0], item[0][1], item[0][2]))
        else:
            known.sort()
        unknown.sort()
        entries = [entry for _, entry in known] + [entry for _, entry in unknown]
        entries = entries[offset:offset + limit]
    return entries

def _dashboard(root):
    """Operational and historical summary. Every figure has one definition.

    `messages` counts unique archived records; `origins` counts mailbox
    occurrences, so it can exceed `messages`. `undated` counts records whose
    declared date could not be normalized. `by_year` groups normalized
    timestamps only and excludes undated records.
    """
    from datetime import datetime, timezone
    base = Path(root)
    path = base / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        messages = conn.execute('SELECT count(*) FROM messages').fetchone()[0]
        origins = conn.execute('SELECT count(*) FROM origins').fetchone()[0]
        try:
            by_year = conn.execute(
                "SELECT cast(strftime('%Y', sort_ts, 'unixepoch') AS integer) AS year, count(*) "
                'FROM messages WHERE sort_ts IS NOT NULL GROUP BY year ORDER BY year').fetchall()
            earliest, latest = conn.execute('SELECT min(sort_ts), max(sort_ts) FROM messages').fetchone()
            undated = conn.execute('SELECT count(*) FROM messages WHERE sort_ts IS NULL').fetchone()[0]
        except sqlite3.OperationalError:
            by_year, earliest, latest = [], None, None
            undated = messages
    finally:
        conn.close()

    def iso(value):
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()

    return {
        'archive': {'messages': messages, 'origins': origins, 'undated': undated,
                    'earliest': iso(earliest), 'latest': iso(latest)},
        'catalog': _catalog_stats(base),
        'by_year': [{'year': year, 'count': count} for year, count in by_year],
        'storage': _storage_footprint(base),
    }

def _storage_footprint(base):
    """Bytes held by preserved originals and by the databases themselves.

    Original sizes are recorded at import, so a summary avoids walking the
    raw directory. An archive predating that column, or containing records
    whose original is missing, falls back to a filesystem scan.
    """
    base = Path(base)
    raw_bytes = None
    archive_path = base / 'archive.sqlite3'
    if archive_path.is_file():
        conn = _ro_connect(archive_path)
        try:
            total, missing = conn.execute(
                'SELECT coalesce(sum(size),0), coalesce(sum(size IS NULL),0) FROM messages'
            ).fetchone()
            if not missing:
                raw_bytes = total
        except sqlite3.OperationalError:
            raw_bytes = None
        finally:
            conn.close()
    if raw_bytes is None:
        raw_bytes = 0
        raw = base / 'raw'
        if raw.is_dir():
            for entry in raw.iterdir():
                try:
                    if entry.is_file() and not entry.is_symlink():
                        raw_bytes += entry.stat().st_size
                except OSError:
                    continue
    database_bytes = 0
    for name in ('archive.sqlite3', 'research.sqlite3', 'catalog.sqlite3',
                 'archive.sqlite3-wal', 'research.sqlite3-wal', 'catalog.sqlite3-wal',
                 'archive.sqlite3-shm', 'research.sqlite3-shm', 'catalog.sqlite3-shm'):
        candidate = Path(base) / name
        try:
            if candidate.is_file():
                database_bytes += candidate.stat().st_size
        except OSError:
            continue
    return {'raw_bytes': raw_bytes, 'database_bytes': database_bytes,
            'total_bytes': raw_bytes + database_bytes}

def _sources(root):
    """Per-source inventory derived from stored provenance, never from intake.

    `messages` per email account counts distinct message hashes reachable from
    that account's mailbox origins; `origins` counts mailbox occurrences, so a
    message present in two folders counts twice in `origins` and once in
    `messages`. Catalog dates are the recorded ISO timestamps and are only
    reported when every compared value uses that format.
    """
    from datetime import datetime, timezone
    base = Path(root)
    path = base / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        accounts = conn.execute(
            'SELECT o.account, count(DISTINCT o.hash), count(*), count(DISTINCT o.folder) '
            'FROM origins o GROUP BY o.account ORDER BY o.account').fetchall()
        coverage = {}
        for account, _, _, _ in accounts:
            try:
                coverage[account] = conn.execute(
                    'SELECT min(m.sort_ts), max(m.sort_ts) FROM origins o '
                    'JOIN messages m ON m.hash=o.hash WHERE o.account=?', (account,)).fetchone()
            except sqlite3.OperationalError:
                coverage[account] = (None, None)
        try:
            runs = conn.execute(
                "SELECT source_key,status,COALESCE(finished_at,started_at),records_new "
                "FROM import_runs WHERE source='imap' "
                'ORDER BY COALESCE(finished_at,started_at) DESC, id DESC').fetchall()
            history_available = True
        except sqlite3.OperationalError:
            runs = []
            history_available = False
        try:
            settings = {(row[0], row[1]): (row[2], row[3]) for row in conn.execute(
                'SELECT source,source_key,disabled_at,note FROM source_settings')}
        except sqlite3.OperationalError:
            settings = {}
    finally:
        conn.close()

    last_run = {}
    for source_key, status, when, records_new in runs:
        account = source_key.rsplit('|', 1)[0] if source_key else ''
        last_run.setdefault(account, {'status': status, 'at': when, 'records_new': records_new})

    def iso(value):
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()

    email_accounts = [
        {'account': account, 'messages': messages, 'origins': origins,
         'folders': folders, 'earliest': iso(coverage.get(account, (None, None))[0]),
         'latest': iso(coverage.get(account, (None, None))[1]),
         'last_import': last_run.get(account),
         'disabled': settings.get(('email', account), (None, None))[0] is not None,
         'note': settings.get(('email', account), (None, None))[1]}
        for account, messages, origins, folders in accounts]

    catalog_sources = []
    catalog = {'files': 0, 'records': 0}
    catalog_path = base / 'catalog.sqlite3'
    if catalog_path.is_file():
        cat = _ro_connect(catalog_path)
        try:
            for source_type, files, size in cat.execute(
                    'SELECT source_type, count(*), coalesce(sum(size),0) '
                    'FROM source_files GROUP BY source_type ORDER BY source_type'):
                records = cat.execute('SELECT count(*) FROM records WHERE source_type=?',
                                      (source_type,)).fetchone()[0]
                earliest, latest = cat.execute(
                    'SELECT min(occurred_at), max(occurred_at) FROM records '
                    'WHERE source_type=? AND occurred_at IS NOT NULL', (source_type,)).fetchone()
                catalog_sources.append({'source_type': source_type, 'files': files,
                                        'records': records, 'bytes': size,
                                        'earliest': earliest, 'latest': latest,
                                        'disabled': settings.get(
                                            ('catalog', source_type), (None, None))[0] is not None,
                                        'note': settings.get(
                                            ('catalog', source_type), (None, None))[1]})
            catalog = _catalog_stats(base)
        finally:
            cat.close()

    return {'email_accounts': email_accounts, 'catalog_sources': catalog_sources,
            'catalog': catalog, 'storage': _storage_footprint(base),
            'history_available': history_available}

def _with_archive(root, action):
    """Run an archive mutation through the documented Archive API."""
    from .dispatch import Archive
    archive = Archive(root)
    try:
        return action(archive)
    finally:
        archive.db.close()

def _source_param(params):
    source = _text(params.get('source'), 'source', 20)
    key = _text(params.get('source_key'), 'source_key', 500)
    if source not in ('email', 'catalog'):
        raise IPCError('INVALID_PARAMS', 'source must be email or catalog')
    if not key.strip():
        raise IPCError('INVALID_PARAMS', 'source_key must not be empty')
    return source, key

def _source_set_enabled(root, params):
    source, key = _source_param(params)
    enabled = params.get('enabled')
    if not isinstance(enabled, bool):
        raise IPCError('INVALID_PARAMS', 'enabled must be a boolean')
    note = params.get('note')
    if note is not None and not isinstance(note, str):
        raise IPCError('INVALID_PARAMS', 'note must be a string or null')
    return _with_archive(root, lambda a: a.source_set_enabled(source, key, enabled, note))

def _source_preview(root, params):
    source, key = _source_param(params)
    return _with_archive(root, lambda a: a.source_removal_preview(source, key))

def _source_remove(root, params):
    source, key = _source_param(params)
    if params.get('confirm') is not True:
        raise IPCError('CONFIRMATION_REQUIRED',
                       'Review the removal preview and pass confirm=true')
    return _with_archive(root, lambda a: a.source_remove(source, key, confirm=True))

def _source_events(root, limit=100):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise IPCError('INVALID_PARAMS', 'limit must be an integer between 1 and 500')
    keys = ('id', 'source', 'source_key', 'action', 'at', 'detail')
    return _with_archive(root, lambda a: {
        'events': [dict(zip(keys, row)) for row in a.source_events(limit)]})

def _import_history(root, limit=50):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise IPCError('INVALID_PARAMS', 'limit must be an integer between 1 and 500')
    path = Path(root) / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    conn = _ro_connect(path)
    try:
        try:
            rows = conn.execute(
                'SELECT id,source,source_key,started_at,finished_at,status,files_seen,'
                'records_seen,records_new,records_existing,records_failed,detail '
                'FROM import_runs ORDER BY COALESCE(finished_at,started_at) DESC, id DESC LIMIT ?',
                (limit,)).fetchall()
        except sqlite3.OperationalError:
            return {'available': False, 'runs': []}
    finally:
        conn.close()
    keys = ('id', 'source', 'source_key', 'started_at', 'finished_at', 'status',
            'files_seen', 'records_seen', 'records_new', 'records_existing',
            'records_failed', 'detail')
    return {'available': True, 'runs': [dict(zip(keys, row)) for row in rows]}

def _catalog_stats(root):
    path = Path(root) / 'catalog.sqlite3'
    if not path.is_file():
        return {'files': 0, 'records': 0}
    conn = _ro_connect(path)
    try:
        return {'files': conn.execute('SELECT count(*) FROM source_files').fetchone()[0],
                'records': conn.execute('SELECT count(*) FROM records').fetchone()[0]}
    finally:
        conn.close()

def _correspondents(root, limit=100):
    from .dispatch import correspondent_counts
    base = Path(root)
    path = base / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise IPCError('INVALID_PARAMS', 'limit must be an integer between 1 and 5000')
    conn = _ro_connect(path)
    try:
        rows = correspondent_counts(conn, limit, exclude_disabled=_hide_disabled_sources(base))
    finally:
        conn.close()
    return [{'sender': sender or '', 'count': count} for sender, count in rows]

def _relationships(root, limit_nodes=30, max_edges=200, after=None, before=None):
    from .dispatch import relationship_graph
    base = Path(root)
    path = base / 'archive.sqlite3'
    if not path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    if not isinstance(limit_nodes, int) or isinstance(limit_nodes, bool) \
            or not 2 <= limit_nodes <= 100:
        raise IPCError('INVALID_PARAMS', 'limit_nodes must be an integer between 2 and 100')
    if not isinstance(max_edges, int) or isinstance(max_edges, bool) \
            or not 1 <= max_edges <= 500:
        raise IPCError('INVALID_PARAMS', 'max_edges must be an integer between 1 and 500')
    for name, value in (('after', after), ('before', before)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise IPCError('INVALID_PARAMS', f'{name} must be a UTC epoch integer')
    conn = _ro_connect(path)
    try:
        return relationship_graph(conn, base, limit_nodes, max_edges, after, before)
    finally:
        conn.close()

def _migration_report(root):
    """Report on-disk schema versions and pending steps without migrating."""
    from .migrations import report
    return report(Path(root).expanduser(), dry_run=True)

def _preferences(root):
    from .preferences import FIELDS, load as load_prefs
    values = load_prefs(root)
    fields = []
    for key, (group, label, default, allowed) in FIELDS.items():
        if isinstance(default, bool):
            kind = 'bool'
        elif isinstance(default, int):
            kind = 'int'
        elif allowed:
            kind = 'choice'
        else:
            kind = 'text'
        fields.append({'key': key, 'group': group, 'label': label, 'kind': kind,
                       'default': default, 'allowed': list(allowed) if allowed else None})
    return {'values': values, 'fields': fields}

def _preferences_set(root, values):
    from .preferences import save as save_prefs, validate
    if not isinstance(values, dict):
        raise IPCError('INVALID_PARAMS', 'values must be an object')
    validated = validate(values)
    save_prefs(root, validated)
    return {'values': validated}

RESEARCH_TABLES = ('workspaces', 'searches', 'tags', 'fields', 'reports')
def _research_list(root, table):
    if table not in RESEARCH_TABLES:
        raise IPCError('INVALID_PARAMS', 'table must be one of ' + ', '.join(RESEARCH_TABLES))
    path = Path(root) / 'research.sqlite3'
    if not path.is_file():
        return {'table': table, 'rows': []}
    conn = _ro_connect(path)
    try:
        rows = conn.execute('SELECT * FROM ' + table).fetchall()
    finally:
        conn.close()
    return {'table': table, 'rows': [list(r) for r in rows]}

def _research_results(root, workspace, name):
    research_path = Path(root) / 'research.sqlite3'
    archive_path = Path(root) / 'archive.sqlite3'
    if not research_path.is_file():
        raise IPCError('NOT_FOUND', 'no research overlay')
    conn = _ro_connect(research_path)
    try:
        row = conn.execute('SELECT query,tag,limit_n FROM searches WHERE workspace=? AND name=?',
                           (workspace, name)).fetchone()
        if row is None:
            raise IPCError('NOT_FOUND', 'unknown saved search')
        query, tag, limit_n = row
        tagged = None if not tag else {h for h, in conn.execute(
            'SELECT DISTINCT hash FROM tags WHERE tag=?', (tag,))}
    finally:
        conn.close()
    if not archive_path.is_file():
        raise IPCError('ARCHIVE_NOT_AVAILABLE', 'The selected archive has no archive.sqlite3')
    aconn = _ro_connect(archive_path)
    try:
        if query:
            sql = ('SELECT m.hash,m.subject,m.sender,m.date,m.message_id,m.reply_to '
                   'FROM search JOIN messages m ON m.hash=search.hash '
                   'WHERE search MATCH ? ORDER BY m.rowid DESC')
            params = (query,)
        else:
            sql = 'SELECT hash,subject,sender,date,message_id,reply_to FROM messages ORDER BY rowid DESC'
            params = ()
        rows = []
        try:
            cursor = aconn.execute(sql, params)
        except sqlite3.OperationalError:
            cursor = iter(())
        for r in cursor:
            if tagged is None or r[0] in tagged:
                rows.append(dict(zip(('hash', 'subject', 'sender', 'date', 'message_id', 'reply_to'), r)))
                if len(rows) >= limit_n:
                    break
    finally:
        aconn.close()
    return {'workspace': workspace, 'name': name, 'count': len(rows), 'results': rows}

def _with_research(root, action):
    """Run a research-overlay write through the documented Research API."""
    from .dispatch import Archive
    from .research import Research
    archive = Archive(root)
    research = Research(archive)
    try:
        return action(archive, research)
    finally:
        research.close()
        archive.db.close()

def _hash_list(value):
    if not isinstance(value, list) or not value:
        raise IPCError('INVALID_PARAMS', 'hashes must be a non-empty list')
    if not all(_valid_hash(h) for h in value):
        raise IPCError('INVALID_PARAMS', 'hashes must be 64-character lowercase hex strings')
    return list(dict.fromkeys(value))

def _text(value, name, limit=4096):
    if not isinstance(value, str):
        raise IPCError('INVALID_PARAMS', f'{name} must be a string')
    if len(value) > limit:
        raise IPCError('INVALID_PARAMS', f'{name} exceeds {limit} characters')
    return value

def _string_list(value, name, max_items, limit=200):
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if (not isinstance(value, list) or len(value) > max_items
            or not all(isinstance(item, str) for item in value)):
        raise IPCError('INVALID_PARAMS',
                       f'{name} must be a list of at most {max_items} strings')
    cleaned = [item.strip() for item in value if item.strip()]
    if any(len(item) > limit for item in cleaned):
        raise IPCError('INVALID_PARAMS',
                       f'{name} contains a value longer than {limit} characters')
    return cleaned or None

def _advanced_query_sources(value):
    sources = _string_list(value, 'sources', 3, 20)
    sources = sources or ['email', 'facebook']
    sources = list(dict.fromkeys(sources))
    if any(source not in ('email', 'facebook', 'document') for source in sources):
        raise IPCError('INVALID_PARAMS',
                       'sources must be email, facebook and/or document')
    return sources

def _advanced_queries_list(root, workspace=None):
    if workspace is not None:
        workspace = _text(workspace, 'workspace', 100)
    return _with_research(
        root, lambda a, r: {'queries': r.advanced_query_list(workspace)})

def _advanced_query_get(root, query_id):
    if not isinstance(query_id, int) or isinstance(query_id, bool) or query_id < 1:
        raise IPCError('INVALID_PARAMS', 'advanced query id must be a positive integer')
    return _with_research(root, lambda a, r: r.advanced_query_get(query_id))

def _advanced_query_save(root, params):
    from .advanced_search import normalize_query
    workspace = _text(params.get('workspace'), 'workspace', 100)
    name = _text(params.get('name'), 'name', 100)
    description = _text(params.get('description', ''), 'description', 4000)
    sources = _advanced_query_sources(params.get('sources'))
    try:
        tree = normalize_query(params.get('query'))
    except ValueError as exc:
        raise IPCError('INVALID_PARAMS', str(exc)) from None
    if tree.get('type') != 'group':
        raise IPCError('INVALID_PARAMS', 'a saved advanced query needs a group root')
    return _with_research(root, lambda a, r: r.advanced_query_save(
        workspace, name, description, tree, sources))

def _advanced_query_delete(root, query_id):
    if not isinstance(query_id, int) or isinstance(query_id, bool) or query_id < 1:
        raise IPCError('INVALID_PARAMS', 'advanced query id must be a positive integer')
    return _with_research(root, lambda a, r: r.advanced_query_delete(query_id))

def _research_workspace_save(root, workspace, description=''):
    return _with_research(root, lambda a, r: (
        r.workspace(_text(workspace, 'workspace', 100), _text(description, 'description', 2000)),
        {'workspace': workspace})[1])

def _research_search_save(root, params):
    workspace = _text(params.get('workspace'), 'workspace', 100)
    name = _text(params.get('name'), 'name', 100)
    query = _text(params.get('query', ''), 'query', 4096)
    tag = _text(params.get('tag', ''), 'tag', 100)
    limit = params.get('limit', 1000)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise IPCError('INVALID_PARAMS', 'limit must be an integer')

    def action(a, r):
        if not r.db.execute('SELECT 1 FROM workspaces WHERE name=?', (workspace,)).fetchone():
            raise IPCError('NOT_FOUND', 'unknown workspace')
        r.save_search(workspace, name, query, tag, limit)
        return {'workspace': workspace, 'name': name}
    return _with_research(root, action)

def _research_tag(root, hashes, tag, remove=False):
    targets = _hash_list(hashes)
    label = _text(tag, 'tag', 100)
    def action(a, r):
        for h in targets:
            (r.untag if remove else r.tag)(h, label)
        return {'tag': label, 'hashes': targets, 'removed': bool(remove)}
    return _with_research(root, action)

def _research_apply_rules(root):
    return _with_research(root, lambda a, r: {'matches': r.apply_rules()})

def _research_field_define(root, params):
    name = _text(params.get('name'), 'name', 100)
    kind = _text(params.get('kind'), 'kind', 20)
    choices = params.get('choices') or []
    if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
        raise IPCError('INVALID_PARAMS', 'choices must be a list of strings')
    return _with_research(root, lambda a, r: (
        r.define_field(name, kind, choices), {'name': name, 'kind': kind})[1])

def _research_metadata_set(root, digest, field, value):
    if not _valid_hash(digest):
        raise IPCError('INVALID_PARAMS', 'hash must be 64-character lowercase hex')
    name = _text(field, 'field', 100)
    text = _text(value, 'value', 4096)
    return _with_research(root, lambda a, r: (
        r.set_metadata(digest, name, text), {'hash': digest, 'field': name, 'value': text})[1])

def _research_report_save(root, params):
    name = _text(params.get('name'), 'name', 100)
    workspace = _text(params.get('workspace'), 'workspace', 100)
    search = _text(params.get('search'), 'search', 100)
    columns = params.get('columns')
    group_by = _text(params.get('group_by', '') or '', 'group_by', 20)
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise IPCError('INVALID_PARAMS', 'columns must be a list of strings')
    return _with_research(root, lambda a, r: (
        r.save_report(name, workspace, search, columns, group_by), {'name': name})[1])

def _research_report_preview(root, name):
    label = _text(name, 'name', 100)
    def action(a, r):
        columns, rows = r.report(label)
        return {'name': label, 'columns': columns, 'rows': rows}
    return _with_research(root, action)

def _research_report_export(root, name, path):
    label = _text(name, 'name', 100)
    destination = _text(path, 'path', 4096)
    def action(a, r):
        r.export_report(label, destination)
        return {'name': label, 'path': destination}
    return _with_research(root, action)

def _facebook_preview(root, paths):
    from .facebook import preview
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise IPCError('INVALID_PARAMS', 'paths must be a non-empty list of strings')
    try:
        return preview(paths, root)
    except OSError as exc:
        raise IPCError('IMPORT_FAILED', str(exc)) from None

def _journal_list(root, limit=100, include_deleted=False):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise IPCError('INVALID_PARAMS', 'limit must be an integer between 1 and 500')
    if not isinstance(include_deleted, bool):
        raise IPCError('INVALID_PARAMS', 'include_deleted must be a boolean')
    return _with_research(root, lambda a, r: {'entries': r.journal_list(limit, include_deleted)})

def _journal_get(root, entry_id):
    return _with_research(root, lambda a, r: r.journal_get(entry_id))

def _journal_save(root, params):
    entry_id = params.get('entry_id')
    body = params.get('body', '')
    event_date = params.get('event_date')
    if not isinstance(body, str):
        raise IPCError('INVALID_PARAMS', 'body must be text')
    if event_date is not None and not isinstance(event_date, str):
        raise IPCError('INVALID_PARAMS', 'event_date must be an ISO 8601 string or null')

    def action(archive, research):
        if entry_id is None:
            return research.journal_create(params.get('title'), body, event_date)
        if not isinstance(entry_id, int) or isinstance(entry_id, bool):
            raise IPCError('INVALID_PARAMS', 'entry_id must be an integer or null')
        return research.journal_update(entry_id, params.get('title'), body, event_date)

    return _with_research(root, action)

def _journal_delete(root, entry_id):
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise IPCError('INVALID_PARAMS', 'entry_id must be an integer')
    return _with_research(root, lambda a, r: r.journal_delete(entry_id))

def _journal_restore(root, entry_id):
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise IPCError('INVALID_PARAMS', 'entry_id must be an integer')
    return _with_research(root, lambda a, r: r.journal_restore(entry_id))

def _collections_list(root, workspace=None):
    if workspace is not None:
        workspace = _text(workspace, 'workspace', 100)
    return _with_research(root, lambda a, r: {'collections': r.collection_list(workspace)})

def _collection_get(root, collection_id):
    return _with_research(root, lambda a, r: r.collection_get(collection_id))

def _collection_create(root, params):
    workspace = _text(params.get('workspace'), 'workspace', 100)
    name = _text(params.get('name'), 'name', 100)
    description = params.get('description', '')
    if not isinstance(description, str):
        raise IPCError('INVALID_PARAMS', 'description must be a string')
    return _with_research(root, lambda a, r: r.collection_create(workspace, name, description))

def _collection_delete(root, collection_id):
    return _with_research(root, lambda a, r: r.collection_delete(collection_id))

def _collection_add(root, params):
    collection_id = params.get('collection_id')
    if not isinstance(collection_id, int) or isinstance(collection_id, bool):
        raise IPCError('INVALID_PARAMS', 'collection_id must be an integer')
    item_type = _text(params.get('item_type'), 'item_type', 20)
    item_id = _text(params.get('item_id'), 'item_id', 500)
    note = params.get('note')
    if note is not None and not isinstance(note, str):
        raise IPCError('INVALID_PARAMS', 'note must be a string or null')
    return _with_research(root, lambda a, r: r.collection_add(
        collection_id, item_type, item_id, note))

def _collection_remove(root, params):
    collection_id = params.get('collection_id')
    if not isinstance(collection_id, int) or isinstance(collection_id, bool):
        raise IPCError('INVALID_PARAMS', 'collection_id must be an integer')
    item_type = _text(params.get('item_type'), 'item_type', 20)
    item_id = _text(params.get('item_id'), 'item_id', 500)
    return _with_research(root, lambda a, r: r.collection_remove(
        collection_id, item_type, item_id))

def _collection_export(root, params):
    from .reporting import (COLLECTION_EXTENSIONS, build_collection_manifest,
                           write_collection_manifest)
    collection_id = params.get('collection_id')
    if not isinstance(collection_id, int) or isinstance(collection_id, bool):
        raise IPCError('INVALID_PARAMS', 'collection_id must be an integer')
    destination = params.get('destination')
    fmt = params.get('format', 'markdown')
    if not isinstance(destination, str) or not destination.strip():
        raise IPCError('INVALID_PARAMS', 'destination is required')
    if fmt not in COLLECTION_EXTENSIONS:
        raise IPCError('INVALID_PARAMS', 'format must be markdown or json')
    root_path = Path(root).expanduser().resolve()
    dest = Path(destination).expanduser().resolve()
    if dest.is_relative_to(root_path) or root_path.is_relative_to(dest):
        raise IPCError('INVALID_PARAMS', 'A manifest must be written outside the archive')
    if not dest.parent.is_dir():
        raise IPCError('INVALID_PARAMS', 'Destination folder does not exist')
    if dest.suffix.lower() != COLLECTION_EXTENSIONS[fmt]:
        raise IPCError('INVALID_PARAMS',
                       f'{fmt} manifests use the {COLLECTION_EXTENSIONS[fmt]} extension')
    if dest.exists():
        raise IPCError('DESTINATION_EXISTS', 'Destination already exists')

    def action(archive, research):
        manifest = build_collection_manifest(research.collection_get(collection_id))
        try:
            return write_collection_manifest(manifest, fmt, dest)
        except FileExistsError:
            raise IPCError('DESTINATION_EXISTS', 'Destination already exists') from None

    return _with_research(root, action)


def _draft_list(root, collection_id=None):
    if collection_id is not None and (not isinstance(collection_id, int)
                                      or isinstance(collection_id, bool)):
        raise IPCError('INVALID_PARAMS', 'collection_id must be an integer or null')
    return _with_research(root, lambda a, r: {'drafts': r.draft_list(collection_id)})

def _draft_get(root, draft_id):
    return _with_research(root, lambda a, r: r.draft_get(draft_id))

def _draft_save(root, params):
    collection_id = params.get('collection_id')
    if not isinstance(collection_id, int) or isinstance(collection_id, bool):
        raise IPCError('INVALID_PARAMS', 'collection_id must be an integer')
    title = _text(params.get('title'), 'title', 200)
    notes = params.get('notes', '')
    if not isinstance(notes, str):
        raise IPCError('INVALID_PARAMS', 'notes must be a string')
    draft_id = params.get('draft_id')

    def action(archive, research):
        if draft_id is None:
            return research.draft_create(collection_id, title, notes)
        if not isinstance(draft_id, int) or isinstance(draft_id, bool):
            raise IPCError('INVALID_PARAMS', 'draft_id must be an integer or null')
        return research.draft_update(draft_id, title, notes)

    return _with_research(root, action)

def _draft_delete(root, draft_id):
    return _with_research(root, lambda a, r: r.draft_delete(draft_id))

def _draft_export(root, params):
    from .reporting import DRAFT_EXTENSIONS, build_draft_report, write_draft_report
    draft_id = params.get('draft_id')
    if not isinstance(draft_id, int) or isinstance(draft_id, bool):
        raise IPCError('INVALID_PARAMS', 'draft_id must be an integer')
    destination = params.get('destination')
    fmt = params.get('format', 'markdown')
    if not isinstance(destination, str) or not destination.strip():
        raise IPCError('INVALID_PARAMS', 'destination is required')
    if fmt not in DRAFT_EXTENSIONS:
        raise IPCError('INVALID_PARAMS', 'format must be markdown, json or html')
    root_path = Path(root).expanduser().resolve()
    dest = Path(destination).expanduser().resolve()
    if dest.is_relative_to(root_path) or root_path.is_relative_to(dest):
        raise IPCError('INVALID_PARAMS', 'A draft must be written outside the archive')
    if not dest.parent.is_dir():
        raise IPCError('INVALID_PARAMS', 'Destination folder does not exist')
    if dest.suffix.lower() != DRAFT_EXTENSIONS[fmt]:
        raise IPCError('INVALID_PARAMS',
                       f'{fmt} drafts use the {DRAFT_EXTENSIONS[fmt]} extension')
    if dest.exists():
        raise IPCError('DESTINATION_EXISTS', 'Destination already exists')

    def action(archive, research):
        report = build_draft_report(research.draft_get(draft_id))
        try:
            return write_draft_report(report, fmt, dest)
        except FileExistsError:
            raise IPCError('DESTINATION_EXISTS', 'Destination already exists') from None

    return _with_research(root, action)

def _export_evidence(root, hashes, destination):
    from .dispatch import Archive
    targets = _hash_list(hashes)
    dest = _text(destination, 'destination', 4096)
    archive = Archive(root)
    try:
        archive.export(targets, dest)
    finally:
        archive.db.close()
    return {'destination': dest, 'messages': len(targets)}

def _schedule_status(root):
    from .schedule import config, read_json
    base = Path(root)
    return {'profiles': config(base), 'status': read_json(base / 'schedule-status.json', {})}

def _schedule_profile_save(root, params):
    from .schedule import profile, config, write_json
    payload = {k: params.get(k) for k in ('name', 'host', 'user', 'folders', 'minutes', 'daily', 'enabled')}
    if not isinstance(payload.get('folders'), list):
        raise IPCError('INVALID_PARAMS', 'folders must be a list')
    item = profile(payload['name'], payload['host'], payload['user'], payload['folders'],
                   payload.get('minutes'), payload.get('daily'),
                   bool(payload.get('enabled', False)))
    base = Path(root)
    profiles = config(base)
    profiles[item['name']] = item
    write_json(base / 'schedule.json', profiles)
    return {'profile': item}

def _schedule_profile_enabled(root, name, enabled):
    from .schedule import config, write_json
    label = _text(name, 'name', 40)
    base = Path(root)
    profiles = config(base)
    if label not in profiles:
        raise IPCError('NOT_FOUND', 'unknown schedule profile')
    profiles[label]['enabled'] = bool(enabled)
    write_json(base / 'schedule.json', profiles)
    return {'name': label, 'enabled': bool(enabled)}

def _schedule_run(root, name=None):
    from .schedule import tick
    base = Path(root)
    if name is not None:
        from .schedule import config
        if _text(name, 'name', 40) not in config(base):
            raise IPCError('NOT_FOUND', 'unknown schedule profile')
    return tick(base, name)

class IPCError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

class IPCServer:
    def __init__(self, root, out=None, err=None):
        self.root = Path(root).expanduser()
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self._jobs = {}
        self._lock = threading.Lock()

    def _write(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self.out.write(line + '\n')
            self.out.flush()

    def _catalog_stats(self):
        from .catalog import Catalog
        catalog = Catalog(self.root)
        try:
            return catalog.stats()
        finally:
            catalog.close()

    def _search_spec(self, params):
        from .unified import CATALOG_KINDS
        query = params.get('query')
        if not isinstance(query, str) or not query.strip():
            raise IPCError('INVALID_PARAMS', 'search requires a non-empty query string')
        sources = params.get('sources', ('email', 'facebook', 'document'))
        if isinstance(sources, str):
            sources = tuple(x.strip() for x in sources.split(',') if x.strip())
        sources = tuple(sources) or ('email', 'facebook', 'document')
        if not all(s in ('email', 'facebook', 'document') for s in sources):
            raise IPCError('INVALID_PARAMS',
                           'sources must be email, facebook and/or document')
        page = params.get('page', 1)
        page_size = params.get('page_size', 20)
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise IPCError('INVALID_PARAMS', 'page must be a positive integer')
        if not isinstance(page_size, int) or isinstance(page_size, bool) \
                or not 1 <= page_size <= 1000:
            raise IPCError('INVALID_PARAMS', 'page_size must be an integer between 1 and 1000')
        if (page - 1) * page_size > 10_000_000:
            raise IPCError('INVALID_PARAMS', 'page offset must not exceed 10000000 records')
        after = params.get('after')
        before = params.get('before')
        for name, value in (('after', after), ('before', before)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise IPCError('INVALID_PARAMS', f'{name} must be a UTC epoch integer')
        senders = _string_list(params.get('senders'), 'senders', 20, 200)
        kinds = _string_list(params.get('kinds'), 'kinds', len(CATALOG_KINDS) + 1, 20)
        if kinds:
            allowed = set(CATALOG_KINDS) | {'message'}
            if any(kind not in allowed for kind in kinds):
                raise IPCError('INVALID_PARAMS', 'kinds contains an unsupported record kind')
        tags = _string_list(params.get('tags'), 'tags', 20, 100)
        return {'query': query, 'sources': sources, 'page': page,
                'page_size': page_size, 'after': after, 'before': before,
                'senders': senders, 'kinds': kinds, 'tags': tags}

    def _search_page(self, spec, cancel=None):
        from .unified import search_unified_page, search_unified_count
        offset = (spec['page'] - 1) * spec['page_size']
        exclude_disabled = _hide_disabled_sources(self.root)
        result = search_unified_page(
            self.root, spec['query'], offset=offset, limit=spec['page_size'],
            sources=spec['sources'], after=spec['after'], before=spec['before'],
            senders=spec['senders'], kinds=spec['kinds'], tags=spec['tags'],
            exclude_disabled=exclude_disabled, cancel=cancel)
        items = result['items']
        if result['cancelled']:
            return {'items': items, 'page': spec['page'], 'page_size': spec['page_size'],
                    'has_more': True, 'total': offset + len(items),
                    'total_is_exact': False, 'capped': False, 'cancelled': True}
        exact = search_unified_count(
            self.root, spec['query'], sources=spec['sources'], after=spec['after'],
            before=spec['before'], senders=spec['senders'], kinds=spec['kinds'],
            tags=spec['tags'], exclude_disabled=exclude_disabled)
        total = exact if exact is not None else offset + len(items)
        has_more = result['has_more'] if exact is None else exact > offset + len(items)
        return {'items': items, 'page': spec['page'], 'page_size': spec['page_size'],
                'has_more': has_more, 'total': total,
                'total_is_exact': exact is not None, 'capped': False,
                'cancelled': False}

    def _search(self, params):
        return self._search_page(self._search_spec(params))

    def _start_search(self, params):
        spec = self._search_spec(params)

        def work(job_id, cancel_event):
            return self._search_page(spec, cancel_event)

        return self._start_job(work)

    def _research_spec(self, params):
        from .intelligence import ConversationStore, normalize_request
        request = params.get('request', params)
        spec = normalize_request(request)
        if spec['conversation_id'] is not None:
            store = ConversationStore(self.root)
            try:
                store.get(spec['conversation_id'])
                if not spec['history']:
                    spec['history'] = store.history(spec['conversation_id'])
            finally:
                store.close()
        return spec

    def _intelligence_status(self):
        from .preferences import load as load_prefs
        prefs = load_prefs(self.root)
        return {'enabled': bool(prefs['ai_enabled']) and prefs['ai_backend'] == 'ollama',
                'endpoint': prefs['ai_endpoint'], 'model': prefs['ai_model'],
                'timeout': prefs['ai_timeout'],
                'max_input_chars': prefs['ai_max_input_chars']}

    def _intelligence_retrieve(self, params):
        from .intelligence import Intelligence
        return Intelligence(self.root).retrieve(self._research_spec(params))

    def _start_intelligence_models(self):
        from .intelligence import OllamaClient

        def work(job_id, cancel_event):
            return OllamaClient(self.root).status()

        return self._start_job(work)

    def _start_intelligence_ask(self, params):
        from .intelligence import ConversationStore, Intelligence
        spec = self._research_spec(params)

        def work(job_id, cancel_event):
            def progress(stage):
                self._write(_event('progress', job_id=job_id, done=0, total=0,
                                   hash='', status=stage))

            result = Intelligence(self.root).ask(spec, cancel_event, progress)
            conversation_id = spec['conversation_id']
            if conversation_id is not None and not result.get('cancelled'):
                store = ConversationStore(self.root)
                try:
                    store.append(
                        conversation_id, 'user', spec['question'],
                        request={key: value for key, value in spec.items()
                                 if key != 'history'})
                    if result.get('answer'):
                        store.append(
                            conversation_id, 'assistant', result['answer'],
                            answer=result, model=result['model']['name'])
                finally:
                    store.close()
            return result

        return self._start_job(work)

    def _conversation_list(self, params):
        from .intelligence import ConversationStore
        workspace = params.get('workspace')
        if workspace is not None and (not isinstance(workspace, str) or not workspace.strip()):
            raise IPCError('INVALID_PARAMS', 'workspace must be a non-empty string')
        store = ConversationStore(self.root)
        try:
            return {'conversations': store.list(workspace)}
        finally:
            store.close()

    def _conversation_create(self, params):
        from .intelligence import ConversationStore
        store = ConversationStore(self.root)
        try:
            return store.create(params.get('workspace', 'General'),
                                params.get('title', 'Untitled research'))
        finally:
            store.close()

    def _conversation_get(self, params):
        from .intelligence import ConversationStore
        store = ConversationStore(self.root)
        try:
            return store.get(params.get('conversation_id'))
        finally:
            store.close()

    def _conversation_delete(self, params):
        from .intelligence import ConversationStore
        store = ConversationStore(self.root)
        try:
            return store.delete(params.get('conversation_id'))
        finally:
            store.close()

    def _conversation_export(self, params):
        from .intelligence import ConversationStore
        store = ConversationStore(self.root)
        try:
            data = store.export(params.get('conversation_id'))
        finally:
            store.close()
        destination = params.get('path')
        if destination is None:
            return {'conversation': data}
        if not isinstance(destination, str) or not destination.strip():
            raise IPCError('INVALID_PARAMS', 'path must be a non-empty string')
        path = Path(destination).expanduser()
        root = Path(self.root).resolve()
        try:
            path.resolve().relative_to(root)
        except ValueError:
            pass
        else:
            raise IPCError('INVALID_PARAMS', 'Export destination must be outside the archive')
        if path.exists():
            raise IPCError('INVALID_PARAMS', 'Export destination already exists')
        if not path.parent.is_dir():
            raise IPCError('INVALID_PARAMS', 'Export directory does not exist')
        try:
            path.write_text(json.dumps({'version': 1, 'conversation': data},
                                       indent=2, ensure_ascii=False), encoding='utf-8')
        except OSError as exc:
            raise IPCError('EXPORT_FAILED', str(exc)) from None
        return {'path': str(path), 'turns': len(data['turns'])}

    def _advanced_search(self, params):
        from .advanced_search import search_advanced
        if 'query' not in params:
            raise IPCError('INVALID_PARAMS', 'search_advanced requires a query tree')
        sources = params.get('sources', ('email', 'facebook'))
        if isinstance(sources, str):
            sources = tuple(x.strip() for x in sources.split(',') if x.strip())
        sources = tuple(sources) or ('email', 'facebook')
        if not all(source in ('email', 'facebook', 'document') for source in sources):
            raise IPCError('INVALID_PARAMS',
                           'sources must be email, facebook and/or document')
        limit = params.get('limit', 100)
        offset = params.get('offset', 0)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
            raise IPCError('INVALID_PARAMS', 'limit must be an integer between 1 and 5000')
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise IPCError('INVALID_PARAMS', 'offset must be a non-negative integer')
        return search_advanced(self.root, params['query'], limit, offset, sources)

    def _start_job(self, work):
        """Run work(cancel_event) on a daemon thread; emit job_complete/job_error."""
        job_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        job = {'id': job_id, 'cancel_event': cancel_event, 'done': False}
        self._jobs[job_id] = job

        def runner():
            try:
                result = work(job_id, cancel_event)
                self._write(_event('job_complete', job_id=job_id, result=result))
            except IPCError as exc:
                self._write(_event('job_error', job_id=job_id, code=exc.code, message=str(exc)))
            except Exception as exc:
                self._write(_event('job_error', job_id=job_id, code='INTERNAL_ERROR',
                                   message=type(exc).__name__ + ': ' + str(exc)))
            finally:
                job['done'] = True

        threading.Thread(target=runner, daemon=True).start()
        return {'job_id': job_id, 'status': 'started'}

    def _start_summarize(self, params):
        from .preferences import load as load_prefs
        from .summarize import Summarizer, OllamaError
        prefs = load_prefs(self.root)
        if not (prefs.get('ai_enabled') and prefs.get('ai_backend') == 'ollama'):
            raise IPCError('AI_DISABLED', 'AI is disabled; enable it before summarizing')
        hashes = params.get('hashes')
        if hashes is not None and (not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes)):
            raise IPCError('INVALID_PARAMS', 'hashes must be a list of strings')
        summary_type = params.get('summary_type', 'message')
        if summary_type not in ('message', 'thread'):
            raise IPCError('INVALID_PARAMS', 'summary_type must be message or thread')

        def work(job_id, cancel_event):
            summarizer = Summarizer(self.root)
            try:
                if not summarizer.enabled():
                    raise IPCError('AI_DISABLED', 'AI is disabled; enable it before summarizing')

                def progress(done, total, digest, status):
                    self._write(_event('progress', job_id=job_id, done=done, total=total,
                                       hash=digest, status=status))

                try:
                    if hashes is not None:
                        return summarizer.summarize_batch(
                            hashes, summary_type, model=params.get('model'),
                            prompt_version=params.get('prompt_version'),
                            progress=progress, cancel_event=cancel_event)
                    return summarizer.summarize_all(
                        summary_type, model=params.get('model'),
                        prompt_version=params.get('prompt_version'),
                        progress=progress, cancel_event=cancel_event)
                except OllamaError as exc:
                    raise IPCError('OLLAMA_ERROR', str(exc)) from None
            finally:
                summarizer.close()

        return self._start_job(work)

    def _start_schedule_run(self, params):
        from .schedule import config
        name = params.get('name')
        if name is not None:
            if not isinstance(name, str) or name not in config(self.root):
                raise IPCError('NOT_FOUND', 'unknown schedule profile')

        def work(job_id, cancel_event):
            self._write(_event('progress', job_id=job_id, done=0, total=1, hash='', status='syncing'))
            result = _schedule_run(self.root, name)
            self._write(_event('progress', job_id=job_id, done=1, total=1, hash='', status='complete'))
            return result

        return self._start_job(work)

    def _start_repair(self, action):
        from .dispatch import Archive
        from .migrations import MigrationError
        from .sync_lock import SyncBusy

        def work(job_id, cancel_event):
            archive = None
            try:
                archive = Archive(self.root)

                def progress(done, total, digest, status):
                    self._write(_event('progress', job_id=job_id, done=done, total=total,
                                       hash=digest, status=status))
                try:
                    if action == 'reindex':
                        count = archive.reindex(progress=progress, cancel_event=cancel_event)
                        return {'action': action, 'reindexed': count,
                                'cancelled': cancel_event.is_set()}
                    if action == 'catalog_reindex':
                        from .catalog import Catalog
                        catalog = Catalog(self.root)
                        try:
                            indexed = catalog.reindex()
                        finally:
                            catalog.close()
                        return {'action': action, 'records_reindexed': indexed,
                                'cancelled': cancel_event.is_set()}
                    recovered = archive.recover_orphans(progress=progress, cancel_event=cancel_event)
                    return {'action': action, 'recovered': recovered,
                            'cancelled': cancel_event.is_set()}
                except SyncBusy as exc:
                    raise IPCError('ARCHIVE_BUSY', str(exc)) from None
            except MigrationError as exc:
                raise IPCError('MIGRATION_ERROR', str(exc)) from None
            finally:
                if archive is not None:
                    archive.db.close()

        return self._start_job(work)

    def _start_backup_action(self, action, params):
        from . import backup as backup_engine
        field = 'destination' if action == 'backup' else 'snapshot'
        path = params.get(field)
        if not isinstance(path, str) or not path.strip():
            raise IPCError('INVALID_PARAMS', f'{field} is required')
        if action == 'restore':
            destination = params.get('destination')
            if not isinstance(destination, str) or not destination.strip():
                raise IPCError('INVALID_PARAMS', 'destination is required')

        def work(job_id, cancel_event):
            self._write(_event('progress', job_id=job_id, done=0, total=1, hash='', status=action))
            try:
                key = session_key(self.root)
                if action == 'backup':
                    result = backup_engine.backup(self.root, path)
                elif action == 'verify_backup':
                    snapshot = Path(path).expanduser()
                    if backup_engine.is_encrypted_archive(snapshot) and key is None:
                        raise ValueError('Unlock the archive before verifying its encrypted backup')
                    result = backup_engine.validate_snapshot(snapshot, key=key)
                else:
                    snapshot = Path(path).expanduser()
                    if backup_engine.is_encrypted_archive(snapshot) and key is None:
                        raise ValueError('Unlock the archive before restoring its encrypted backup')
                    result = backup_engine.restore(snapshot, params['destination'], key=key)
            except (ValueError, FileExistsError, OSError, sqlite3.Error) as exc:
                code = {'backup': 'BACKUP_FAILED', 'verify_backup': 'BACKUP_INVALID',
                        'restore': 'RESTORE_FAILED'}[action]
                raise IPCError(code, str(exc)) from None
            except Exception as exc:
                code = {'backup': 'BACKUP_FAILED', 'verify_backup': 'BACKUP_INVALID',
                        'restore': 'RESTORE_FAILED'}[action]
                raise IPCError(code, type(exc).__name__ + ': ' + str(exc)) from None
            self._write(_event('progress', job_id=job_id, done=1, total=1, hash='', status='complete'))
            return result

        return self._start_job(work)

    def _start_source_verify(self, params):
        from .dispatch import Archive
        source, key = _source_param(params)

        def work(job_id, cancel_event):
            archive = None
            try:
                archive = Archive(self.root)

                def progress(done, total, item, status):
                    self._write(_event('progress', job_id=job_id, done=done, total=total,
                                       hash='', status=f'{status} {item}'))

                return archive.verify_source(source, key, progress=progress,
                                             cancel_event=cancel_event)
            finally:
                if archive is not None:
                    archive.db.close()

        return self._start_job(work)

    def _start_report(self, params):
        from .reporting import build_archive_report, write_report
        destination = params.get('destination')
        fmt = params.get('format', 'markdown')
        if not isinstance(destination, str) or not destination.strip():
            raise IPCError('INVALID_PARAMS', 'destination is required')
        if fmt not in ('markdown', 'json', 'html'):
            raise IPCError('INVALID_PARAMS', 'format must be markdown, json or html')
        root = Path(self.root).expanduser().resolve()
        dest = Path(destination).expanduser().resolve()
        from .reporting import EXTENSIONS
        if dest.is_relative_to(root) or root.is_relative_to(dest):
            raise IPCError('INVALID_PARAMS', 'A report must be written outside the archive')
        if not dest.parent.is_dir():
            raise IPCError('INVALID_PARAMS', 'Destination folder does not exist')
        if dest.suffix.lower() != EXTENSIONS[fmt]:
            raise IPCError('INVALID_PARAMS',
                           f'{fmt} reports use the {EXTENSIONS[fmt]} extension')
        if dest.exists():
            raise IPCError('DESTINATION_EXISTS', 'Destination already exists')

        def work(job_id, cancel_event):
            self._write(_event('progress', job_id=job_id, done=0, total=1,
                               hash='', status='collecting'))
            sources = _sources(self.root)
            dashboard = _dashboard(self.root)
            health = _health(self.root)
            history = _import_history(self.root, 20)
            if cancel_event.is_set():
                return {'cancelled': True, 'path': str(dest), 'format': fmt}
            report = build_archive_report(sources, dashboard, health, history)
            try:
                result = write_report(report, fmt, dest)
            except FileExistsError:
                raise IPCError('DESTINATION_EXISTS', 'Destination already exists') from None
            self._write(_event('progress', job_id=job_id, done=1, total=1,
                               hash='', status='complete'))
            return result

        return self._start_job(work)

    def _start_eml_import(self, params):
        from .dispatch import Archive
        from .migrations import MigrationError
        from .sync_lock import SyncBusy
        paths = params.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise IPCError('INVALID_PARAMS', 'paths must be a non-empty list of strings')

        def work(job_id, cancel_event):
            archive = None
            try:
                archive = Archive(self.root)

                def progress(done, total, path, status):
                    self._write(_event('progress', job_id=job_id, done=done, total=total,
                                       hash='', status=f'{status} {path}'))

                return archive.import_files(paths, progress=progress, cancel_event=cancel_event)
            except SyncBusy as exc:
                raise IPCError('ARCHIVE_BUSY', str(exc)) from None
            except MigrationError as exc:
                raise IPCError('MIGRATION_ERROR', str(exc)) from None
            finally:
                if archive is not None:
                    archive.db.close()

        return self._start_job(work)

    def _document_preview(self, params):
        from .documents import preview
        paths = params.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise IPCError('INVALID_PARAMS', 'paths must be a non-empty list of strings')
        return preview(paths)

    def _start_document_import(self, params):
        from .documents import import_files
        paths = params.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise IPCError('INVALID_PARAMS', 'paths must be a non-empty list of strings')

        def work(job_id, cancel_event):
            def progress(done, total, path, status):
                self._write(_event('progress', job_id=job_id, done=done, total=total,
                                   hash='', status=f'{status} {path}'))
            try:
                return import_files(paths, self.root, progress=progress,
                                    cancel_event=cancel_event)
            except OSError as exc:
                raise IPCError('IMPORT_FAILED', str(exc)) from None

        return self._start_job(work)

    def _start_facebook_import(self, params):
        from .facebook import import_files
        paths = params.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise IPCError('INVALID_PARAMS', 'paths must be a non-empty list of strings')

        def work(job_id, cancel_event):
            def progress(done, total, path, status):
                self._write(_event('progress', job_id=job_id, done=done, total=total,
                                   hash='', status=f'{status} {path}'))
            try:
                result = import_files(paths, self.root, progress=progress,
                                      cancel_event=cancel_event)
            except OSError as exc:
                raise IPCError('IMPORT_FAILED', str(exc)) from None
            # History is best-effort: a completed import must not fail merely
            # because its run could not be recorded, but the result says so.
            from .dispatch import Archive
            archive = None
            try:
                archive = Archive(self.root)
                run = archive.begin_import_run('facebook', ', '.join(paths)[:200])
                archive.finish_import_run(
                    run, 'cancelled' if result.get('cancelled') else 'ok',
                    files_seen=result.get('files_discovered'),
                    records_seen=result.get('records_encountered'),
                    records_new=result.get('records_imported'),
                    records_existing=result.get('records_duplicate'),
                    records_failed=result.get('parse_errors') + result.get('files_unsupported', 0))
                result['run_id'] = run
            except Exception as exc:
                result['history_error'] = type(exc).__name__ + ': ' + str(exc)
            finally:
                if archive is not None:
                    archive.db.close()
            return result

        return self._start_job(work)

    def _cancel(self, params):
        job_id = params.get('job_id')
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            raise IPCError('UNKNOWN_JOB', 'no such job')
        job['cancel_event'].set()
        return {'job_id': job_id, 'status': 'cancelling'}

    def handle(self, request):
        """Validate and dispatch one request. Returns a response dict or None for shutdown."""
        if not isinstance(request, dict):
            return _error(None, 'INVALID_REQUEST', 'request must be an object')
        if request.get('protocol_version') != PROTOCOL_VERSION:
            return _error(request.get('request_id'), 'UNSUPPORTED_VERSION',
                          f'supported protocol_version is {PROTOCOL_VERSION}')
        request_id = request.get('request_id')
        method = request.get('method')
        params = request.get('params', {}) if isinstance(request.get('params'), dict) else {}
        try:
            if method == 'ping':
                return _result(request_id, {'pong': True, 'protocol_version': PROTOCOL_VERSION})
            if method == 'encryption_status':
                return _result(request_id, _encryption_status(self.root))
            if method == 'encryption_unlock':
                return _result(request_id, _encryption_unlock(self.root, params))
            if method == 'encryption_lock':
                if any(not job['done'] for job in self._jobs.values()):
                    raise IPCError('ARCHIVE_BUSY',
                                   'cancel active jobs before locking the archive')
                return _result(request_id, _encryption_lock(self.root))
            if method == 'archive_status':
                return _result(request_id, _archive_status(self.root))
            if method == 'acknowledge_boundary_notice':
                return _result(request_id, _acknowledge_boundary_notice(self.root))
            if method == 'entities':
                return _result(request_id, _entities(self.root, params))
            if method == 'create_archive':
                path = params.get('path')
                if not isinstance(path, str) or not path.strip():
                    raise IPCError('INVALID_PARAMS',
                                   'create_archive requires a destination path')
                return _result(request_id, _create_archive(path))
            if method == 'stats':
                return _result(request_id, {'archive': _archive_stats(self.root),
                                            'catalog': self._catalog_stats()})
            if method == 'search':
                return _result(request_id, self._search(params))
            if method == 'search_job':
                return _result(request_id, self._start_search(params))
            if method == 'intelligence_status':
                return _result(request_id, self._intelligence_status())
            if method == 'intelligence_models':
                return _result(request_id, self._start_intelligence_models())
            if method == 'intelligence_retrieve':
                return _result(request_id, self._intelligence_retrieve(params))
            if method == 'intelligence_ask':
                return _result(request_id, self._start_intelligence_ask(params))
            if method == 'conversation_list':
                return _result(request_id, self._conversation_list(params))
            if method == 'conversation_create':
                return _result(request_id, self._conversation_create(params))
            if method == 'conversation_get':
                return _result(request_id, self._conversation_get(params))
            if method == 'conversation_delete':
                return _result(request_id, self._conversation_delete(params))
            if method == 'conversation_export':
                return _result(request_id, self._conversation_export(params))
            if method == 'search_advanced':
                return _result(request_id, self._advanced_search(params))
            if method == 'advanced_queries_list':
                return _result(request_id, _advanced_queries_list(
                    self.root, params.get('workspace')))
            if method == 'advanced_query_get':
                return _result(request_id, _advanced_query_get(
                    self.root, params.get('query_id')))
            if method == 'advanced_query_save':
                return _result(request_id, _advanced_query_save(self.root, params))
            if method == 'advanced_query_delete':
                return _result(request_id, _advanced_query_delete(
                    self.root, params.get('query_id')))
            if method == 'search_validate':
                from .unified import validate_query
                return _result(request_id, validate_query(
                    self.root, params.get('query')))
            if method == 'message':
                return _result(request_id, _message(self.root, params.get('hash')))
            if method == 'thread':
                return _result(request_id, _thread(self.root, params.get('hash')))
            if method == 'timeline':
                return _result(request_id, _timeline(
                    self.root, params.get('sender'), params.get('limit', 100),
                    params.get('offset', 0), params.get('order', 'asc'),
                    params.get('after'), params.get('before'), params.get('sources')))
            if method == 'dashboard':
                return _result(request_id, _dashboard(self.root))
            if method == 'correspondents':
                return _result(request_id, _correspondents(self.root, params.get('limit', 100)))
            if method == 'relationships':
                return _result(request_id, _relationships(
                    self.root, params.get('limit_nodes', 30), params.get('max_edges', 200),
                    params.get('after'), params.get('before')))
            if method == 'record':
                return _result(request_id, _record(self.root, params.get('record_id')))
            if method == 'verify':
                return _result(request_id, _verify(self.root))
            if method == 'health':
                return _result(request_id, _health(self.root))
            if method == 'migrate_status':
                return _result(request_id, _migration_report(self.root))
            if method == 'reindex':
                return _result(request_id, self._start_repair('reindex'))
            if method == 'catalog_reindex':
                return _result(request_id, self._start_repair('catalog_reindex'))
            if method == 'recover_orphans':
                return _result(request_id, self._start_repair('recover'))
            if method == 'backup':
                return _result(request_id, self._start_backup_action('backup', params))
            if method == 'verify_backup':
                return _result(request_id, self._start_backup_action('verify_backup', params))
            if method == 'restore':
                return _result(request_id, self._start_backup_action('restore', params))
            if method == 'facebook_preview':
                return _result(request_id, _facebook_preview(self.root, params.get('paths')))
            if method == 'facebook_import':
                return _result(request_id, self._start_facebook_import(params))
            if method == 'document_preview':
                return _result(request_id, self._document_preview(params))
            if method == 'document_import':
                return _result(request_id, self._start_document_import(params))
            if method == 'import_eml':
                return _result(request_id, self._start_eml_import(params))
            if method == 'import_history':
                return _result(request_id, _import_history(self.root, params.get('limit', 50)))
            if method == 'sources':
                return _result(request_id, _sources(self.root))
            if method == 'source_set_enabled':
                return _result(request_id, _source_set_enabled(self.root, params))
            if method == 'source_removal_preview':
                return _result(request_id, _source_preview(self.root, params))
            if method == 'source_remove':
                return _result(request_id, _source_remove(self.root, params))
            if method == 'source_events':
                return _result(request_id, _source_events(
                    self.root, params.get('limit', 100)))
            if method == 'source_verify':
                return _result(request_id, self._start_source_verify(params))
            if method == 'report_archive':
                return _result(request_id, self._start_report(params))
            if method == 'preferences':
                return _result(request_id, _preferences(self.root))
            if method == 'preferences_set':
                return _result(request_id, _preferences_set(self.root, params.get('values')))
            if method == 'research_list':
                return _result(request_id, _research_list(self.root, params.get('table')))
            if method == 'research_results':
                return _result(request_id, _research_results(self.root, params.get('workspace'), params.get('name')))
            if method == 'research_workspace_save':
                return _result(request_id, _research_workspace_save(
                    self.root, params.get('workspace'), params.get('description', '')))
            if method == 'research_search_save':
                return _result(request_id, _research_search_save(self.root, params))
            if method == 'research_tag':
                return _result(request_id, _research_tag(self.root, params.get('hashes'), params.get('tag')))
            if method == 'research_untag':
                return _result(request_id, _research_tag(
                    self.root, params.get('hashes'), params.get('tag'), remove=True))
            if method == 'research_apply_rules':
                return _result(request_id, _research_apply_rules(self.root))
            if method == 'research_field_define':
                return _result(request_id, _research_field_define(self.root, params))
            if method == 'research_metadata_set':
                return _result(request_id, _research_metadata_set(
                    self.root, params.get('hash'), params.get('field'), params.get('value')))
            if method == 'research_report_save':
                return _result(request_id, _research_report_save(self.root, params))
            if method == 'research_report_preview':
                return _result(request_id, _research_report_preview(self.root, params.get('name')))
            if method == 'research_report_export':
                return _result(request_id, _research_report_export(
                    self.root, params.get('name'), params.get('path')))
            if method == 'journal_list':
                return _result(request_id, _journal_list(
                    self.root, params.get('limit', 100), params.get('include_deleted', False)))
            if method == 'journal_get':
                return _result(request_id, _journal_get(self.root, params.get('entry_id')))
            if method == 'journal_save':
                return _result(request_id, _journal_save(self.root, params))
            if method == 'journal_delete':
                return _result(request_id, _journal_delete(self.root, params.get('entry_id')))
            if method == 'journal_restore':
                return _result(request_id, _journal_restore(self.root, params.get('entry_id')))
            if method == 'collections_list':
                return _result(request_id, _collections_list(
                    self.root, params.get('workspace')))
            if method == 'collection_get':
                return _result(request_id, _collection_get(
                    self.root, params.get('collection_id')))
            if method == 'collection_create':
                return _result(request_id, _collection_create(self.root, params))
            if method == 'collection_delete':
                return _result(request_id, _collection_delete(
                    self.root, params.get('collection_id')))
            if method == 'collection_add':
                return _result(request_id, _collection_add(self.root, params))
            if method == 'collection_remove':
                return _result(request_id, _collection_remove(self.root, params))
            if method == 'collection_export':
                return _result(request_id, _collection_export(self.root, params))
            if method == 'draft_list':
                return _result(request_id, _draft_list(
                    self.root, params.get('collection_id')))
            if method == 'draft_get':
                return _result(request_id, _draft_get(self.root, params.get('draft_id')))
            if method == 'draft_save':
                return _result(request_id, _draft_save(self.root, params))
            if method == 'draft_delete':
                return _result(request_id, _draft_delete(self.root, params.get('draft_id')))
            if method == 'draft_export':
                return _result(request_id, _draft_export(self.root, params))
            if method == 'export_evidence':
                return _result(request_id, _export_evidence(
                    self.root, params.get('hashes'), params.get('destination')))
            if method == 'schedule_status':
                return _result(request_id, _schedule_status(self.root))
            if method == 'schedule_profile_save':
                return _result(request_id, _schedule_profile_save(self.root, params))
            if method == 'schedule_profile_enabled':
                return _result(request_id, _schedule_profile_enabled(
                    self.root, params.get('name'), params.get('enabled')))
            if method == 'schedule_run':
                return _result(request_id, self._start_schedule_run(params))
            if method == 'summarize':
                return _result(request_id, self._start_summarize(params))
            if method == 'cancel':
                return _result(request_id, self._cancel(params))
            if method == 'shutdown':
                self._shutdown()
                return None
            return _error(request_id, 'UNKNOWN_METHOD', f'unknown method {method!r}')
        except IPCError as exc:
            return _error(request_id, exc.code, str(exc))
        except (ValueError, sqlite3.Error) as exc:
            return _error(request_id, 'INVALID_PARAMS', str(exc))
        except Exception as exc:
            return _error(request_id, 'INTERNAL_ERROR', type(exc).__name__ + ': ' + str(exc))

    def _shutdown(self):
        for job in self._jobs.values():
            job['cancel_event'].set()

    def serve(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write(_error(None, 'INVALID_JSON', 'request is not valid JSON'))
                continue
            response = self.handle(request)
            if response is not None:
                self._write(response)
            if request.get('method') == 'shutdown':
                break
