# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import tempfile, unittest, threading, time
from pathlib import Path
from unittest import mock
from hb_engine.dispatch import Archive
from hb_engine.search_worker import SearchController, open_readonly, find_readonly, recent_readonly, stats_readonly

class RecordingScheduler:
    def __init__(self):
        self.calls=[]
        self.cancelled=[]
    def __call__(self, delay_ms, fn):
        self.calls.append((delay_ms, fn))
        return len(self.calls)
    def cancel(self, after_id):
        self.cancelled.append(after_id)

class ReadonlyParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.a=Archive(Path(self.tmp.name)/'archive')
        self.a.ingest(b'From: a@example.invalid\r\nSubject: Silver invoice\r\n\r\nBullion receipt')
        self.a.ingest(b'From: b@example.invalid\r\nSubject: =1+1\r\n\r\nUnrelated')
        self.a.ingest(b'Content-Type: text/plain; charset=x-does-not-exist\r\n\r\ncaf\xe9 bill\r\n')
    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()
    def test_readonly_helpers_match_archive(self):
        conn=open_readonly(self.a.root)
        try:
            for q in ('invoice','silver','bill','a:b','-invoice','"unterminated','OR','*'):
                self.assertEqual(find_readonly(conn,q),self.a.find(q))
            self.assertEqual(recent_readonly(conn,100),self.a.recent(100))
            self.assertEqual(recent_readonly(conn,1),self.a.recent(1))
            self.assertEqual(stats_readonly(conn),self.a.stats())
        finally:
            conn.close()

class SearchControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.a=Archive(Path(self.tmp.name)/'archive')
        self.h=self.a.ingest(b'From: a@example.invalid\r\nSubject: Silver invoice\r\n\r\nBullion receipt')
        self.sched=RecordingScheduler()
        self.controllers=[]
    def controller(self, query_fn):
        c=SearchController(self.a.root, query_fn, self.sched, self.sched.cancel)
        c.on_result=mock.Mock(); c.on_error=mock.Mock()
        self.controllers.append(c)
        return c
    def tearDown(self):
        for c in self.controllers:
            c.close()
        self.a.db.close(); self.tmp.cleanup()
    def drive(self, c, timeout=2.0):
        deadline=time.time()+timeout
        while time.time()<deadline:
            c._poll()
            if not c.busy: return
            time.sleep(0.01)
        raise AssertionError('controller did not settle')
    def test_roundtrip(self):
        done=threading.Event()
        def qf(conn, term):
            done.set()
            return find_readonly(conn, term)
        c=self.controller(qf)
        c.submit('invoice')
        self.assertTrue(c.busy)
        self.assertTrue(done.wait(2))
        self.drive(c)
        self.assertFalse(c.busy)
        hits,stats=c.on_result.call_args[0]
        self.assertEqual([h for h,_,_ in hits],[self.h])
        self.assertEqual(stats['messages'],1)
        c.on_error.assert_not_called()
    def test_stale_result_dropped(self):
        gate=threading.Event()
        def qf(conn, term):
            gate.wait(2)
            return []
        c=self.controller(qf)
        c.submit('first')
        c.submit('second')
        stats={'messages':1,'origins':0}
        c._results.put(('ok',1,[('stale','S','F')],stats))
        c._poll()
        self.assertTrue(c.busy)
        c.on_result.assert_not_called()
        c._results.put(('ok',2,[('fresh','S','F')],stats))
        c._poll()
        self.assertFalse(c.busy)
        c.on_result.assert_called_once_with([('fresh','S','F')],stats)
        c.cancel()
        gate.set()
        self.assertTrue(c.close())
    def test_destroy_waits_for_in_flight_queries(self):
        gate=threading.Event()
        finished=threading.Event()
        def qf(conn, term):
            gate.wait(2)
            finished.set()
            return []
        c=self.controller(qf)
        c.submit('x')
        threading.Timer(0.05, gate.set).start()
        c.destroy()
        self.assertTrue(finished.is_set())
        self.assertFalse(c._threads)
        with self.assertRaises(RuntimeError):
            c.submit('y')

    def test_error_re_enables(self):
        def qf(conn, term):
            raise RuntimeError('boom')
        c=self.controller(qf)
        c.submit('x')
        self.drive(c)
        self.assertFalse(c.busy)
        c.on_error.assert_called_once()
        self.assertIsInstance(c.on_error.call_args[0][0],RuntimeError)
        c.on_result.assert_not_called()
    def test_cancel_clears_busy(self):
        gate=threading.Event()
        def qf(conn, term):
            gate.wait(2)
            return []
        c=self.controller(qf)
        c.submit('x')
        self.assertTrue(c.busy)
        c.cancel()
        self.assertFalse(c.busy)
        c._poll()
        self.assertFalse(c.busy)
        gate.set()
        self.assertTrue(c.close())
