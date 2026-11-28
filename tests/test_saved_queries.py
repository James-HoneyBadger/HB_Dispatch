# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Saved advanced query trees in the research overlay."""
import io, tempfile, unittest
from pathlib import Path
from hb_engine.dispatch import Archive
from hb_engine.ipc import IPCServer, PROTOCOL_VERSION
from hb_engine.research import Research


def tree(value='gold'):
    return {'type': 'group', 'op': 'and',
            'children': [{'type': 'rule', 'kind': 'text', 'value': value}]}


def request(method, request_id='1', **params):
    return {'protocol_version': PROTOCOL_VERSION, 'request_id': request_id,
            'method': method, 'params': params}


class SavedQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)
        self.research = Research(self.archive)

    def tearDown(self):
        self.research.close()
        self.archive.db.close()
        self.tmp.cleanup()

    def test_save_list_get_update_and_delete(self):
        saved = self.research.advanced_query_save(
            'General', 'Gold', 'first', tree(), ['email'])
        self.assertEqual(saved['workspace'], 'General')
        self.assertEqual(saved['sources'], ['email'])
        self.assertEqual(saved['tree'], tree())
        listed = self.research.advanced_query_list()
        self.assertEqual([row['name'] for row in listed], ['Gold'])
        updated = self.research.advanced_query_save(
            'General', 'Gold', 'second', tree('silver'), ['email', 'facebook'])
        self.assertEqual(updated['id'], saved['id'])
        self.assertEqual(updated['description'], 'second')
        self.assertEqual(updated['sources'], ['email', 'facebook'])
        self.assertEqual(len(self.research.advanced_query_list()), 1)
        self.assertEqual(self.research.advanced_query_get(saved['id'])['tree'], tree('silver'))
        self.assertEqual(self.research.advanced_query_delete(saved['id']),
                         {'deleted': saved['id']})
        self.assertEqual(self.research.advanced_query_list(), [])
        with self.assertRaises(ValueError):
            self.research.advanced_query_get(saved['id'])

    def test_queries_are_scoped_to_workspaces_and_cascade(self):
        self.research.workspace('Case file', 'Investigation')
        self.research.advanced_query_save('General', 'General query', '', tree())
        self.research.advanced_query_save('Case file', 'Case query', '', tree())
        self.assertEqual([row['name'] for row in
                          self.research.advanced_query_list('Case file')], ['Case query'])
        with self.research.db:
            self.research.db.execute("DELETE FROM workspaces WHERE name='Case file'")
        self.assertEqual([row['name'] for row in self.research.advanced_query_list()],
                         ['General query'])

    def test_invalid_saved_queries_are_rejected(self):
        cases = [
            (('General', 'Name', '', {'type': 'rule', 'kind': 'text', 'value': 'x'},
              ('email',)), 'root must be a group'),
            (('Missing', 'Name', '', tree(), ('email',)), 'Unknown workspace'),
            (('General', '', '', tree(), ('email',)), 'Name'),
            (('General', 'Name', '', tree(), ('other',)), 'Sources'),
            (('General', 'Name', '',
              {'type': 'group', 'op': 'and', 'children': [object()]}, ('email',)), 'JSON'),
            (('General', 'Name', '',
              {'type': 'group', 'op': 'and', 'children': ['x' * 70_000]},
              ('email',)), '65,536'),
        ]
        for args, message in cases:
            with self.assertRaises(ValueError) as caught:
                self.research.advanced_query_save(*args)
            self.assertIn(message, str(caught.exception))


class SavedQueryIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)
        self.server = IPCServer(self.root, out=io.StringIO())

    def tearDown(self):
        self.archive.db.close()
        self.tmp.cleanup()

    def test_save_list_get_update_delete_over_ipc(self):
        save = self.server.handle(request(
            'advanced_query_save', workspace='General', name='Gold',
            description='first', query=tree(), sources=['email']))
        self.assertNotIn('error', save)
        saved = save['result']
        self.assertEqual(saved['tree'], tree())
        listed = self.server.handle(request('advanced_queries_list'))['result']
        self.assertEqual([row['name'] for row in listed['queries']], ['Gold'])
        fetched = self.server.handle(request(
            'advanced_query_get', query_id=saved['id']))['result']
        self.assertEqual(fetched['description'], 'first')
        updated = self.server.handle(request(
            'advanced_query_save', workspace='General', name='Gold',
            description='second', query=tree('silver'), sources=['email', 'facebook']))
        self.assertEqual(updated['result']['id'], saved['id'])
        self.assertEqual(updated['result']['description'], 'second')
        self.assertEqual(self.server.handle(request(
            'advanced_query_delete', query_id=saved['id']))['result'],
            {'deleted': saved['id']})
        self.assertEqual(self.server.handle(request(
            'advanced_queries_list'))['result']['queries'], [])

    def test_ipc_validates_tree_and_ids(self):
        self.assertEqual(self.server.handle(request(
            'advanced_query_save', workspace='General', name='Rule',
            query={'type': 'rule', 'kind': 'text', 'value': 'gold'}
        ))['error']['code'], 'INVALID_PARAMS')
        self.assertEqual(self.server.handle(request(
            'advanced_query_save', workspace='General', name='Empty',
            query={'type': 'group', 'op': 'and', 'children': []}
        ))['error']['code'], 'INVALID_PARAMS')
        self.assertEqual(self.server.handle(request(
            'advanced_query_save', workspace='Missing', name='Name', query=tree()
        ))['error']['code'], 'INVALID_PARAMS')
        self.assertEqual(self.server.handle(request(
            'advanced_query_get', query_id='one'))['error']['code'], 'INVALID_PARAMS')
        self.assertEqual(self.server.handle(request(
            'advanced_query_delete', query_id=0))['error']['code'], 'INVALID_PARAMS')


if __name__ == '__main__':
    unittest.main()
