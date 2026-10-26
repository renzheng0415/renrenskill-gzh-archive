import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gzh.content import parse_content
from gzh.crawl import CrawlState, crawl, CrawlError, QuotaLimited
from gzh.runtime import inspect_archive
from gzh.exporter import KnowledgeBase

HEAD = '<meta property="og:url" content="https://mp.weixin.qq.com/s?__biz=MTIz&amp;mid=1&amp;idx=1&amp;sn=abc"><meta property="og:title" content="测试">'

class ReliabilityTests(unittest.TestCase):
    def test_regular_article(self):
        r = parse_content(HEAD + '<div id="js_content"><p>正文</p></div>')
        self.assertIn('正文', r.content_html)
        self.assertIn('&mid=1', r.meta.url)

    def test_album_text_and_original_images(self):
        raw = HEAD + '<meta property="og:description" content="第一行\\x0a第二行">picture_page_info_list: [{cdn_url: \'https://example.org/a?x=1\\x26amp;y=2\', width: 1280, watermark_info:{cdn_url:\'https://example.org/watermark\'}}]'
        r = parse_content(raw)
        self.assertIn('第二行', r.content_html)
        self.assertEqual(r.images, ['https://example.org/a?x=1&y=2'])
        self.assertNotIn('watermark', r.content_html)

    def test_empty_content_never_success(self):
        with self.assertRaises(Exception): parse_content(HEAD)

    def test_tail_checks_larger_offset(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('gzh.crawl.fetch_page', return_value={'reviews':0,'items':[]}) as fetch:
                result = crawl('MP_WXS_123','demo',root,{'pages_per_run':1},verbose=False)
            self.assertEqual([c.args[1] for c in fetch.call_args_list], [0,20])
            self.assertTrue(result['finished'])

    def test_failed_tail_does_not_mark_finished(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('gzh.crawl.fetch_page', side_effect=[{'reviews':0},CrawlError('offline')]):
                result=crawl('MP_WXS_123','demo',root,{'pages_per_run':1},verbose=False)
            self.assertFalse(result['finished'])

    def test_discovery_saved_before_export(self):
        with tempfile.TemporaryDirectory() as root:
            def fail(*args,**kwargs):
                state=CrawlState.load(Path(root)/'demo'/'.weread-crawl.json')
                self.assertEqual(len(state.seen),1)
                raise RuntimeError('interruption')
            with patch('gzh.crawl.fetch_page',return_value={'reviews':1,'items':[{'url':'weread:opaque','t':1}]}),patch('gzh.crawl.export_new_items',side_effect=fail):
                with self.assertRaises(RuntimeError):crawl('MP_WXS_123','demo',root,{'pages_per_run':1},verbose=False)

    def test_local_quota_does_not_sleep_retry(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('gzh.crawl.fetch_page',side_effect=QuotaLimited('quota')),patch('gzh.crawl.time.sleep') as sleep:
                result=crawl('MP_WXS_123','demo',root,verbose=False)
            sleep.assert_not_called()
            self.assertIn('额度',result['stopped_reason'])

    def test_finished_list_with_missing_article_is_incomplete(self):
        with tempfile.TemporaryDirectory() as root:
            kb=KnowledgeBase(root,'demo');kb.ensure_dirs()
            CrawlState('MP_WXS_123','demo',finished=True,seen={'weread:one':{'title':'missing'}}).save(kb.dir/'.weread-crawl.json')
            r=inspect_archive(root,'demo')
            self.assertTrue(r['total_known']);self.assertFalse(r['complete'])
            self.assertEqual(len(r['unresolved']),1)

if __name__=='__main__':unittest.main()
