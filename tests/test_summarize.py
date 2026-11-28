# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import http.server, json, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
from hb_engine.dispatch import Archive
from hb_engine.preferences import validate
from hb_engine.summarize import Summarizer, OllamaError

def prefs(enabled=True, backend='ollama', endpoint='http://127.0.0.1:1', **overrides):
    values = {'ai_enabled': enabled, 'ai_backend': backend, 'ai_endpoint': endpoint,
              'ai_model': 'test-model', 'ai_timeout': 5, 'ai_batch_size': 8,
              'ai_max_input_chars': 12000, 'ai_prompt_version': '1'}
    values.update(overrides)
    return values

class FakeOllama:
    def __init__(self, content='A faithful summary.'):
        self.content = content
        self.requests = []
        self.status = 200
        self.redirect_to = None
    def __enter__(self):
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0) or 0)
                body = self.rfile.read(length)
                owner.requests.append((self.path, json.loads(body) if body else None))
                if owner.redirect_to:
                    self.send_response(302)
                    self.send_header('Location', owner.redirect_to)
                    self.end_headers()
                    return
                if owner.status != 200:
                    self.send_response(owner.status)
                    self.end_headers()
                    return
                data = json.dumps({'message': {'content': owner.content}}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self
    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

class SummarizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = Archive(self.root)
        self.h = self.a.ingest(
            b'From: sender@example.invalid\r\nSubject: Silver invoice\r\n'
            b'Message-ID: <test@example.invalid>\r\n'
            b'Date: Thu, 26 Nov 2026 12:00:00 +0000\r\n\r\nBullion receipt')
    def tearDown(self):
        self.a.db.close()
        self.tmp.cleanup()

    def test_disabled_blocks_network(self):
        with FakeOllama() as fake:
            s = Summarizer(self.root, prefs(enabled=False, endpoint=fake.url))
            try:
                self.assertFalse(s.enabled())
                with self.assertRaises(ValueError):
                    s.summarize_message(self.h)
                self.assertEqual(fake.requests, [])
            finally:
                s.close()

    def test_message_summary_writes_row(self):
        with FakeOllama() as fake:
            s = Summarizer(self.root, prefs(endpoint=fake.url))
            try:
                self.assertEqual(s.summarize_message(self.h), 'summarized')
                row = s.db.execute('SELECT summary_text, input_digest FROM summaries').fetchone()
                self.assertEqual(row[0], 'A faithful summary.')
                self.assertTrue(row[1])
                self.assertEqual(len(fake.requests), 1)
                self.assertEqual(fake.requests[0][0], '/api/chat')
                self.assertEqual(self.a.verify(), [])
            finally:
                s.close()

    def test_resume_skips_current(self):
        with FakeOllama() as fake:
            s = Summarizer(self.root, prefs(endpoint=fake.url))
            try:
                self.assertEqual(s.summarize_message(self.h), 'summarized')
                self.assertEqual(s.summarize_message(self.h), 'skipped')
                self.assertEqual(len(fake.requests), 1)
            finally:
                s.close()

    def test_thread_summary_and_growth_regeneration(self):
        reply = self.a.ingest(
            b'From: other@example.invalid\r\nSubject: Re: invoice\r\n'
            b'Message-ID: <reply@example.invalid>\r\nIn-Reply-To: <test@example.invalid>\r\n\r\nReply.\r\n')
        with FakeOllama() as fake:
            s = Summarizer(self.root, prefs(endpoint=fake.url))
            try:
                self.assertEqual(s.summarize_thread(reply), 'summarized')
                row = s.db.execute("SELECT message_hash, thread_members FROM summaries WHERE summary_type='thread'").fetchone()
                self.assertEqual(row[0], self.h)
                self.assertEqual(set(json.loads(row[1])), {self.h, reply})
                self.assertEqual(s.summarize_thread(reply), 'skipped')
                self.a.ingest(
                    b'From: third@example.invalid\r\nMessage-ID: <third@example.invalid>\r\n'
                    b'In-Reply-To: <reply@example.invalid>\r\n\r\nThird.\r\n')
                self.assertEqual(s.summarize_thread(reply), 'regenerated')
                self.assertEqual(len(fake.requests), 2)
            finally:
                s.close()

    def test_connection_failure(self):
        s = Summarizer(self.root, prefs(endpoint='http://127.0.0.1:1'))
        try:
            with self.assertRaises(OllamaError):
                s.summarize_message(self.h)
        finally:
            s.close()

    def test_loopback_restriction(self):
        s = Summarizer(self.root, prefs(endpoint='https://example.com'))
        try:
            with self.assertRaises(ValueError):
                s.summarize_message(self.h)
        finally:
            s.close()

    def test_no_redirect(self):
        with FakeOllama() as fake:
            fake.redirect_to = 'https://example.com/exfiltrate'
            s = Summarizer(self.root, prefs(endpoint=fake.url))
            try:
                with self.assertRaises(OllamaError):
                    s.summarize_message(self.h)
            finally:
                s.close()

    def test_oversized_response(self):
        with FakeOllama(content='x' * 3000) as fake:
            s = Summarizer(self.root, prefs(endpoint=fake.url))
            try:
                with mock.patch('hb_engine.summarize.MAX_RESPONSE', 1024):
                    with self.assertRaises(OllamaError):
                        s.summarize_message(self.h)
            finally:
                s.close()

    def test_preferences_validation(self):
        self.assertTrue(validate({'ai_enabled': True, 'ai_backend': 'ollama'})['ai_enabled'])
        with self.assertRaises(ValueError):
            validate({'ai_backend': 'ollama', 'ai_model': ''})
        with self.assertRaises(ValueError):
            validate({'ai_backend': 'ollama', 'ai_endpoint': 'not-a-url'})
        with self.assertRaises(ValueError):
            validate({'ai_prompt_version': ''})

if __name__ == '__main__':
    unittest.main()
