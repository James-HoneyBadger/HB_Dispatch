# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Database connection service: plaintext default and optional SQLCipher."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hb_engine import db, migrations


class DatabaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plaintext_connections_remain_the_default(self):
        path = self.root / 'plain.sqlite3'
        conn = db.connect(path)
        conn.execute('CREATE TABLE evidence(body TEXT)')
        conn.execute('INSERT INTO evidence VALUES (?)', ('plain evidence',))
        conn.commit()
        conn.close()
        self.assertFalse(db.is_encrypted_file(path))
        conn = db.connect(path, readonly=True)
        try:
            self.assertEqual(conn.execute('SELECT body FROM evidence').fetchone()[0],
                             'plain evidence')
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('INSERT INTO evidence VALUES (?)', ('blocked',))
        finally:
            conn.close()

    def test_requested_encryption_never_falls_back_when_binding_is_missing(self):
        path = self.root / 'must-not-be-created.sqlite3'
        with mock.patch('hb_engine.db.importlib.import_module',
                        side_effect=ImportError('sqlcipher3 unavailable')):
            with self.assertRaises(db.EncryptionUnavailable):
                db.connect(path, key=os.urandom(db.KEY_BYTES))
        self.assertFalse(path.exists())

    def test_keys_are_raw_fixed_length_bytes(self):
        for value in ('passphrase', b'short', b'x' * 31, b'x' * 33):
            with self.subTest(value=value):
                with self.assertRaises(db.InvalidDatabaseKey):
                    db.connect(self.root / 'invalid.sqlite3', key=value)

    @unittest.skipUnless(db.encryption_available(),
                         'optional sqlcipher3 binding is not installed')
    def test_encrypted_database_requires_the_correct_raw_key(self):
        path = self.root / 'protected.sqlite3'
        key = os.urandom(db.KEY_BYTES)
        wrong = os.urandom(db.KEY_BYTES)
        conn = db.connect(path, key=key)
        conn.execute('CREATE TABLE secrets(body TEXT)')
        conn.execute('CREATE VIRTUAL TABLE secret_search USING fts5(body)')
        conn.execute('INSERT INTO secrets VALUES (?)', ('synthetic encrypted evidence',))
        conn.execute('INSERT INTO secret_search VALUES (?)', ('synthetic encrypted evidence',))
        conn.commit()
        conn.close()
        self.assertTrue(db.is_encrypted_file(path))
        with self.assertRaises(sqlite3.DatabaseError):
            sqlite3.connect(path).execute('SELECT count(*) FROM secrets').fetchone()
        with self.assertRaises(Exception):
            db.connect(path, key=wrong)
        conn = db.connect(path, key=key, readonly=True)
        try:
            self.assertEqual(
                conn.execute('SELECT body FROM secret_search WHERE secret_search MATCH ?',
                             ('encrypted',)).fetchone()[0],
                'synthetic encrypted evidence')
            with self.assertRaises(Exception):
                conn.execute('INSERT INTO secrets VALUES (?)', ('blocked',))
        finally:
            conn.close()

    @unittest.skipUnless(db.encryption_available(),
                         'optional sqlcipher3 binding is not installed')
    def test_encrypted_migrations_and_native_backup(self):
        path = self.root / 'migrated.sqlite3'
        backup = self.root / 'backup.sqlite3'
        key = os.urandom(db.KEY_BYTES)
        conn = db.connect(path, key=key)
        try:
            outcome = migrations.apply(conn, 'archive')
            self.assertEqual(outcome['applied'], [1, 2, 3, 4, 5, 6, 7, 8, 9])
            conn.execute("INSERT INTO messages(hash,subject,body) VALUES ('h','s','encrypted migration')")
            conn.execute("INSERT INTO search(hash,subject,body) VALUES ('h','s','encrypted migration')")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT hash FROM search WHERE search MATCH 'migration'").fetchone()[0],
                'h')
        finally:
            conn.close()
        source = db.connect(path, key=key)
        target = db.connect(backup, key=key)
        try:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
        finally:
            source.close()
            target.close()
        self.assertTrue(db.is_encrypted_file(backup))
        restored = db.connect(backup, key=key)
        try:
            self.assertEqual(restored.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        finally:
            restored.close()
        with self.assertRaises(Exception):
            db.connect(backup, key=os.urandom(db.KEY_BYTES))


if __name__ == '__main__':
    unittest.main()
