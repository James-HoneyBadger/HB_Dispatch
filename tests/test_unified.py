# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import hashlib, json, tempfile, threading, unittest
from datetime import datetime, timezone
from pathlib import Path
from hb_engine.dispatch import Archive
from hb_engine.facebook import import_files
from hb_engine.unified import search_unified, search_unified_count, search_unified_page

class UnifiedSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.a=Archive(self.root)
        self.a.ingest(b'From: a@example.invalid\r\nSubject: Silver invoice\r\n\r\nBullion receipt')
        posts=self.root/'posts.json'
        posts.write_text(json.dumps({'your_posts':[
            {'timestamp': 1700000000, 'data': [{'post': 'Bought bullion from the dealer'}]},
        ]}))
        import_files([str(posts)], self.root)
    def tearDown(self):
        self.a.db.close(); self.tmp.cleanup()

    def test_searches_both_sources(self):
        results=search_unified(self.root, 'bullion')
        self.assertEqual({r['source'] for r in results},{'email','facebook'})
        email=[r for r in results if r['source']=='email'][0]
        facebook=[r for r in results if r['source']=='facebook'][0]
        self.assertEqual(email['kind'],'message')
        self.assertEqual(facebook['kind'],'post')
        self.assertIn('Bullion receipt',email['snippet'])
        self.assertIn('Bought bullion',facebook['snippet'])

    def test_attachment_metadata_is_searchable(self):
        chat=self.root/'messages/inbox/Chat/message_1.json'
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({'title': 'Chat', 'messages': [{
            'sender_name': 'Alice', 'timestamp_ms': 1700000000000,
            'content': 'No matching body text',
            'files': [{'uri': 'files/tax-document.pdf'}]}]}))
        import_files([str(chat)], self.root)
        results=search_unified(self.root, 'document', sources=('facebook',))
        self.assertEqual(len(results),1)
        self.assertEqual(results[0]['kind'],'message')
        self.assertIn('tax-document.pdf',results[0]['snippet'])

    def test_source_filter(self):
        results=search_unified(self.root, 'bullion', sources=('facebook',))
        self.assertEqual({r['source'] for r in results},{'facebook'})

    def test_malformed_query_returns_empty(self):
        for q in ('a:b','-invoice','*','"unterminated'):
            self.assertIsInstance(search_unified(self.root, q),list)
        self.assertEqual(search_unified(self.root, 'a:b'),[])

    def test_limit(self):
        self.a.ingest(b'From: b@example.invalid\r\nSubject: Another bullion note\r\n\r\nMore bullion')
        self.assertEqual(len(search_unified(self.root, 'bullion', limit=1)),1)

    def test_date_range_filters_both_sources(self):
        self.a.ingest(b'From: b@example.invalid\r\nSubject: Bullion later\r\n'
                      b'Date: Tue, 02 Jan 2024 00:00:00 +0000\r\n\r\nbullion')
        # The Facebook fixture is timestamped 2023-11-14; a 2024 window excludes it.
        results=search_unified(self.root, 'bullion', after=1704067200)
        self.assertEqual({r['source'] for r in results},{'email'})
        self.assertTrue(all(r['source']=='email' for r in results))
        self.assertEqual(search_unified(self.root, 'bullion', after=1704067200, before=1704153600),[])

    def test_sender_and_kind_filters(self):
        email_only=search_unified(self.root,'bullion',senders=['a@example.invalid'])
        self.assertEqual({r['source'] for r in email_only},{'email'})
        catalog_only=search_unified(self.root,'bullion',kinds=['post'])
        self.assertEqual({r['source'] for r in catalog_only},{'facebook'})
        messages_only=search_unified(self.root,'bullion',kinds=['message'])
        self.assertEqual({r['source'] for r in messages_only},{'email'})
        self.assertEqual(search_unified(self.root,'bullion',kinds=['reaction']),[])

    def test_disabled_sources_can_be_excluded_from_search(self):
        digest=self.a.db.execute('SELECT hash FROM messages').fetchone()[0]
        self.a.db.execute('INSERT INTO origins VALUES(?,?,?,?,?)',
                          ('hidden@example.invalid','INBOX','1','1',digest))
        self.a.db.commit()
        self.a.source_set_enabled('email','hidden@example.invalid',False)
        self.a.source_set_enabled('catalog','facebook',False)
        self.assertEqual({r['source'] for r in search_unified(self.root,'bullion')},
                         {'email','facebook'})
        self.assertEqual(search_unified(self.root,'bullion',exclude_disabled=True),[])
        self.assertEqual(search_unified_count(self.root,'bullion',exclude_disabled=True),0)

    def test_message_shared_with_enabled_account_is_not_hidden(self):
        digest=self.a.db.execute('SELECT hash FROM messages').fetchone()[0]
        self.a.db.executemany('INSERT INTO origins VALUES(?,?,?,?,?)',[
            ('hidden@example.invalid','INBOX','1','1',digest),
            ('visible@example.invalid','INBOX','1','1',digest)])
        self.a.db.commit()
        self.a.source_set_enabled('email','hidden@example.invalid',False)
        results=search_unified(self.root,'bullion',sources=('email',),exclude_disabled=True)
        self.assertEqual(len(results),1)
        self.assertEqual(search_unified_count(self.root,'bullion',sources=('email',),
                                             exclude_disabled=True),1)

    def test_message_without_an_account_remains_visible(self):
        self.a.source_set_enabled('email','hidden@example.invalid',False)
        results=search_unified(self.root,'bullion',sources=('email',),exclude_disabled=True)
        self.assertEqual(len(results),1)

    def test_tag_filter_reads_the_research_overlay(self):
        from hb_engine.research import Research
        research=Research(self.a)
        try:
            research.tag(self.a.find('bullion')[0][0],'important')
        finally:
            research.close()
        results=search_unified(self.root,'bullion',tags=['important'])
        self.assertEqual({r['source'] for r in results},{'email'})
        self.assertEqual(len(results),1)

    def test_query_validation_explains_rejections(self):
        from hb_engine.unified import validate_query
        self.assertTrue(validate_query(self.root,'bullion')['valid'])
        self.assertTrue(validate_query(self.root,'"bull ion"')['valid'])
        bad=validate_query(self.root,'a:b')
        self.assertFalse(bad['valid'])
        self.assertIn('AND/OR/NOT',bad['message'])
        self.assertFalse(validate_query(self.root,'   ')['valid'])

    def test_exact_counts(self):
        from hb_engine.unified import search_unified_count
        self.assertEqual(search_unified_count(self.root,'bullion'),2)
        self.assertEqual(search_unified_count(self.root,'bullion',sources=('email',)),1)
        self.assertEqual(search_unified_count(self.root,'bullion',kinds=['post']),1)
        self.assertIsNone(search_unified_count(self.root,'bullion',tags=['important']))

    def test_relevance_orders_more_specific_matches_first(self):
        self.a.ingest(b'From: a@example.invalid\r\nSubject: Bullion\r\n\r\nBullion bullion bullion')
        results=search_unified(self.root, 'bullion', sources=('email',))
        self.assertEqual(len(results),2)
        self.assertIn('bullion bullion bullion', results[0]['snippet'].lower())

class UnifiedPaginationTests(unittest.TestCase):
    """Pages must reach beyond the former 5,000-candidate ceiling."""

    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.tmp.name)
        archive=Archive(cls.root)
        cls.records=5050
        messages=[]
        index=[]
        try:
            for i in range(cls.records):
                digest=hashlib.sha256(f'bulk-{i}'.encode()).hexdigest()
                subject=f'Bulk needle {i}'
                sender='a@example.invalid' if i%2==0 else 'b@example.invalid'
                body=f'needle token {i}'
                stamp=1700000000+i
                date=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
                messages.append((digest,subject,sender,date,None,None,body,
                                 stamp,'second',len(body)))
                index.append((digest,subject,sender,body))
            with archive.db:
                archive.db.executemany(
                    'INSERT INTO messages(hash,subject,sender,date,message_id,reply_to,'
                    'body,sort_ts,date_precision,size) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    messages)
                archive.db.executemany(
                    'INSERT INTO search(hash,subject,sender,body) VALUES(?,?,?,?)',
                    index)
        finally:
            archive.db.close()
        posts=cls.root/'posts.json'
        posts.write_text(json.dumps({'your_posts':[
            {'timestamp':1700000000,'data':[{'post':'needle in catalog'}]}]}))
        import_files([str(posts)],cls.root)
        cls.total=cls.records+1

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def collect(self,page_size=1000,**kwargs):
        items=[]
        offset=0
        while True:
            result=search_unified_page(self.root,'needle',offset=offset,
                                       limit=page_size,**kwargs)
            items.extend(result['items'])
            if not result['has_more']:
                return items
            offset+=len(result['items'])
            if not result['items']:
                self.fail('has_more with an empty page')

    def test_retrieves_records_beyond_the_old_ceiling(self):
        self.assertEqual(search_unified_count(self.root,'needle'),self.total)
        tail=search_unified_page(self.root,'needle',offset=5000,limit=100)
        self.assertEqual(len(tail['items']),self.total-5000)
        self.assertFalse(tail['has_more'])
        items=self.collect(page_size=1000)
        self.assertEqual(len(items),self.total)
        self.assertEqual(len({(item['source'],item['id']) for item in items}),self.total)
        self.assertEqual({item['source'] for item in items},{'email','facebook'})

    def test_pages_are_deterministic_and_do_not_overlap(self):
        first=search_unified_page(self.root,'needle',offset=1200,limit=50)
        again=search_unified_page(self.root,'needle',offset=1200,limit=50)
        self.assertEqual([item['id'] for item in first['items']],
                         [item['id'] for item in again['items']])
        following=search_unified_page(self.root,'needle',offset=1250,limit=50)
        self.assertFalse({item['id'] for item in first['items']} &
                         {item['id'] for item in following['items']})

    def test_filters_apply_before_pagination(self):
        items=self.collect(page_size=500,senders=['a@example.invalid'])
        self.assertEqual(len(items),self.records//2)
        self.assertTrue(all(item['actor']=='a@example.invalid' for item in items))
        dated=search_unified_page(self.root,'needle',offset=0,limit=10,
                                  after=1700001000,before=1700001020)
        self.assertEqual(len(dated['items']),10)
        self.assertTrue(dated['has_more'])

    def test_cancellation_returns_a_partial_page(self):
        event=threading.Event(); event.set()
        result=search_unified_page(self.root,'needle',limit=10,cancel=event)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['items'],[])
        class CancelAfter:
            def __init__(self,remaining): self.remaining=remaining
            def is_set(self):
                self.remaining-=1
                return self.remaining<0
        partial=search_unified_page(self.root,'needle',limit=1000,
                                    cancel=CancelAfter(600))
        self.assertTrue(partial['cancelled'])
        self.assertLess(len(partial['items']),1000)

    def test_malformed_query_returns_an_empty_page(self):
        result=search_unified_page(self.root,'a:b',limit=10)
        self.assertEqual(result['items'],[])
        self.assertFalse(result['cancelled'])


if __name__=='__main__':
    unittest.main()
