# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Local read-only email archive. No network access except explicit sync."""
import shutil, argparse, hashlib, imaplib, sqlite3, ssl, json, os, tempfile
from contextlib import closing
from datetime import datetime, timezone
from .db import connect as db_connect
from pathlib import Path

__version__ = '1.0.0'

def valid_hash(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)

def verify_raw(conn, raw):
    """Return stored hashes whose original is missing, unsafe or altered."""
    bad=[]
    for h, in conn.execute('SELECT hash FROM messages'):
        if not valid_hash(h): bad.append(h); continue
        path=raw/(h+'.eml')
        try:
            if path.is_symlink() or not path.is_file(): bad.append(h); continue
            with path.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=h: bad.append(h)
        except OSError: bad.append(h)
    return bad

def _legacy_timeline(conn, sender, limit, offset, order, after, before):
    """In-memory ordering for an archive that has not been migrated to schema 3."""
    from .timestamps import parse_timestamp
    sql='SELECT hash,subject,sender,date FROM messages'
    params=()
    if sender is not None:
        sql+=' WHERE sender=?'
        params=(sender,)
    rows=[]
    for row in conn.execute(sql,params):
        epoch,_=parse_timestamp(row[3])
        if after is not None and (epoch is None or epoch<after): continue
        if before is not None and (epoch is None or epoch>=before): continue
        rows.append((epoch,row))
    known=[item for item in rows if item[0] is not None]
    unknown=[item for item in rows if item[0] is None]
    if str(order).lower()=='desc':
        known.sort(key=lambda item:(item[0],item[1][0]),reverse=True)
    else:
        known.sort(key=lambda item:(item[0],item[1][0]))
    ordered=[row for _,row in known]+[row for _,row in unknown]
    return ordered[offset:offset+limit]

def _visible_message_filter(conn, exclude_disabled):
    """SQL condition keeping messages not tied solely to disabled accounts."""
    if not exclude_disabled:
        return None, []
    try:
        accounts=[key for key, in conn.execute(
            "SELECT source_key FROM source_settings WHERE source='email' "
            'AND disabled_at IS NOT NULL')]
    except sqlite3.OperationalError:
        return None, []
    if not accounts:
        return None, []
    placeholders=','.join('?' for _ in accounts)
    condition=('(NOT EXISTS (SELECT 1 FROM origins o WHERE o.hash=m.hash) OR EXISTS '
               '(SELECT 1 FROM origins o WHERE o.hash=m.hash AND o.account NOT IN ('
               + placeholders + ')))')
    return condition, accounts

def timeline_rows(conn, sender=None, limit=100, offset=0, order='asc', after=None, before=None,
                  exclude_disabled=False):
    """Chronological view of declared message dates; unknown dates sort last.

    Ordering happens in SQL against the indexed `sort_ts` column so large
    archives are paginated without loading every row. Archives still on
    schema 2 fall back to an in-memory sort. With `exclude_disabled`, messages
    whose only known accounts are disabled are omitted; messages with no
    account, or shared with an enabled account, remain visible.
    """
    limit=max(1,min(int(limit),5000))
    offset=max(0,int(offset or 0))
    direction='DESC' if str(order).lower()=='desc' else 'ASC'
    visibility, visibility_params=_visible_message_filter(conn, exclude_disabled)
    dated=['m.sort_ts IS NOT NULL']
    dated_params=[]
    if sender is not None: dated.append('m.sender=?'); dated_params.append(sender)
    if after is not None: dated.append('m.sort_ts>=?'); dated_params.append(int(after))
    if before is not None: dated.append('m.sort_ts<?'); dated_params.append(int(before))
    if visibility: dated.append(visibility); dated_params.extend(visibility_params)
    dated_sql=('SELECT m.hash,m.subject,m.sender,m.date FROM messages m WHERE '+
               (' AND '.join(dated))+' ORDER BY m.sort_ts '+direction+', m.hash ASC LIMIT ? OFFSET ?')
    try:
        rows=conn.execute(dated_sql,(*dated_params,limit,offset)).fetchall()
        # Unknown dates always trail the dated records, and a date-range
        # filter excludes them entirely.
        if len(rows)<limit and after is None and before is None:
            unknown_offset=0
            if not rows:
                known_total=conn.execute(
                    'SELECT count(*) FROM messages m WHERE '+(' AND '.join(dated)),
                    dated_params).fetchone()[0]
                unknown_offset=max(0,offset-known_total)
            unknown=['m.sort_ts IS NULL']
            unknown_params=[]
            if sender is not None:
                unknown.append('m.sender=?'); unknown_params.append(sender)
            if visibility:
                unknown.append(visibility); unknown_params.extend(visibility_params)
            rows.extend(conn.execute(
                'SELECT m.hash,m.subject,m.sender,m.date FROM messages m WHERE '+
                (' AND '.join(unknown))+' ORDER BY m.hash ASC LIMIT ? OFFSET ?',
                (*unknown_params,limit-len(rows),unknown_offset)).fetchall())
        return rows
    except sqlite3.OperationalError:
        return _legacy_timeline(conn,sender,limit,offset,order,after,before)

def _thread_groups(rows):
    """Group messages that share Message-ID / In-Reply-To links.

    `rows` is an iterable of `(hash, message_id, reply_to)`. Only links among
    the supplied messages are followed; no subject or time-based guesses.
    """
    rows = list(rows)
    parent = {}
    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value
    def union(first, second):
        left, right = find(first), find(second)
        if left != right:
            parent[right] = left
    by_message_id = {}
    for digest, message_id, _ in rows:
        parent[digest] = digest
        if message_id:
            by_message_id.setdefault(message_id, []).append(digest)
    for digest, message_id, reply_to in rows:
        if message_id:
            for other in by_message_id.get(message_id, ()):
                union(digest, other)
        if reply_to:
            for target in by_message_id.get(reply_to, ()):
                union(digest, target)
    groups = {}
    for digest in parent:
        groups.setdefault(find(digest), []).append(digest)
    return list(groups.values())

def relationship_graph(conn, root, limit_nodes=30, max_edges=200, after=None, before=None):
    """Observed co-participation in shared email threads or catalog conversations.

    An edge means two participants appear in the same archived thread
    (Message-ID / In-Reply-To) or the same catalog conversation key. It is
    not a claim about the nature of any relationship. Isolated
    correspondents and pairs beyond the requested limits are omitted and
    reported as truncated.
    """
    from collections import Counter
    from itertools import combinations
    limit_nodes = max(2, min(int(limit_nodes), 100))
    max_edges = max(1, min(int(max_edges), 500))
    node_stats = {}
    edge_data = {}

    def add_group(source, counter, example):
        people = sorted(counter)[:12]
        size = sum(counter.values())
        for person in people:
            stats = node_stats.setdefault((source, person), {'messages': 0, 'threads': 0})
            stats['messages'] += counter[person]
            stats['threads'] += 1
        for first, second in combinations(people, 2):
            entry = edge_data.setdefault((source, first, second),
                                         {'threads': 0, 'messages': 0, 'examples': []})
            entry['threads'] += 1
            entry['messages'] += size
            if example and len(entry['examples']) < 3:
                entry['examples'].append(example)

    try:
        clauses = []
        params = []
        if after is not None:
            clauses.append('sort_ts>=?'); params.append(int(after))
        if before is not None:
            clauses.append('sort_ts<?'); params.append(int(before))
        sql = ('SELECT hash,message_id,reply_to,sender,subject,date FROM messages'
               + (' WHERE ' + ' AND '.join(clauses) if clauses else ''))
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        from .timestamps import parse_timestamp
        rows = []
        for row in conn.execute(
                'SELECT hash,message_id,reply_to,sender,subject,date FROM messages'):
            epoch, _ = parse_timestamp(row[5])
            if after is not None and (epoch is None or epoch < after):
                continue
            if before is not None and (epoch is None or epoch >= before):
                continue
            rows.append(row)
    info = {row[0]: {'sender': row[3] or '', 'subject': row[4] or '',
                     'date': row[5] or ''} for row in rows}
    for group in _thread_groups((row[0], row[1], row[2]) for row in rows):
        people = Counter(info[digest]['sender'] for digest in group
                         if info[digest]['sender'])
        example = None
        for digest in group:
            if info[digest]['subject']:
                example = {'kind': 'email', 'id': digest,
                           'title': info[digest]['subject'],
                           'date': info[digest]['date']}
                break
        if people:
            add_group('email', people, example)

    catalog_path = root / 'catalog.sqlite3'
    if catalog_path.is_file():
        from datetime import datetime, timezone
        clauses = ["record_kind='message'", 'actor IS NOT NULL', "actor<>''"]
        params = []
        if after is not None:
            clauses.append('occurred_at>=?')
            params.append(datetime.fromtimestamp(after, timezone.utc).isoformat())
        if before is not None:
            clauses.append('occurred_at<?')
            params.append(datetime.fromtimestamp(before, timezone.utc).isoformat())
        with closing(db_connect(str(catalog_path), timeout=30)) as conn:
            conn.execute('PRAGMA query_only=ON')
            catalog_rows = conn.execute(
                'SELECT source_type,source_key,actor,record_id,occurred_at '
                'FROM records WHERE ' + ' AND '.join(clauses), params).fetchall()
        conversations = {}
        for source_type, source_key, actor, record_id, occurred in catalog_rows:
            conversations.setdefault((source_type, source_key), []).append(
                (actor, record_id, occurred))
        for (source_type, source_key), members in conversations.items():
            people = Counter(actor for actor, _, _ in members)
            example = {'kind': 'catalog', 'id': members[0][1],
                       'title': source_key or '', 'date': members[0][2] or ''}
            add_group(source_type, people, example)

    node_truncated = len(node_stats) > limit_nodes
    edges = []
    for (source, first, second), data in edge_data.items():
        edges.append({'source': f'{source}|{first}', 'target': f'{source}|{second}',
                      'source_type': source, 'source_actor': first,
                      'target_actor': second, 'threads': data['threads'],
                      'messages': data['messages'], 'examples': data['examples']})
    edges.sort(key=lambda edge: (-edge['threads'], -edge['messages'],
                                 edge['source_actor'], edge['target_actor']))
    edge_truncated = len(edges) > max_edges
    edges = edges[:max_edges]
    used = {edge['source'] for edge in edges} | {edge['target'] for edge in edges}
    nodes = [{'id': f'{source}|{actor}', 'source': source, 'actor': actor,
              'messages': stats['messages'], 'shared_threads': stats['threads']}
             for (source, actor), stats in node_stats.items()
             if f'{source}|{actor}' in used]
    nodes.sort(key=lambda node: (-node['shared_threads'], -node['messages'], node['actor']))
    nodes = nodes[:limit_nodes]
    allowed = {node['id'] for node in nodes}
    edges = [edge for edge in edges
             if edge['source'] in allowed and edge['target'] in allowed]
    return {
        'nodes': nodes, 'edges': edges,
        'truncated': node_truncated or edge_truncated,
        'definitions': [
            'Edges mean two participants appear in the same archived email thread '
            '(Message-ID / In-Reply-To) or the same catalog conversation key.',
            'A shared thread does not imply any personal, professional or causal '
            'relationship.',
            'Only participants with at least one shared thread are listed; isolated '
            'correspondents are omitted.',
            f'At most 12 participants per thread are compared; at most {max_edges} edges '
            f'and {limit_nodes} nodes are returned, and anything beyond is truncated.',
            'Email addresses and catalog actor names are never merged across sources.',
        ],
    }


def correspondent_counts(conn, limit=100, exclude_disabled=False):
    visibility, params=_visible_message_filter(conn, exclude_disabled)
    where=(' WHERE '+visibility) if visibility else ''
    return conn.execute(
        "SELECT m.sender, count(*) FROM messages m"+where+
        " GROUP BY m.sender ORDER BY count(*) DESC, m.sender LIMIT ?",
        (*params, max(1,min(int(limit),5000)))).fetchall()

def diagnose(conn, raw):
    """Consistency report shared by the CLI and the desktop protocol."""
    checks=conn.execute('PRAGMA quick_check').fetchall()
    foreign=conn.execute('PRAGMA foreign_key_check').fetchall()
    hashes={h for h, in conn.execute('SELECT hash FROM messages')}
    indexed={h for h, in conn.execute('SELECT hash FROM search')}
    originals={p.stem for p in raw.glob('*.eml') if p.is_file()}
    duplicate=conn.execute('SELECT count(*) FROM search').fetchone()[0]-len(indexed)
    try:
        undated=conn.execute('SELECT count(*) FROM messages WHERE sort_ts IS NULL').fetchone()[0]
    except sqlite3.OperationalError:
        undated=None
    return {'sqlite_ok':checks==[('ok',)],'foreign_key_errors':len(foreign),'messages':len(hashes),
            'undated':undated,
            'index_missing':sorted(hashes-indexed),'index_extra':sorted(indexed-hashes),
            'index_duplicate_rows':duplicate,'raw_missing':sorted(hashes-originals),
            'raw_unindexed':sorted(originals-hashes),'raw_invalid':verify_raw(conn,raw)}

class Archive:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve(); self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.raw = self.root / 'raw'
        if self.raw.is_symlink() or (self.root / 'archive.sqlite3').is_symlink(): raise ValueError('Unsafe archive path')
        self.raw.mkdir(mode=0o700, exist_ok=True)
        self.db = db_connect(self.root / 'archive.sqlite3', timeout=30)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=30000')
        try:
            self._migrate_safely()
        except BaseException:
            self.db.close()
            raise
    def _migrate_safely(self):
        """Migrate under the archive lock, snapshotting an existing archive first.

        A brand-new database needs no snapshot. If another process is already
        working, an up-to-date schema is safe to use read-only; a pending
        migration must wait rather than race.
        """
        from . import migrations
        from .sync_lock import sync_lock, SyncBusy
        target=migrations.target_version('archive')
        try:
            with sync_lock(self.root):
                version=migrations.current_version(self.db)
                if version<target and migrations.has_schema(self.db):
                    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                    path=self.root/f'archive.v{version}.{stamp}.backup.sqlite3'
                    suffix=1
                    while path.exists():
                        path=self.root/f'archive.v{version}.{stamp}-{suffix}.backup.sqlite3'
                        suffix+=1
                    migrations.snapshot(self.db,path)
                migrations.apply(self.db,'archive',root=self.root)
        except SyncBusy:
            if migrations.current_version(self.db)<target:
                raise
    def ingest(self, raw, origin=None):
        from .sync_lock import sync_lock
        with sync_lock(self.root):
            return self._ingest(raw,origin)
    def _ingest(self, raw, origin=None):
        if len(raw) > 64 * 1024 * 1024: raise ValueError('Message exceeds 64 MiB limit')
        digest = hashlib.sha256(raw).hexdigest(); target = self.raw / (digest+'.eml')
        if target.is_symlink(): raise ValueError('Unsafe raw file')
        if not target.exists():
            tmp=None
            try:
                with tempfile.NamedTemporaryFile(dir=self.raw, delete=False) as f:
                    tmp=Path(f.name)
                    f.write(raw); f.flush(); os.fsync(f.fileno())
                os.replace(tmp,target)
            finally:
                if tmp is not None: tmp.unlink(missing_ok=True)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=digest: raise ValueError('Raw integrity mismatch')
        from .mime_index import EXTRACTION_VERSION, extract, index_text
        from .timestamps import parse_timestamp
        msg,body,attachments=extract(raw)
        fields=[str(msg.get(k,'')) for k in ('Subject','From','Date','Message-ID','In-Reply-To')]
        sort_ts,precision=parse_timestamp(fields[2])
        with self.db:
            cur=self.db.execute(
                'INSERT OR IGNORE INTO messages(hash,subject,sender,date,message_id,reply_to,body,'
                'sort_ts,date_precision,size) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (digest,*fields,body,sort_ts,precision,len(raw)))
            if cur.rowcount:
                self.db.execute('INSERT INTO search VALUES(?,?,?,?)',(digest,fields[0],fields[1],index_text(body,attachments)))
                self.db.executemany('INSERT INTO attachment_index VALUES(?,?,?,?,?)',[(digest,*a) for a in attachments])
                self.db.execute('INSERT INTO extraction_versions VALUES(?,?)',
                                (digest, EXTRACTION_VERSION))
            if origin:
                self.db.execute('INSERT OR IGNORE INTO origins VALUES(?,?,?,?,?)',(*origin,digest))
                if self.db.execute('SELECT hash FROM origins WHERE account=? AND folder=? AND validity=? AND uid=?',origin).fetchone()[0]!=digest: raise ValueError('Origin conflict')
        return digest
    def begin_import_run(self, source, source_key=None):
        """Open an import-run record so the UI can show real history."""
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.db:
            cur=self.db.execute(
                'INSERT INTO import_runs(source,source_key,started_at,status) VALUES(?,?,?,?)',
                (source,source_key,now,'running'))
        return cur.lastrowid
    def finish_import_run(self, run_id, status='ok', files_seen=None, records_seen=None,
                          records_new=None, records_existing=None, records_failed=None,
                          detail=None):
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.db:
            self.db.execute(
                'UPDATE import_runs SET finished_at=?,status=?,files_seen=?,records_seen=?,'
                'records_new=?,records_existing=?,records_failed=?,detail=? WHERE id=?',
                (now,status,files_seen,records_seen,records_new,records_existing,
                 records_failed,detail,run_id))
        return run_id
    def import_history(self, limit=50):
        limit=max(1,min(int(limit),500))
        return self.db.execute(
            'SELECT id,source,source_key,started_at,finished_at,status,files_seen,'
            'records_seen,records_new,records_existing,records_failed,detail '
            'FROM import_runs ORDER BY COALESCE(finished_at,started_at) DESC, id DESC LIMIT ?',
            (limit,)).fetchall()
    def source_enabled(self, source, source_key):
        """False when a source is explicitly disabled for new ingestion."""
        if source not in ('email','catalog'): raise ValueError('Unknown source type')
        row=self.db.execute(
            'SELECT disabled_at FROM source_settings WHERE source=? AND source_key=?',
            (source,source_key)).fetchone()
        return row is None or row[0] is None
    def source_set_enabled(self, source, source_key, enabled, note=None):
        """Enable or disable a source without touching its historical records."""
        self._validate_source(source, source_key, note)
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.db:
            self.db.execute(
                'INSERT INTO source_settings(source,source_key,disabled_at,note) VALUES(?,?,?,?) '
                'ON CONFLICT(source,source_key) DO UPDATE SET '
                'disabled_at=excluded.disabled_at, note=excluded.note',
                (source,source_key,None if enabled else now,note))
            self.db.execute(
                'INSERT INTO source_events(source,source_key,action,at,detail) VALUES(?,?,?,?,?)',
                (source,source_key,'enabled' if enabled else 'disabled',now,note))
        return {'source':source,'source_key':source_key,'enabled':bool(enabled),'note':note}
    def source_events(self, limit=100):
        limit=max(1,min(int(limit),500))
        return self.db.execute(
            'SELECT id,source,source_key,action,at,detail FROM source_events '
            'ORDER BY at DESC, id DESC LIMIT ?',(limit,)).fetchall()
    def _validate_source(self, source, source_key, note=None):
        if source not in ('email','catalog'):
            raise ValueError('Unknown source type')
        if (not isinstance(source_key,str) or not source_key or len(source_key)>500
                or any(ord(c)<32 for c in source_key)):
            raise ValueError('Invalid source key')
        if note is not None and (not isinstance(note,str) or len(note)>1000
                                 or any(ord(c)<32 and c not in '\n\t' for c in note)):
            raise ValueError('Invalid note')
    def _unique_hashes(self, account):
        """Hashes whose only mailbox account is `account`."""
        return [h for h, in self.db.execute(
            'SELECT o.hash FROM origins o WHERE o.account=? GROUP BY o.hash '
            'HAVING (SELECT count(DISTINCT x.account) FROM origins x WHERE x.hash=o.hash)=1',
            (account,))]
    def _settings_exist(self, source, source_key):
        return self.db.execute(
            'SELECT 1 FROM source_settings WHERE source=? AND source_key=?',
            (source,source_key)).fetchone() is not None
    def source_removal_preview(self, source, source_key):
        """Describe exactly what removing a source would delete and retain."""
        self._validate_source(source,source_key)
        if source=='email':
            origins=self.db.execute('SELECT count(*) FROM origins WHERE account=?',
                                    (source_key,)).fetchone()[0]
            messages=self.db.execute('SELECT count(DISTINCT hash) FROM origins WHERE account=?',
                                     (source_key,)).fetchone()[0]
            unique=self._unique_hashes(source_key)
            raw_bytes=self.db.execute(
                'SELECT coalesce(sum(m.size),0) FROM messages m WHERE m.hash IN ('
                'SELECT o.hash FROM origins o WHERE o.account=? GROUP BY o.hash '
                'HAVING (SELECT count(DISTINCT x.account) FROM origins x WHERE x.hash=o.hash)=1)',
                (source_key,)).fetchone()[0] if unique else 0
            if origins==0 and messages==0 and not self._settings_exist(source,source_key):
                raise ValueError('Unknown email account')
            retained=messages-len(unique)
            return {
                'source':source,'source_key':source_key,'removable':True,
                'origins_removed':origins,'records_removed':len(unique),
                'records_retained':retained,'shared_duplicates_retained':retained,
                'raw_bytes_freed':raw_bytes,'catalog_records_removed':0,
                'catalog_files_removed':0,'catalog_bytes':0,
                'research_annotations_retained':True,
                'explanation':[
                    f'{origins} mailbox-origin records will be deleted.',
                    f'{len(unique)} unique messages and their preserved EML originals will be deleted.',
                    f'{retained} messages also exist in another account and will be retained.',
                    'Messages imported from .eml files have no account and are never affected.',
                    'Research tags, metadata and summaries keyed by removed message hashes stay in '
                    'the research overlay; the same original re-imports under the same hash.',
                ]}
        catalog_path=self.root/'catalog.sqlite3'
        if not catalog_path.is_file():
            if self._settings_exist(source,source_key): catalog_path=None
            else: raise ValueError('Unknown catalog source')
        files=records=size=0
        if catalog_path is not None:
            with closing(db_connect(str(catalog_path),timeout=30)) as conn:
                conn.execute('PRAGMA query_only=ON')
                files,size=conn.execute(
                    'SELECT count(*),coalesce(sum(size),0) FROM source_files WHERE source_type=?',
                    (source_key,)).fetchone()
                records=conn.execute('SELECT count(*) FROM records WHERE source_type=?',
                                     (source_key,)).fetchone()[0]
        if files==0 and records==0 and not self._settings_exist(source,source_key):
            raise ValueError('Unknown catalog source')
        return {
            'source':source,'source_key':source_key,'removable':True,
            'origins_removed':0,'records_removed':records,'records_retained':0,
            'shared_duplicates_retained':0,'raw_bytes_freed':0,
            'catalog_records_removed':records,'catalog_files_removed':files,
            'catalog_bytes':size,'research_annotations_retained':False,
            'explanation':[
                f'{records} catalog records will be deleted.',
                f'{files} catalog source-file references will be deleted.',
                'Original export files on disk are not modified or deleted.',
                'Archived email and other catalog sources are unaffected.',
            ]}
    def verify_source(self, source, source_key, progress=None, cancel_event=None):
        """Verify integrity for one email account or catalog source only.

        Email checks are scoped to message hashes reachable from the account's
        mailbox origins. Catalog checks re-hash the recorded export files where
        they still exist. Hash checks detect changed bytes, not authenticity.
        """
        self._validate_source(source,source_key)
        if source=='email':
            hashes=[h for h, in self.db.execute(
                'SELECT DISTINCT hash FROM origins WHERE account=? ORDER BY hash',
                (source_key,))]
            if not hashes and not self._settings_exist(source,source_key):
                raise ValueError('Unknown email account')
            bad=[]; missing=[]; checked=0; cancelled=False
            for done,h in enumerate(hashes,1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled=True; break
                checked+=1
                if not valid_hash(h):
                    bad.append(h)
                else:
                    path=self.raw/(h+'.eml')
                    try:
                        if path.is_symlink() or not path.is_file():
                            missing.append(h)
                        else:
                            with path.open('rb') as handle:
                                if hashlib.file_digest(handle,'sha256').hexdigest()!=h:
                                    bad.append(h)
                    except OSError:
                        missing.append(h)
                if progress is not None and (done%50==0 or done==len(hashes)):
                    progress(done,len(hashes),h,'checked')
            return {'source':source,'source_key':source_key,'checked':checked,
                    'ok':checked-len(bad)-len(missing),'bad':bad,'missing':missing,
                    'cancelled':cancelled}
        catalog_path=self.root/'catalog.sqlite3'
        if not catalog_path.is_file():
            if self._settings_exist(source,source_key):
                return {'source':source,'source_key':source_key,'checked':0,'ok':0,
                        'bad':[],'missing':[],'cancelled':False}
            raise ValueError('Unknown catalog source')
        with closing(db_connect(str(catalog_path),timeout=30)) as conn:
            conn.execute('PRAGMA query_only=ON')
            rows=conn.execute(
                'SELECT file_hash,path FROM source_files WHERE source_type=? ORDER BY path',
                (source_key,)).fetchall()
        if not rows and not self._settings_exist(source,source_key):
            raise ValueError('Unknown catalog source')
        bad=[]; missing=[]; checked=0; cancelled=False
        for done,(expected,recorded) in enumerate(rows,1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled=True; break
            checked+=1
            path=Path(recorded).expanduser()
            try:
                if path.is_symlink() or not path.is_file():
                    missing.append(recorded)
                else:
                    with path.open('rb') as handle:
                        if hashlib.file_digest(handle,'sha256').hexdigest()!=expected:
                            bad.append(recorded)
            except OSError:
                missing.append(recorded)
            if progress is not None:
                progress(done,len(rows),recorded,'checked')
        return {'source':source,'source_key':source_key,'checked':checked,
                'ok':checked-len(bad)-len(missing),'bad':bad,'missing':missing,
                'cancelled':cancelled}
    def source_remove(self, source, source_key, confirm=False):
        """Remove a source after an explicit confirmation, reporting what was kept."""
        if confirm is not True:
            raise ValueError('Removal requires explicit confirmation')
        preview=self.source_removal_preview(source,source_key)
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        if source=='email':
            from .sync_lock import sync_lock
            unique=self._unique_hashes(source_key)
            with sync_lock(self.root):
                with self.db:
                    self.db.execute('DELETE FROM origins WHERE account=?',(source_key,))
                    self.db.executemany('DELETE FROM attachment_index WHERE hash=?',
                                        [(h,) for h in unique])
                    self.db.executemany('DELETE FROM extraction_versions WHERE hash=?',
                                        [(h,) for h in unique])
                    self.db.executemany('DELETE FROM search WHERE hash=?',[(h,) for h in unique])
                    self.db.executemany('DELETE FROM messages WHERE hash=?',[(h,) for h in unique])
                    self.db.execute('DELETE FROM source_settings WHERE source=? AND source_key=?',
                                    (source,source_key))
                    self.db.execute(
                        'INSERT INTO source_events(source,source_key,action,at,detail) '
                        'VALUES(?,?,?,?,?)',
                        (source,source_key,'removed',now,
                         f"removed {len(unique)} messages; retained "
                         f"{preview['records_retained']} shared"))
            removed=0; failures=[]
            for h in unique:
                path=self.raw/(h+'.eml')
                try:
                    if path.is_symlink():
                        raise ValueError('Unsafe raw file')
                    path.unlink(missing_ok=True)
                    removed+=1
                except (OSError,ValueError) as exc:
                    failures.append({'hash':h,'error':type(exc).__name__})
            return {**preview,'removed':True,'raw_files_removed':removed,
                    'raw_file_errors':failures}
        from .catalog import Catalog
        from .sync_lock import sync_lock
        with sync_lock(self.root):
            catalog=Catalog(self.root)
            try:
                with catalog.db:
                    catalog.db.execute('DELETE FROM records WHERE source_type=?',(source_key,))
                    catalog.db.execute('DELETE FROM source_files WHERE source_type=?',(source_key,))
            finally:
                catalog.close()
            with self.db:
                self.db.execute('DELETE FROM source_settings WHERE source=? AND source_key=?',
                                (source,source_key))
                self.db.execute(
                    'INSERT INTO source_events(source,source_key,action,at,detail) '
                    'VALUES(?,?,?,?,?)',
                    (source,source_key,'removed',now,
                     f"removed {preview['catalog_records_removed']} catalog records"))
        return {**preview,'removed':True}
    def import_files(self, paths, progress=None, cancel_event=None):
        """Import .eml files or directories with progress, cancellation and history.

        Existing bytes are never rewritten; a file already in the archive is
        counted as existing rather than new. Per-file failures are reported
        without aborting the rest of the run.
        """
        files=[]; errors=[]; seen=set()
        def add_file(candidate):
            key=candidate.resolve()
            if key not in seen:
                seen.add(key)
                files.append(candidate)
        for raw in paths:
            path=Path(raw).expanduser()
            if path.is_symlink():
                errors.append({'path':str(path),'error':'Unsafe symbolic link'})
                continue
            if path.is_dir():
                for current, dirnames, filenames in os.walk(path, followlinks=False):
                    current=Path(current)
                    dirnames[:]=sorted(
                        name for name in dirnames
                        if not name.startswith('.') and not (current/name).is_symlink())
                    for name in sorted(filenames):
                        if name.startswith('.') or not name.lower().endswith('.eml'):
                            continue
                        candidate=current/name
                        if candidate.is_symlink():
                            errors.append({'path':str(candidate),'error':'Unsafe symbolic link'})
                        elif candidate.is_file():
                            add_file(candidate)
                        else:
                            errors.append({'path':str(candidate),'error':'Not a regular file'})
            elif path.is_file():
                add_file(path)
            else:
                errors.append({'path':str(path),'error':'FileNotFoundError: not found'})
        run=self.begin_import_run('eml', ', '.join(str(p) for p in paths)[:200])
        imported=[]; existing=0; processed=0; status='ok'
        total=len(files)
        try:
            for done,path in enumerate(files,1):
                if cancel_event is not None and cancel_event.is_set():
                    status='cancelled'; break
                try:
                    raw=path.read_bytes()
                    digest=hashlib.sha256(raw).hexdigest()
                    is_new=self.db.execute('SELECT 1 FROM messages WHERE hash=?',(digest,)).fetchone() is None
                    self.ingest(raw)
                    processed+=1
                    if is_new: imported.append(digest)
                    else: existing+=1
                    if progress is not None: progress(done,total,str(path),'imported' if is_new else 'existing')
                except Exception as exc:
                    errors.append({'path':str(path),'error':type(exc).__name__+': '+str(exc)})
                    if progress is not None: progress(done,total,str(path),'error')
        except Exception as exc:
            self.finish_import_run(run,'error',len(files),processed,len(imported),existing,
                                   len(errors),type(exc).__name__)
            raise
        self.finish_import_run(run,status,len(files),processed,len(imported),existing,len(errors))
        return {'files_seen':len(files),'imported':len(imported),'existing':existing,
                'hashes':imported,'errors':errors,'cancelled':status=='cancelled','run_id':run}
    def reindex(self, progress=None, cancel_event=None):
        """Repair derived index data from the preserved raw originals.

        Messages extracted by an older build, messages with a missing search
        row, and messages with duplicate search rows are re-extracted. Each
        message is verified against its raw original and re-indexed in its own
        transaction, so an interrupted or cancelled run keeps its progress.
        """
        from .documents import binary_extractors_available
        from .mime_index import EXTRACTION_VERSION, extract, index_text
        from .sync_lock import sync_lock
        count=0
        with sync_lock(self.root):
            pending_sql=(
                'SELECT hash,subject,sender,date FROM messages '
                'WHERE hash NOT IN (SELECT hash FROM extraction_versions WHERE version=?) '
                'OR hash NOT IN (SELECT hash FROM search) '
                'OR hash IN (SELECT hash FROM search GROUP BY hash HAVING count(*)>1)')
            pending_params=[EXTRACTION_VERSION]
            if binary_extractors_available():
                pending_sql += (
                    " OR hash IN (SELECT hash FROM attachment_index WHERE content='' "
                    "AND (lower(mime)=? OR lower(mime)=? OR lower(filename) LIKE '%.pdf' "
                    "OR lower(filename) LIKE '%.docx'))")
                pending_params.extend([
                    'application/pdf',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'])
            pending=self.db.execute(pending_sql,pending_params).fetchall()
            total=len(pending)
            rowids={}
            for rowid,h in self.db.execute('SELECT rowid,hash FROM search'):
                rowids.setdefault(h,[]).append(rowid)
            for done,(h,subject,sender,date) in enumerate(pending,1):
                if cancel_event is not None and cancel_event.is_set(): break
                if not valid_hash(h): raise ValueError('Invalid stored message hash')
                source=self.raw/(h+'.eml')
                if source.is_symlink(): raise ValueError('Unsafe raw file')
                raw=source.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=h: raise ValueError('Raw integrity mismatch: '+h)
                _,body,attachments=extract(raw)
                from .timestamps import parse_timestamp
                sort_ts,precision=parse_timestamp(date)
                with self.db:
                    self.db.execute('UPDATE messages SET body=?,sort_ts=?,date_precision=? WHERE hash=?',
                                    (body,sort_ts,precision,h))
                    self.db.executemany('DELETE FROM search WHERE rowid=?',[(rid,) for rid in rowids.get(h,[])])
                    self.db.execute('INSERT INTO search VALUES(?,?,?,?)',(h,subject,sender,index_text(body,attachments)))
                    self.db.execute('DELETE FROM attachment_index WHERE hash=?',(h,))
                    self.db.executemany('INSERT INTO attachment_index VALUES(?,?,?,?,?)',[(h,*a) for a in attachments])
                    self.db.execute('INSERT OR REPLACE INTO extraction_versions VALUES(?,?)',
                                    (h, EXTRACTION_VERSION))
                count+=1
                if progress is not None: progress(done,total,h,'reindexed')
        return count
    def attachments(self,h):
        return self.db.execute('SELECT filename,mime,length(content) FROM attachment_index WHERE hash=? ORDER BY part',(h,)).fetchall()
    def health(self):
        from .sync_lock import sync_lock
        with sync_lock(self.root):
            return self._health()
    def _health(self):
        from .catalog import catalog_health
        report = diagnose(self.db, self.raw)
        report['catalog'] = catalog_health(self.root)
        return report
    def recover_orphans(self, progress=None, cancel_event=None):
        """Re-ingest valid raw originals left behind by an interrupted ingest.

        Only byte-verified files with a valid hash are recovered; no origin is
        invented, so a recovered message has no mailbox provenance. Altered,
        oversized and unreadable files are left in place for investigation.
        """
        from .sync_lock import sync_lock
        recovered=[]
        with sync_lock(self.root):
            candidates=sorted(self.raw.glob('*.eml'))
            total=len(candidates)
            for done,path in enumerate(candidates,1):
                if cancel_event is not None and cancel_event.is_set(): break
                h=path.stem; status='skipped'
                try:
                    if (valid_hash(h) and not path.is_symlink() and path.is_file()
                            and not self.db.execute('SELECT 1 FROM messages WHERE hash=?',(h,)).fetchone()
                            and path.stat().st_size<=64*1024*1024):
                        raw=path.read_bytes()
                        if hashlib.sha256(raw).hexdigest()==h:
                            self._ingest(raw); recovered.append(h); status='recovered'
                except OSError: status='unreadable'
                if progress is not None: progress(done,total,h,status)
        return recovered
    def find(self, query, limit=100):
        limit=max(1,min(int(limit),5000))
        try:
            return self.db.execute('SELECT hash,subject,sender FROM search WHERE search MATCH ? LIMIT ?', (query,limit)).fetchall()
        except sqlite3.OperationalError:
            phrase='"'+query.replace('"','""')+'"'
            try:
                return self.db.execute('SELECT hash,subject,sender FROM search WHERE search MATCH ? LIMIT ?', (phrase,limit)).fetchall()
            except sqlite3.OperationalError:
                return []
    def verify(self):
        return verify_raw(self.db,self.raw)
    def export(self, hashes, destination):
        """Write original message files to a user-chosen directory.

        Boundary: the destination is outside the archive and is written as
        plaintext, even when the archive databases are encrypted.
        """
        dest=Path(destination)
        hashes=list(dict.fromkeys(hashes))
        if not hashes: raise ValueError('Select at least one message')
        for h in hashes:
            if not valid_hash(h):
                raise ValueError('Invalid message hash')
            if not self.db.execute('SELECT 1 FROM messages WHERE hash=?',(h,)).fetchone():
                raise ValueError('Unknown message')
        dest.mkdir(mode=0o700,parents=True,exist_ok=False)
        try:
            manifest=[]
            for h in hashes:
                source=self.raw/(h+'.eml')
                if source.is_symlink(): raise ValueError('Unsafe raw file')
                raw=source.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=h: raise ValueError('Integrity mismatch')
                with os.fdopen(os.open(dest/(h+'.eml'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as output:
                    output.write(raw)
                origins=self.db.execute('SELECT account,folder,validity,uid FROM origins WHERE hash=? ORDER BY account,folder,validity,uid',(h,)).fetchall()
                manifest.append({'sha256':h,'file':h+'.eml','origins':origins})
            with os.fdopen(os.open(dest/'manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as output:
                json.dump(manifest,output,indent=2)
        except Exception:
            shutil.rmtree(dest)
            raise
    def details(self, digest):
        row=self.db.execute('SELECT subject,sender,date,message_id,reply_to,body FROM messages WHERE hash=?',(digest,)).fetchone()
        if row is None: raise ValueError('Unknown message')
        return dict(zip(('subject','sender','date','message_id','reply_to','body'),row))
    def recent(self, limit=100):
        return self.db.execute('SELECT hash,subject,sender FROM messages ORDER BY rowid DESC LIMIT ?', (max(1,min(int(limit),5000)),)).fetchall()
    def timeline(self, sender=None, limit=100, offset=0, order='asc', after=None, before=None,
                 exclude_disabled=False):
        """Chronological view of declared message dates; unparseable dates sort last."""
        return timeline_rows(self.db,sender,limit,offset,order,after,before,exclude_disabled)
    def correspondents(self, limit=100, exclude_disabled=False):
        return correspondent_counts(self.db,limit,exclude_disabled)
    def relationship_graph(self, limit_nodes=30, max_edges=200, after=None, before=None):
        return relationship_graph(self.db, self.root, limit_nodes, max_edges, after, before)

    def thread(self, digest):
        """Return linked messages using Message-ID and In-Reply-To, not subject guesses."""
        row=self.db.execute('SELECT message_id,reply_to FROM messages WHERE hash=?',(digest,)).fetchone()
        if row is None: raise ValueError('Unknown message')
        visited={digest}
        frontier={value for value in row if value}
        identifiers=set()
        while frontier:
            value=frontier.pop()
            if value in identifiers: continue
            identifiers.add(value)
            for h,mid,parent in self.db.execute(
                'SELECT hash,message_id,reply_to FROM messages WHERE message_id=? OR reply_to=?',(value,value)):
                visited.add(h)
                frontier.update(v for v in (mid,parent) if v and v not in identifiers)
        return [self.details(h) | {'sha256':h} for h in sorted(visited)]
    def stats(self):
        return {'messages':self.db.execute('SELECT count(*) FROM messages').fetchone()[0],
                'origins':self.db.execute('SELECT count(*) FROM origins').fetchone()[0]}
    def sync(self, host, user, password, folder='INBOX'):
        from .sync_lock import sync_lock
        if not self.source_enabled('email', f'{host}|{user}'):
            raise ValueError('Source is disabled; enable it before synchronizing')
        with sync_lock(self.root):
            run=self.begin_import_run('imap', f'{host}|{user}|{folder}')
            try:
                count=self._sync(host, user, password, folder)
            except Exception as exc:
                self.finish_import_run(run,'error',detail=type(exc).__name__)
                raise
            self.finish_import_run(run,'ok',records_seen=count,records_new=count,
                                   records_existing=0,records_failed=0)
            return count
    def mailboxes(self, host, user, password):
        """List selectable mailboxes on the server. Read-only; makes no changes."""
        from .preferences import load
        prefs=load(self.root)
        with imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context(),
                               timeout=prefs['network_timeout']) as m:
            m.login(user,normalize_password(host,password))
            status,lines=m.list()
            if status!='OK': raise RuntimeError('List failed')
        return parse_mailbox_list(lines)

    def sync_mailboxes(self, host, user, password, folders=None):
        """Synchronize several mailboxes in one run, reporting per-folder results."""
        targets=(list(folders) if folders is not None
                 else self.mailboxes(host,user,password))
        if not targets: raise ValueError('No selectable mailboxes were found')
        result={'folders':{},'imported':0,'failed':0}
        for folder in targets:
            try:
                count=self.sync(host,user,password,folder)
            except Exception as exc:
                result['folders'][folder]={'error':f'{type(exc).__name__}: {exc}'}
                result['failed']+=1
                continue
            result['folders'][folder]={'imported':count}
            result['imported']+=count
        return result

    def _sync_watermark(self, account, folder, quoted, validity):
        """Highest UID already stored for this account, folder and UIDVALIDITY.

        A new UIDVALIDITY means the server renumbered the mailbox, so the
        watermark is dropped and the next run searches everything again.
        """
        row=self.db.execute(
            'SELECT MAX(CAST(uid AS INTEGER)) FROM origins '
            'WHERE account=? AND folder IN (?,?) AND validity=?',
            (account,folder,quoted,validity)).fetchone()
        return row[0] if row and row[0] is not None else None

    def _sync(self, host, user, password, folder):
        if folder.startswith('"') and folder.endswith('"'):
            folder=folder[1:-1]
        if not folder or any(c in folder for c in '\r\n\x00'):
            raise ValueError('Invalid mailbox name')
        quoted='"'+folder.replace('\\','\\\\').replace('"','\\"')+'"'
        password=normalize_password(host,password)
        from .preferences import load
        prefs=load(self.root)
        with imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context(), timeout=prefs['network_timeout']) as m:
            m.login(user,password)
            if m.select(quoted,readonly=True)[0]!='OK': raise RuntimeError('Select failed')
            resp=m.response('UIDVALIDITY')[1]
            if not resp or resp[0] is None or not resp[0].decode().isdigit(): raise RuntimeError('Missing UIDVALIDITY')
            validity=resp[0].decode()
            watermark=self._sync_watermark(host+'|'+user,folder,quoted,validity)
            # IMAP sequence numbers are unsigned 32-bit; clamp the lower bound so
            # the highest possible UID cannot produce an invalid range.
            criteria=('ALL' if watermark is None
                      else f'UID {min(watermark+1, MAX_IMAP_UID)}:*')
            status,data=m.uid('search',None,criteria)
            if status!='OK': raise RuntimeError('Search failed')
            count=0
            for uid in data[0].split():
                if watermark is not None:
                    try:
                        if int(uid)<=watermark: continue
                    except ValueError:
                        pass
                origin=(host+'|'+user,folder,validity,uid.decode())
                if self.db.execute('SELECT 1 FROM origins WHERE account=? AND folder IN (?,?) AND validity=? AND uid=?',(origin[0],folder,quoted,validity,uid.decode())).fetchone(): continue
                status,parts=m.uid('fetch',uid,'(BODY.PEEK[])')
                if status!='OK': raise RuntimeError('Fetch failed')
                payload=[p[1] for p in parts if isinstance(p,tuple)]
                if len(payload)!=1: raise RuntimeError('Unexpected fetch response')
                self._ingest(payload[0],origin); count+=1
                if prefs['batch_limit'] and count>=prefs['batch_limit']: break
            return count

MAX_IMAP_UID = 4294967295


LIST_RE = None


def normalize_password(host, password):
    """Gmail app passwords are shown with spaces that must not be sent."""
    if host.lower() == 'imap.gmail.com':
        return ''.join(password.split())
    return password


def _unquote(value):
    """Decode an IMAP quoted string, handling \\ and \" escapes."""
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        out = []
        index = 0
        while index < len(inner):
            char = inner[index]
            if char == '\\' and index + 1 < len(inner):
                index += 1
                out.append(inner[index])
            else:
                out.append(char)
            index += 1
        return ''.join(out)
    return value


def parse_mailbox_list(lines):
    """Names of selectable mailboxes from an IMAP LIST response."""
    global LIST_RE
    if LIST_RE is None:
        import re
        LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
    names=[]
    for raw in lines:
        if raw is None: continue
        if isinstance(raw, tuple):
            header, literal = raw
            match=LIST_RE.match(header if isinstance(header,bytes)
                                else str(header).encode())
            if not match: continue
            name=literal.decode('utf-8','replace') if isinstance(literal,bytes) else str(literal)
        else:
            match=LIST_RE.match(raw if isinstance(raw,bytes) else str(raw).encode())
            if not match: continue
            name=_unquote(match.group('name').strip())
        flags=match.group('flags').decode('utf-8','replace').lower()
        if '\\noselect' in flags or '\\nonexistent' in flags:
            continue
        if not name: continue
        names.append(name)
    return names


def capabilities():
    """Report optional capabilities so packaging omissions are visible.

    Each entry names the import name, whether it can be imported, and its
    version when available. Nothing is installed or downloaded here.
    """
    import importlib
    report = {}
    for name in ('sqlcipher3', 'pypdf', 'docx', 'keyring', 'cryptography'):
        entry = {'available': False, 'version': None}
        try:
            module = importlib.import_module(name)
        except Exception:
            report[name] = entry
            continue
        version = getattr(module, '__version__', None)
        if version is None and name == 'cryptography':
            try:
                from importlib.metadata import version as _version
                version = _version('cryptography')
            except Exception:
                version = None
        entry['available'] = True
        entry['version'] = version
        report[name] = entry
    return {'optional_dependencies': report,
            'encryption': report['sqlcipher3']['available'],
            'document_extraction': report['pypdf']['available'] or report['docx']['available'],
            'os_credential_store': report['keyring']['available']}

def main(argv=None):
    import atexit, getpass, sys
    p=argparse.ArgumentParser(); p.add_argument('--archive',default='~/.hb-dispatch'); p.add_argument('--version',action='version',version=f'%(prog)s {__version__}'); sub=p.add_subparsers(dest='cmd',required=True)
    q=sub.add_parser('import'); q.add_argument('files',nargs='+'); q=sub.add_parser('search'); q.add_argument('query'); sub.add_parser('verify'); q=sub.add_parser('export'); q.add_argument('destination'); q.add_argument('hashes',nargs='+'); q=sub.add_parser('sync'); q.add_argument('host'); q.add_argument('user'); q.add_argument('--folder',default='INBOX'); q.add_argument('--all-folders',action='store_true'); sub.add_parser('gui'); q=sub.add_parser('facebook'); fb=q.add_subparsers(dest='fcmd',required=True); fp=fb.add_parser('preview'); fp.add_argument('paths',nargs='+'); fi=fb.add_parser('import'); fi.add_argument('paths',nargs='+'); fl=fb.add_parser('list'); fl.add_argument('--kind'); fl.add_argument('--actor'); fl.add_argument('--limit',type=int,default=100); fs=fb.add_parser('search'); fs.add_argument('query'); fs.add_argument('--limit',type=int,default=100); fb.add_parser('verify'); fb.add_parser('reindex'); q=sub.add_parser('documents'); dc=q.add_subparsers(dest='dcmd',required=True); dp=dc.add_parser('preview'); dp.add_argument('paths',nargs='+'); di=dc.add_parser('import'); di.add_argument('paths',nargs='+'); q=sub.add_parser('summarize'); su=q.add_subparsers(dest='scmd',required=True); sr=su.add_parser('run'); sr.add_argument('hashes',nargs='*'); sr.add_argument('--all',action='store_true'); sr.add_argument('--type',choices=('message','thread'),default='message'); sr.add_argument('--model'); sr.add_argument('--prompt-version'); sr.add_argument('--force',action='store_true'); sl=su.add_parser('list'); sl.add_argument('hash'); sd=su.add_parser('delete'); sd.add_argument('hash'); sd.add_argument('--type'); sd.add_argument('--model'); sd.add_argument('--prompt-version'); q=sub.add_parser('search-all'); q.add_argument('query'); q.add_argument('--limit',type=int,default=100); q.add_argument('--sources',default='email,facebook,document'); sub.add_parser('serve'); sub.add_parser('health'); sub.add_parser('capabilities'); q=sub.add_parser('mbox'); mc=q.add_subparsers(dest='mcmd',required=True); mp=mc.add_parser('preview'); mp.add_argument('path'); mi=mc.add_parser('import'); mi.add_argument('path'); me=mc.add_parser('export'); me.add_argument('destination'); me.add_argument('--hash',action='append',default=[]); me.add_argument('--account'); sub.add_parser('reindex'); sub.add_parser('recover-orphans'); q=sub.add_parser('import-history'); q.add_argument('--limit',type=int,default=50); q=sub.add_parser('backup'); q.add_argument('destination'); q=sub.add_parser('verify-backup'); q.add_argument('snapshot'); q=sub.add_parser('restore'); q.add_argument('snapshot'); q=sub.add_parser('schedule'); q.add_argument('args',nargs=argparse.REMAINDER); q=sub.add_parser('migrate'); q.add_argument('--dry-run',action='store_true'); q.add_argument('--no-snapshot',action='store_true')
    sub.add_parser('init-encrypted'); sub.add_parser('encryption-status')
    q=sub.add_parser('migrate-encrypted'); q.add_argument('--destination',required=True); q.add_argument('--backup',required=True)
    a=p.parse_args(argv)
    from . import encryption
    from .db import EncryptionUnavailable, session_key
    root=Path(a.archive).expanduser().resolve()
    if a.cmd=='init-encrypted':
        password=getpass.getpass('New archive password: ')
        confirm=getpass.getpass('Confirm password: ')
        if password!=confirm:
            print('init-encrypted: passwords do not match',file=sys.stderr); return 1
        try: result=encryption.create(root,password)
        except (encryption.EncryptionError,EncryptionUnavailable,OSError) as exc:
            print(f'init-encrypted: {exc}',file=sys.stderr); return 1
        print(json.dumps(result,indent=2)); return 0
    if a.cmd=='encryption-status':
        print(json.dumps(encryption.status(root),indent=2)); return 0
    if a.cmd=='migrate-encrypted':
        from . import migration
        password=getpass.getpass('New encrypted archive password: ')
        confirm=getpass.getpass('Confirm password: ')
        if password!=confirm:
            print('migrate-encrypted: passwords do not match',file=sys.stderr); return 1
        try:
            result=migration.migrate(root,a.destination,a.backup,password)
        except (migration.MigrationError,encryption.EncryptionError,
                EncryptionUnavailable,OSError,ValueError) as exc:
            print(f'migrate-encrypted: {exc}',file=sys.stderr); return 1
        print(json.dumps(result,indent=2))
        print(result['warning'],file=sys.stderr)
        return 0
    if a.cmd not in ('serve','verify-backup','restore') and encryption.is_encrypted_archive(root):
        try: encryption.unlock(root,getpass.getpass('Archive password: '))
        except (encryption.EncryptionError,EncryptionUnavailable) as exc:
            print(f'unlock: {exc}',file=sys.stderr); return 1
    if a.cmd=='migrate':
        from .migrations import report
        print(json.dumps(report(Path(a.archive).expanduser().resolve(),a.dry_run,not a.no_snapshot),indent=2))
        return 0
    if a.cmd in ('backup','verify-backup','restore'):
        from . import backup
        backup_key=None; snapshot_root=None
        try:
            if a.cmd in ('verify-backup','restore'):
                snapshot_root=Path(a.snapshot).expanduser().resolve()
                if encryption.is_encrypted_archive(snapshot_root):
                    try: encryption.unlock(snapshot_root,getpass.getpass('Backup password: '))
                    except (encryption.EncryptionError,EncryptionUnavailable) as exc:
                        print(f'{a.cmd}: {exc}',file=sys.stderr); return 1
                    backup_key=session_key(snapshot_root)
            if a.cmd=='backup': result=backup.backup(a.archive,a.destination)
            elif a.cmd=='verify-backup':
                result=backup.validate_snapshot(snapshot_root,key=backup_key)
            else:
                result=backup.restore(snapshot_root,a.archive,key=backup_key)
        except (ValueError,FileExistsError,OSError,sqlite3.Error) as exc:
            print(f'{a.cmd}: {exc}',file=sys.stderr); return 1
        finally:
            if snapshot_root is not None:
                encryption.lock(snapshot_root)
        print(json.dumps(result))
        if result.get('warning'): print(result['warning'],file=sys.stderr)
        return 0
    if a.cmd=='serve' and encryption.is_encrypted_archive(root):
        from .ipc import IPCServer
        IPCServer(root).serve()
        return 0
    if a.cmd=='serve' and not (root/'archive.sqlite3').is_file():
        # The desktop asks for archive_status before creating anything. Do not
        # instantiate Archive here, because that would silently create the
        # hidden default archive on first launch.
        from .ipc import IPCServer
        IPCServer(root).serve()
        return 0
    ar=Archive(a.archive)
    atexit.register(ar.db.close)
    if a.cmd=='import':
        def progress(done,total,path,status): print(f'[{done}/{total}] {path} {status}',file=sys.stderr)
        result=ar.import_files(a.files,progress=progress)
        for h in result['hashes']: print(h)
        if result['errors']: print(json.dumps(result['errors'],indent=2),file=sys.stderr)
        return int(bool(result['errors']) or result['cancelled'])
    elif a.cmd=='import-history':
        keys=('id','source','source_key','started_at','finished_at','status','files_seen',
              'records_seen','records_new','records_existing','records_failed','detail')
        print(json.dumps([dict(zip(keys,row)) for row in ar.import_history(a.limit)],indent=2))
    elif a.cmd=='reindex': print(ar.reindex())
    elif a.cmd=='recover-orphans': print(json.dumps(ar.recover_orphans()))
    elif a.cmd=='mbox':
        from . import mbox as mbox_module
        try:
            if a.mcmd=='preview':
                print(json.dumps(mbox_module.preview(a.path),indent=2)); return 0
            if a.mcmd=='export':
                exported=mbox_module.export_mbox(ar,a.destination,
                                                 hashes=a.hash or None,
                                                 account=a.account)
                print(json.dumps(exported,indent=2))
                print('WARNING: an exported MBOX file contains plaintext message '
                      'content and is not protected by database encryption.',
                      file=sys.stderr)
                return 0 if not exported['skipped'] and not exported['cancelled'] else 1
            def _report(done, imported, existing, failed):
                print(f'mbox: message {done} imported={imported} '
                      f'existing={existing} failed={failed}', file=sys.stderr)
            result=mbox_module.import_mbox(ar,a.path,progress=_report)
        except mbox_module.MboxError as exc:
            print(f'mbox: {exc}',file=sys.stderr); return 1
        print(json.dumps(result,indent=2))
        return 0 if not result['failed'] else 1
    elif a.cmd=='capabilities':
        print(json.dumps(capabilities(),indent=2)); return 0
    elif a.cmd=='health':
        diagnosis=ar.health(); print(json.dumps(diagnosis,indent=2))
        catalog=diagnosis.get('catalog') or {}
        return int(not diagnosis['sqlite_ok'] or any(diagnosis[k] for k in ('foreign_key_errors','index_missing','index_extra','index_duplicate_rows','raw_missing','raw_unindexed','raw_invalid')) or (catalog.get('available') and not catalog['consistent']))
    elif a.cmd=='search': print(json.dumps(ar.find(a.query),indent=2))
    elif a.cmd=='search-all':
        from .unified import search_unified
        sources=tuple(x.strip() for x in a.sources.split(',') if x.strip()) or ('email','facebook')
        print(json.dumps(search_unified(ar.root,a.query,a.limit,sources),indent=2))
    elif a.cmd=='verify':
        bad=ar.verify(); print(json.dumps(bad)); return int(bool(bad))
    elif a.cmd=='facebook':
        from .facebook import preview, import_files
        if a.fcmd=='preview': print(json.dumps(preview(a.paths,ar.root),indent=2))
        elif a.fcmd=='import': print(json.dumps(import_files(a.paths,ar.root),indent=2))
        elif a.fcmd=='verify':
            from .catalog import catalog_health
            report=catalog_health(ar.root); print(json.dumps(report,indent=2))
            return int(report.get('available') and not report['consistent'])
        elif a.fcmd=='reindex':
            from .catalog import Catalog
            c=Catalog(ar.root)
            try: indexed=c.reindex()
            finally: c.close()
            print(json.dumps({'records_reindexed':indexed}))
        else:
            from .catalog import Catalog
            c=Catalog(ar.root)
            try:
                rows=c.list_records('facebook',a.kind,a.actor,a.limit) if a.fcmd=='list' else c.search_records(a.query,'facebook',a.limit)
            finally:
                c.close()
            print(json.dumps([dict(zip(('record_id','source_key','record_kind','actor','occurred_at','body'),r)) for r in rows],indent=2))
    elif a.cmd=='documents':
        from .documents import preview, import_files
        if a.dcmd=='preview': print(json.dumps(preview(a.paths),indent=2))
        else: print(json.dumps(import_files(a.paths,ar.root),indent=2))
    elif a.cmd=='summarize':
        from .summarize import Summarizer, OllamaError
        s=Summarizer(ar.root)
        try:
            if a.scmd=='run':
                if not s.enabled(): print('summarize: AI is disabled; set ai.enabled and ai.backend=ollama in preferences', file=sys.stderr); return 1
                def progress(done,total,digest,status): print(f'[{done}/{total}] {digest[:12]} {status}', file=sys.stderr)
                try:
                    if a.all:
                        result=s.summarize_all(a.type, model=a.model, prompt_version=a.prompt_version, force=a.force, progress=progress)
                    else:
                        if not a.hashes: print('summarize: specify message hashes or --all', file=sys.stderr); return 2
                        result=s.summarize_batch(a.hashes, a.type, model=a.model, prompt_version=a.prompt_version, force=a.force, progress=progress)
                except OllamaError as e:
                    print(f'summarize: Ollama error: {e} (is Ollama running at {s.prefs["ai_endpoint"]}?)', file=sys.stderr); return 2
                print(json.dumps(result,indent=2))
            elif a.scmd=='list': print(json.dumps(s.list_summaries(a.hash),indent=2))
            elif a.scmd=='delete': print(json.dumps(s.delete(a.hash, a.type, a.model, a.prompt_version),indent=2))
        finally:
            s.close()
    elif a.cmd=='export':
        print('export: exported messages and evidence are plaintext',file=sys.stderr)
        ar.export(a.hashes,a.destination)
    elif a.cmd=='sync':
        password=getpass.getpass()
        if a.all_folders:
            print(json.dumps(ar.sync_mailboxes(a.host,a.user,password),indent=2))
        else:
            print(ar.sync(a.host,a.user,password,a.folder))
    elif a.cmd=='serve':
        from .ipc import IPCServer
        IPCServer(ar.root).serve()
    elif a.cmd=='schedule':
        from . import schedule
        schedule.main(['--archive',str(ar.root),*a.args])
    elif a.cmd=='gui':
        from .tk.app import launch
        launch(ar)
    return 0
if __name__=='__main__': raise SystemExit(main())
