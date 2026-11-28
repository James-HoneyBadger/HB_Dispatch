# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Observed co-participation graph: real thread links only, no inference."""
import tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
from hb_engine.catalog import Catalog
from hb_engine.dispatch import Archive


def message(sender, subject, date, message_id, reply_to=None):
    headers = [f'From: {sender}', f'Subject: {subject}', f'Date: {date}',
               f'Message-ID: {message_id}']
    if reply_to:
        headers.append(f'In-Reply-To: {reply_to}')
    return ('\r\n'.join(headers) + '\r\n\r\nbody').encode()


def epoch(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)

    def tearDown(self):
        self.archive.db.close()
        self.tmp.cleanup()

    def test_thread_links_create_pairwise_edges(self):
        for sender, digest, reply in (
                ('a@example.invalid', '<m1@example.invalid>', None),
                ('b@example.invalid', '<m2@example.invalid>', '<m1@example.invalid>'),
                ('c@example.invalid', '<m3@example.invalid>', '<m2@example.invalid>')):
            self.archive.ingest(message(sender, 'Thread', 'Mon, 01 Jan 2024 10:00:00 +0000',
                                        digest, reply))
        graph = self.archive.relationship_graph()
        self.assertEqual(len(graph['edges']), 3)
        self.assertTrue(all(edge['threads'] == 1 for edge in graph['edges']))
        self.assertTrue(all(edge['messages'] == 3 for edge in graph['edges']))
        self.assertEqual({node['actor'] for node in graph['nodes']},
                         {'a@example.invalid', 'b@example.invalid', 'c@example.invalid'})
        self.assertTrue(all(node['shared_threads'] == 1 for node in graph['nodes']))
        self.assertFalse(graph['truncated'])

    def test_same_sender_alone_in_thread_is_not_an_edge(self):
        self.archive.ingest(message('a@example.invalid', 'One',
                                    'Mon, 01 Jan 2024 10:00:00 +0000', '<m1@example.invalid>'))
        self.archive.ingest(message('a@example.invalid', 'Two',
                                    'Mon, 01 Jan 2024 11:00:00 +0000', '<m2@example.invalid>',
                                    '<m1@example.invalid>'))
        graph = self.archive.relationship_graph()
        self.assertEqual(graph['edges'], [])
        self.assertEqual(graph['nodes'], [])

    def test_separate_threads_do_not_merge(self):
        self.archive.ingest(message('a@example.invalid', 'One',
                                    'Mon, 01 Jan 2024 10:00:00 +0000', '<m1@example.invalid>'))
        self.archive.ingest(message('b@example.invalid', 'Two',
                                    'Mon, 01 Jan 2024 11:00:00 +0000', '<m2@example.invalid>',
                                    '<m1@example.invalid>'))
        self.archive.ingest(message('c@example.invalid', 'Three',
                                    'Mon, 01 Jan 2024 12:00:00 +0000', '<m3@example.invalid>'))
        self.archive.ingest(message('d@example.invalid', 'Four',
                                    'Mon, 01 Jan 2024 13:00:00 +0000', '<m4@example.invalid>',
                                    '<m3@example.invalid>'))
        graph = self.archive.relationship_graph()
        self.assertEqual({(edge['source_actor'], edge['target_actor'])
                          for edge in graph['edges']},
                         {('a@example.invalid', 'b@example.invalid'),
                          ('c@example.invalid', 'd@example.invalid')})

    def test_date_filter_limits_observation_window(self):
        self.archive.ingest(message('a@example.invalid', 'Old',
                                    'Mon, 01 Jan 2024 10:00:00 +0000', '<m1@example.invalid>'))
        self.archive.ingest(message('b@example.invalid', 'Old reply',
                                    'Mon, 01 Jan 2024 11:00:00 +0000', '<m2@example.invalid>',
                                    '<m1@example.invalid>'))
        self.archive.ingest(message('c@example.invalid', 'New',
                                    'Mon, 01 Jul 2024 10:00:00 +0000', '<m3@example.invalid>'))
        self.archive.ingest(message('d@example.invalid', 'New reply',
                                    'Mon, 01 Jul 2024 11:00:00 +0000', '<m4@example.invalid>',
                                    '<m3@example.invalid>'))
        graph = self.archive.relationship_graph(after=epoch(2024, 6, 1))
        self.assertEqual({(edge['source_actor'], edge['target_actor'])
                          for edge in graph['edges']},
                         {('c@example.invalid', 'd@example.invalid')})
        graph = self.archive.relationship_graph(before=epoch(2024, 6, 1))
        self.assertEqual({(edge['source_actor'], edge['target_actor'])
                          for edge in graph['edges']},
                         {('a@example.invalid', 'b@example.invalid')})

    def test_catalog_conversations_are_grouped_by_source_key(self):
        catalog = Catalog(self.root)
        try:
            with catalog.db:
                for source_key in ('conversation-1', 'conversation-2'):
                    catalog.add_file(f'hash-{source_key}', 'facebook', source_key,
                                     f'/tmp/{source_key}.json', 10,
                                     '2024-01-02T00:00:00+00:00')
                for record_id, source_key, actor in (
                        ('r1', 'conversation-1', 'Alice'),
                        ('r2', 'conversation-1', 'Bob'),
                        ('r3', 'conversation-2', 'Alice'),
                        ('r4', 'conversation-2', 'Carol')):
                    catalog.add_record(record_id, 'facebook', source_key, 'message', actor,
                                       '2024-01-01T10:00:00+00:00', 'second', 'body', '{}',
                                       f'hash-{source_key}', '2024-01-02T00:00:00+00:00')
        finally:
            catalog.close()
        graph = self.archive.relationship_graph()
        pairs = {(edge['source_actor'], edge['target_actor']) for edge in graph['edges']}
        self.assertEqual(pairs, {('Alice', 'Bob'), ('Alice', 'Carol')})
        self.assertTrue(all(edge['source_type'] == 'facebook' for edge in graph['edges']))
        self.assertTrue(all(node['id'].startswith('facebook|') for node in graph['nodes']))
        self.assertEqual(len(graph['definitions']), 5)

    def test_identities_are_not_merged_across_sources(self):
        self.archive.ingest(message('shared@example.invalid', 'Email',
                                    'Mon, 01 Jan 2024 10:00:00 +0000', '<m1@example.invalid>'))
        self.archive.ingest(message('other@example.invalid', 'Email reply',
                                    'Mon, 01 Jan 2024 11:00:00 +0000', '<m2@example.invalid>',
                                    '<m1@example.invalid>'))
        catalog = Catalog(self.root)
        try:
            with catalog.db:
                catalog.add_file('file-hash', 'facebook', 'conversation-1',
                                 '/tmp/conversation-1.json', 10,
                                 '2024-01-02T00:00:00+00:00')
                catalog.add_record('r1', 'facebook', 'conversation-1', 'message',
                                   'shared@example.invalid', '2024-01-01T10:00:00+00:00',
                                   'second', 'body', '{}', 'file-hash',
                                   '2024-01-02T00:00:00+00:00')
                catalog.add_record('r2', 'facebook', 'conversation-1', 'message',
                                   'friend@example.invalid', '2024-01-01T11:00:00+00:00',
                                   'second', 'body', '{}', 'file-hash',
                                   '2024-01-02T00:00:00+00:00')
        finally:
            catalog.close()
        graph = self.archive.relationship_graph()
        node_ids = {node['id'] for node in graph['nodes']}
        self.assertIn('email|shared@example.invalid', node_ids)
        self.assertIn('facebook|shared@example.invalid', node_ids)
        self.assertEqual(len(node_ids), 4)

    def test_participant_and_edge_caps_report_truncation(self):
        for index in range(13):
            sender = f'person{index:02d}@example.invalid'
            reply = '<m0@example.invalid>' if index else None
            self.archive.ingest(message(sender, 'Large thread',
                                        'Mon, 01 Jan 2024 10:00:00 +0000',
                                        f'<m{index}@example.invalid>', reply))
        graph = self.archive.relationship_graph(max_edges=5)
        self.assertTrue(graph['truncated'])
        self.assertLessEqual(len(graph['nodes']), 12)
        self.assertLessEqual(len(graph['edges']), 5)
        self.assertNotIn('person12@example.invalid', {node['actor'] for node in graph['nodes']})

    def test_empty_archive_returns_no_graph(self):
        graph = self.archive.relationship_graph()
        self.assertEqual(graph['nodes'], [])
        self.assertEqual(graph['edges'], [])
        self.assertFalse(graph['truncated'])
        self.assertEqual(len(graph['definitions']), 5)


if __name__ == '__main__':
    unittest.main()
