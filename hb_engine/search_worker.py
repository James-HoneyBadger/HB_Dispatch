# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Background search for the Tk workstation. No widgets are touched off-thread."""
import queue, sqlite3, threading, time
from pathlib import Path
from hb_engine.db import connect as db_connect

def open_readonly(root):
    conn = db_connect(str(Path(root) / 'archive.sqlite3'), timeout=30)
    conn.execute('PRAGMA query_only=ON')
    return conn

def find_readonly(conn, query, limit=100):
    limit = max(1, min(int(limit), 5000))
    try:
        return conn.execute('SELECT hash,subject,sender FROM search WHERE search MATCH ? LIMIT ?', (query, limit)).fetchall()
    except sqlite3.OperationalError:
        phrase = '"' + query.replace('"', '""') + '"'
        try:
            return conn.execute('SELECT hash,subject,sender FROM search WHERE search MATCH ? LIMIT ?', (phrase, limit)).fetchall()
        except sqlite3.OperationalError:
            return []

def recent_readonly(conn, limit=100):
    return conn.execute('SELECT hash,subject,sender FROM messages ORDER BY rowid DESC LIMIT ?', (max(1, min(int(limit), 5000)),)).fetchall()

def stats_readonly(conn):
    return {'messages': conn.execute('SELECT count(*) FROM messages').fetchone()[0],
            'origins': conn.execute('SELECT count(*) FROM origins').fetchone()[0]}

class SearchController:
    """Runs one daemon thread per query and marshals results back with after()."""
    def __init__(self, archive_root, query_fn, after_scheduler, after_canceller=None, interval_ms=75):
        self._root = Path(archive_root)
        self._query_fn = query_fn
        self._after = after_scheduler
        self._after_cancel = after_canceller
        self._interval_ms = interval_ms
        self._results = queue.SimpleQueue()
        self._generation = 0
        self._busy = False
        self._after_id = None
        self._closed = False
        self._threads = []
        self.on_result = None
        self.on_error = None

    @property
    def busy(self):
        return self._busy

    def submit(self, term):
        if self._closed:
            raise RuntimeError('search controller is closed')
        self._generation += 1
        self._busy = True
        gen = self._generation
        thread = threading.Thread(target=self._worker, args=(gen, term), daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()
        if self._after_id is None:
            self._after_id = self._after(self._interval_ms, self._poll)

    def close(self, timeout=5.0):
        """Stop polling and wait for in-flight queries to finish.

        Callers that close the archive must call this first: a worker still
        opening or reading a database would otherwise race with cleanup.
        """
        self._closed = True
        self._generation += 1
        self._busy = False
        if self._after_id is not None:
            if self._after_cancel is not None:
                self._after_cancel(self._after_id)
            self._after_id = None
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        self._threads = [t for t in self._threads if t.is_alive()]
        return not self._threads

    def _worker(self, gen, term):
        try:
            conn = open_readonly(self._root)
            try:
                hits = self._query_fn(conn, term)
                stats = stats_readonly(conn)
            finally:
                conn.close()
            if gen != self._generation:
                return
            self._results.put(('ok', gen, hits, stats))
        except Exception as exc:
            self._results.put(('err', gen, exc))

    def _poll(self):
        self._after_id = None
        try:
            while True:
                item = self._results.get_nowait()
                kind, gen = item[0], item[1]
                if gen != self._generation:
                    continue
                self._busy = False
                if kind == 'ok':
                    if self.on_result:
                        self.on_result(item[2], item[3])
                elif self.on_error:
                    self.on_error(item[2])
                return
        except queue.Empty:
            pass
        if self._busy:
            self._after_id = self._after(self._interval_ms, self._poll)

    def cancel(self):
        self._generation += 1
        self._busy = False

    def destroy(self):
        """Stop polling and wait briefly for in-flight queries on window close.

        Tk calls this from the main thread when the window closes, so the wait
        is bounded to keep the close responsive while still preventing a worker
        from touching the archive after the application has torn it down.
        """
        self.close(timeout=2.0)
