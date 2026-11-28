# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Nested boolean search: set semantics, metadata rules and exact totals."""
import tempfile, unittest
from pathlib import Path
from hb_engine.advanced_search import search_advanced
from hb_engine.catalog import Catalog
from hb_engine.dispatch import Archive
from hb_engine.research import Research


def message(sender, subject, body, date):
    headers = [f'From: {sender}', f'Subject: {subject}']
    if date is not None:
        headers.append(f'Date: {date}')
    return ('\r\n'.join(headers) + f'\r\n\r\n{body}').encode()


def text(value):
    return {'type': 'rule', 'kind': 'text', 'value': value}


def group(op, *children):
    return {'type': 'group', 'op': op, 'children': list(children)}


class AdvancedSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)
        self.gold = self.archive.ingest(message(
            'a@example.invalid', 'Gold invoice', 'bullion receipt',
            'Mon, 01 Jan 2024 10:00:00 +0000'))
        self.silver = self.archive.ingest(message(
            'b@example.invalid', 'Silver invoice', 'silver receipt',
            'Thu, 01 Feb 2024 10:00:00 +0000'))
        self.memo = self.archive.ingest(message(
            'c@example.invalid', 'Gold memo', 'gold note',
            'Fri, 01 Mar 2024 10:00:00 +0000'))
        self.undated = self.archive.ingest(message(
            'd@example.invalid', 'Gold undated', 'gold undated note', None))
        catalog = Catalog(self.root)
        try:
            with catalog.db:
                catalog.add_file('file-hash', 'facebook', 'conversation-1',
                                 '/tmp/conversation-1.json', 10,
                                 '2024-01-02T00:00:00+00:00')
                catalog.add_record('r1', 'facebook', 'conversation-1', 'post', 'Alice',
                                   '2024-01-15T10:00:00+00:00', 'second',
                                   'gold catalog post', '{}', 'file-hash',
                                   '2024-01-02T00:00:00+00:00')
                catalog.add_record('r2', 'facebook', 'conversation-1', 'comment', 'Bob',
                                   '2024-02-15T10:00:00+00:00', 'second',
                                   'silver catalog receipt', '{}', 'file-hash',
                                   '2024-01-02T00:00:00+00:00')
        finally:
            catalog.close()
        self.research = Research(self.archive)
        self.research.tag(self.gold, 'gold')
        self.research.tag(self.gold, 'invoice')

    def tearDown(self):
        self.research.close()
        self.archive.db.close()
        self.tmp.cleanup()

    def ids(self, result):
        return {(('catalog' if item['source'] == 'facebook' else item['source']),
                 item['id']) for item in result['items']}

    def test_nested_and_or(self):
        result = search_advanced(self.root, group(
            'and', group('or', text('gold'), text('silver')),
            {'type': 'rule', 'kind': 'actor', 'values': ['a@example.invalid']}))
        self.assertEqual(self.ids(result), {('email', self.gold)})
        self.assertEqual(result['total'], 1)

    def test_not_is_exact_complement(self):
        result = search_advanced(self.root, group('not', text('gold')))
        self.assertEqual(self.ids(result), {('email', self.silver), ('catalog', 'r2')})
        self.assertEqual(result['total'], 2)

    def test_nested_not_inside_and(self):
        result = search_advanced(self.root, group(
            'and', group('or', text('gold'), text('silver')),
            group('not', {'type': 'rule', 'kind': 'actor', 'values': ['Alice']})))
        self.assertEqual(self.ids(result), {
            ('email', self.gold), ('email', self.silver),
            ('email', self.memo), ('email', self.undated), ('catalog', 'r2')})

    def test_document_source_scope_is_specific(self):
        catalog = Catalog(self.root)
        try:
            with catalog.db:
                catalog.add_file('doc-hash', 'document', 'notes.txt',
                                 '/tmp/notes.txt', 10, '2024-01-02T00:00:00+00:00')
                catalog.add_record('doc1', 'document', 'notes.txt', 'document', None,
                                   '2024-01-15T10:00:00+00:00', 'second',
                                   'gold document body', '{}', 'doc-hash',
                                   '2024-01-02T00:00:00+00:00')
        finally:
            catalog.close()
        documents = search_advanced(self.root, text('gold'), sources=('document',))
        self.assertEqual(self.ids(documents), {('document', 'doc1')})
        self.assertEqual(documents['total'], 1)
        rule = search_advanced(self.root, {'type': 'rule', 'kind': 'source',
                                           'values': ['document']},
                               sources=('email', 'facebook', 'document'))
        self.assertEqual(self.ids(rule), {('document', 'doc1')})

    def test_source_kind_and_date_rules(self):
        result = search_advanced(self.root, group(
            'and', {'type': 'rule', 'kind': 'source', 'values': ['facebook']},
            {'type': 'rule', 'kind': 'kind', 'values': ['comment']},
            {'type': 'rule', 'kind': 'date', 'after': 1704067200, 'before': 1709251200}))
        self.assertEqual(self.ids(result), {('catalog', 'r2')})
        result = search_advanced(self.root, {'type': 'rule', 'kind': 'actor',
                                             'values': ['b@example.invalid']})
        self.assertEqual(self.ids(result), {('email', self.silver)})

    def test_date_rule_excludes_undated_records(self):
        result = search_advanced(self.root, group(
            'and', text('gold'),
            {'type': 'rule', 'kind': 'date', 'after': 0}))
        self.assertEqual(self.ids(result), {('email', self.gold), ('email', self.memo),
                                            ('catalog', 'r1')})
        negated = search_advanced(self.root, group(
            'and', text('gold'), group('not', {
                'type': 'rule', 'kind': 'date', 'after': 0})))
        self.assertEqual(self.ids(negated), {('email', self.undated)})

    def test_tag_any_and_all(self):
        any_tag = search_advanced(self.root, {'type': 'rule', 'kind': 'tag',
                                              'values': ['gold']})
        self.assertEqual(self.ids(any_tag), {('email', self.gold)})
        all_tags = search_advanced(self.root, {'type': 'rule', 'kind': 'tag',
                                               'values': ['gold', 'invoice'],
                                               'mode': 'all'})
        self.assertEqual(self.ids(all_tags), {('email', self.gold)})
        missing = search_advanced(self.root, {'type': 'rule', 'kind': 'tag',
                                              'values': ['gold', 'missing'],
                                              'mode': 'all'})
        self.assertEqual(missing['items'], [])

    def test_exact_total_and_pagination(self):
        result = search_advanced(self.root, text('receipt'), limit=1, offset=1)
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['has_more'])
        last = search_advanced(self.root, text('receipt'), limit=1, offset=2)
        self.assertFalse(last['has_more'])
        self.assertEqual(last['total'], 3)

    def test_global_source_scope_restricts_every_rule(self):
        result = search_advanced(self.root, {'type': 'rule', 'kind': 'source',
                                             'values': ['email']},
                                 sources=('facebook',))
        self.assertEqual(result['items'], [])

    def test_validation_errors_are_specific(self):
        cases = [
            (group('and'), 'at least one child'),
            (group('xor', text('gold')), 'group op'),
            (group('not', text('gold'), text('silver')), 'exactly one child'),
            (text(''), 'non-empty expression'),
            ({'type': 'rule', 'kind': 'kind', 'values': ['bogus']}, 'unsupported'),
            ({'type': 'rule', 'kind': 'date'}, 'needs after'),
            ({'type': 'rule', 'kind': 'source', 'values': ['other']}, 'email, facebook or document'),
            ({'type': 'rule', 'kind': 'tag', 'values': ['x'], 'mode': 'some'}, 'any or all'),
            ('text', 'must be an object'),
        ]
        for tree, expected in cases:
            with self.assertRaises(ValueError) as caught:
                search_advanced(self.root, tree)
            self.assertIn(expected, str(caught.exception))

    def test_depth_limit_and_malformed_fts(self):
        deep = text('gold')
        for _ in range(7):
            deep = group('not', deep)
        with self.assertRaises(ValueError) as caught:
            search_advanced(self.root, deep)
        self.assertIn('nesting is limited', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            search_advanced(self.root, text('('))
        self.assertIn('index rejected', str(caught.exception))
        self.assertIn('quotes', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
