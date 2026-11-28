# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
import tempfile,unittest
from pathlib import Path
from hb_engine.dispatch import Archive
from hb_engine.research import Research
class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.a=Archive(Path(self.tmp.name))
        self.h=self.a.ingest(b'From: example@example.invalid\r\nSubject: Silver invoice\r\nDate: Thu, 26 Nov 2026 12:00:00 +0000\r\n\r\nBullion receipt')
        self.other=self.a.ingest(b'Subject: =1+1\r\n\r\nUnrelated')
        self.r=Research(self.a)
    def tearDown(self):self.r.close();self.a.db.close();self.tmp.cleanup()
    def test_workspace_and_search(self):
        self.r.workspace('Metals','Receipts')
        self.r.save_search('Metals','Invoices','silver')
        self.assertEqual([x['hash'] for x in self.r.results('Metals','Invoices')],[self.h])
    def test_rules_and_manual_tags(self):
        self.r.tag(self.h,'personal')
        self.r.save_rule('bullion','subject','contains','SILVER','metal')
        self.assertEqual(self.r.apply_rules(),1)
        self.r.save_search('General','tagged',tag='metal',limit=1)
        self.assertEqual(self.r.results('General','tagged')[0]['hash'],self.h)
        self.r.save_rule('bullion','subject','contains','SILVER','metal',False)
        self.assertEqual(self.r.apply_rules(),0)
        self.assertEqual(self.r.db.execute('select tag from tags').fetchall(),[('personal',)])
        self.assertEqual(self.a.verify(),[])
    def test_fields(self):
        self.r.define_field('State','choice',['Open','Closed'])
        self.r.set_metadata(self.h,'State','Open')
        with self.assertRaises(ValueError):self.r.set_metadata(self.h,'State','bad')
        with self.assertRaises(ValueError):self.r.define_field('State','text')
        self.r.define_field('Cost','number')
        self.r.set_metadata(self.h,'Cost','10.50')
        with self.assertRaises(ValueError):self.r.set_metadata(self.h,'Cost','NaN')
    def test_report(self):
        self.r.save_search('General','all')
        self.r.save_report('export','General','all',['subject','hash'])
        p=Path(self.tmp.name)/'report.csv'
        self.r.export_report('export',p)
        self.assertIn("'=1+1",p.read_text())
        with self.assertRaises(FileExistsError):self.r.export_report('export',p)
        self.r.save_report('years','General','all',['subject'],'year')
        cols,rows=self.r.report('years')
        self.assertEqual(cols,['year','count'])
        self.assertEqual(sum(x['count'] for x in rows),2)
