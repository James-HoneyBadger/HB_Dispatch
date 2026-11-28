# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import tempfile
import unittest
from pathlib import Path

from hb_engine import entities
from hb_engine.dispatch import Archive

MESSAGES = (
    b'From: Ada Example <ada@example.invalid>\r\nTo: Grace <grace@example.invalid>\r\n'
    b'Subject: Invoice one\r\nDate: Tue, 14 Mar 2023 09:15:00 +0000\r\n\r\nBody one',
    b'From: "Ada Example" <ADA@EXAMPLE.INVALID>\r\nSubject: Invoice two\r\n'
    b'Date: Wed, 15 Mar 2023 10:00:00 +0000\r\n\r\nBody two',
    b'From: Grace Example <grace@example.invalid>\r\nSubject: Reply\r\n'
    b'Date: Thu, 16 Mar 2023 11:00:00 +0000\r\n\r\nBody three',
    b'From: not-an-address\r\nSubject: Junk\r\n\r\nNo address here',
)


class EntityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Archive(Path(self.tmp.name) / 'archive')
        self.hashes = [self.a.ingest(raw) for raw in MESSAGES]

    def tearDown(self):
        self.a.db.close()

    def build(self):
        return entities.build(self.a.db)

    def test_parse_sender_handles_quotes_and_case(self):
        self.assertEqual(entities.parse_sender('"Ada Example" <Ada@Example.INVALID>'),
                         ('Ada Example', 'ada@example.invalid'))
        self.assertEqual(entities.parse_sender('not-an-address'), ('', 'not-an-address'))

    def test_exact_addresses_cluster_into_one_entity(self):
        self.build()
        rows = entities.list_entities(self.a.db)
        ada = [r for r in rows if 'ada@example.invalid' in [
            alias['value'] for alias in entities.aliases(self.a.db, r['entity_id'])]]
        self.assertEqual(len(ada), 1)
        self.assertEqual(ada[0]['mentions'], 2)

    def test_aliases_are_heuristic_and_include_the_display_name(self):
        self.build()
        entity_id = entities.list_entities(self.a.db)[0]['entity_id']
        values = {(a['value'], a['type'], a['confidence'])
                  for a in entities.aliases(self.a.db, entity_id)}
        self.assertTrue(all(confidence == 'heuristic'
                            for _, _, confidence in values))
        self.assertTrue(any(kind == 'email' for _, kind, _ in values))

    def test_messages_without_an_address_are_skipped(self):
        result = self.build()
        self.assertEqual(result['skipped'], 1)

    def test_rebuild_is_idempotent(self):
        self.build()
        first = entities.list_entities(self.a.db)
        self.build()
        self.assertEqual(entities.list_entities(self.a.db), first)

    def test_timeline_returns_records_newest_first(self):
        self.build()
        entity_id = [r['entity_id'] for r in entities.list_entities(self.a.db)
                     if any(a['value'] == 'ada@example.invalid'
                            for a in entities.aliases(self.a.db, r['entity_id']))][0]
        rows = entities.timeline(self.a.db, entity_id)
        self.assertEqual(len(rows), 2)
        self.assertGreaterEqual(rows[0]['timestamp'], rows[1]['timestamp'])
        self.assertEqual(rows[0]['source_type'], 'email')

    def test_activity_by_year_counts_mentions(self):
        self.build()
        entity_id = [r['entity_id'] for r in entities.list_entities(self.a.db)
                     if any(a['value'] == 'ada@example.invalid'
                            for a in entities.aliases(self.a.db, r['entity_id']))][0]
        self.assertEqual(entities.activity_by_year(self.a.db, entity_id),
                         [{'year': '2023', 'mentions': 2}])

    def test_two_entities_sharing_a_record_are_correspondents(self):
        self.build()
        self.a.db.execute(
            "INSERT OR IGNORE INTO entity_mentions(entity_id, source_type, "
            "record_id, role, timestamp) SELECT entity_id, 'email', ?, 'recipient', 0 "
            "FROM entities WHERE canonical_name='Grace Example'", (self.hashes[0],))
        self.a.db.commit()
        rows = {r['entity_id']: r for r in entities.list_entities(self.a.db)}
        ada = [r for r in rows.values() if 'Ada' in r['canonical_name']][0]
        correspondents = entities.top_correspondents(self.a.db, ada['entity_id'])
        self.assertEqual([c['name'] for c in correspondents], ['Grace Example'])

    def test_merge_moves_aliases_and_mentions(self):
        self.build()
        rows = entities.list_entities(self.a.db)
        keep, merge = rows[0]['entity_id'], rows[1]['entity_id']
        before = rows[0]['mentions'] + rows[1]['mentions']
        entities.merge(self.a.db, keep, merge)
        after = {r['entity_id']: r for r in entities.list_entities(self.a.db)}
        self.assertNotIn(merge, after)
        self.assertEqual(after[keep]['mentions'], before)

    def test_merge_rejects_self_and_missing_entity(self):
        self.build()
        entity_id = entities.list_entities(self.a.db)[0]['entity_id']
        with self.assertRaises(ValueError):
            entities.merge(self.a.db, entity_id, entity_id)
        with self.assertRaises(ValueError):
            entities.merge(self.a.db, entity_id, 9999)

    def test_verify_alias_marks_confidence(self):
        self.build()
        entity_id = entities.list_entities(self.a.db)[0]['entity_id']
        alias_id = entities.aliases(self.a.db, entity_id)[0]['alias_id']
        entities.verify_alias(self.a.db, alias_id)
        confirmed = [a for a in entities.aliases(self.a.db, entity_id)
                     if a['alias_id'] == alias_id][0]
        self.assertEqual(confirmed['confidence'], 'verified')

    def test_verify_unknown_alias_raises(self):
        with self.assertRaises(ValueError):
            entities.verify_alias(self.a.db, 4242)

    def test_split_alias_creates_a_new_entity(self):
        self.build()
        entity_id = entities.list_entities(self.a.db)[0]['entity_id']
        alias_id = entities.aliases(self.a.db, entity_id)[0]['alias_id']
        new_id = entities.split_alias(self.a.db, alias_id, 'Separate Person')
        self.assertNotEqual(new_id, entity_id)
        self.assertEqual(len(entities.aliases(self.a.db, new_id)), 1)
