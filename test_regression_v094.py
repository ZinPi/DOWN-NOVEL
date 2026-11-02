"""Offline regression tests for v0.9.4; optional GUI tests require an X display."""
from __future__ import annotations
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
from bs4 import BeautifulSoup

import auto_web
import truyen_nhat_txt_tool as t


def test_narrative_blocks():
    html = '<article><p>第一段落。</p><blockquote>引用文の重要な内容。</blockquote><p>第二段落。<br>改行された会話。</p></article>'
    block = BeautifulSoup(html, 'html.parser').article
    expected = '第一段落。\n引用文の重要な内容。\n第二段落。\n改行された会話。'
    assert t.extract_block_text(block) == expected
    assert auto_web._extract_text(block) == expected
    ruby = BeautifulSoup('<div><p><ruby>漢字<rt>かんじ</rt></ruby>です。</p><p>次。</p></div>', 'html.parser').div
    assert t.extract_block_text(ruby) == '漢字です。\n次。'


def test_short_windows_names_and_run_collision():
    with tempfile.TemporaryDirectory() as tmp:
        manual = t.safe_windows_name('日本語タイトル' * 200 + '::::A', 48)
        assert len(manual.encode('utf-16-le')) // 2 <= 48
        assert not any(ch in manual for ch in '\\/:*?"<>|')
        assert t.sanitize_filename('CON.txt') == '_CON.txt'
        root = Path(tmp) / manual
        r1 = t.make_run_directory(root, '20261023_160000')
        marker = r1 / 'TAT_CA_EPISODE_DA_CHON.txt'
        t.atomic_write_text(marker, 'FIRST DATA')
        r2 = t.make_run_directory(root, '20261023_160000')
        assert r1 != r2 and marker.read_text('utf-8-sig') == 'FIRST DATA'
        assert len(r1.name) < 80
        assert r2.name.endswith('_001')


def test_cache_checksum_and_rejection():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'cache.json'
        payload = {'url': 'https://test.invalid/episode/1', 'index':1, 'title':'TITLE', 'body':'これは正しい文章の本文です。'*12}
        t.write_cache(path, payload)
        assert t.read_cache(path, payload['url']) == payload
        assert t.read_cache(path, payload['url']+'other') is None
        tampered = json.loads(path.read_text('utf-8'))
        tampered['episode']['body'] = 'INVALID' * 25
        path.write_text(json.dumps(tampered), encoding='utf-8')
        assert t.read_cache(path, payload['url']) is None
        legacy = {'url':payload['url'], 'body':payload['body']}
        path.write_text(json.dumps(legacy), encoding='utf-8')
        assert t.read_cache(path, payload['url']) is None


def test_dynamic_browser_pagination_complete():
    root = 'https://pages.example.org/fiction/story'
    page2 = root+'?page=2'
    def toc(ids, nxt=''):
        header = '<html><head><title>Test novel</title></head><body>'
        links = ''.join(f'<a href="/fiction/story/episode/{i}">第{i}話：新しい物語</a>' for i in ids)
        nextlink = f'<a rel="next" href="{nxt}">Next</a>' if nxt else ''
        return header + links + nextlink + '</body></html>'
    shell = '<html><head><title>Test novel</title></head><body><div id="app"></div></body></html>'
    first = toc([1,2,3], '?page=2')
    second = toc([4,5,6])
    html_ep = '<html><body><article class="episode-body"><p>'+'本文あ' * 100+'</p></article></body></html>'
    class Fake(auto_web.AutoWebAnalyzer):
        def __init__(self, report_dir):
            super().__init__(delay=0.0, stop_event=threading.Event())
            self.report_dir = report_dir
            self.browser_calls = []
        def _http_get(self, url):
            if url == root: return shell, root, 200
            if '/episode/' in url: return html_ep, url, 200
            raise AssertionError('page2 must use browser, not raw HTTP: '+url)
        def _browser_collect(self, urls, folder, progress=None):
            self.browser_calls.extend(urls)
            return [first if url == root else second if url == page2 else html_ep for url in urls], []
        @staticmethod
        def _profile_path(domain):
            return Path(tempfile.gettempdir()) / 'truyen_v094_dynamic_profile.json'
        def _begin_report(self, url):
            folder = self.report_dir
            folder.mkdir(parents=True, exist_ok=True)
            self.last_report_folder = str(folder)
            self.last_report_path = str(folder/'AUTO_ANALYSIS_REPORT.json')
            self._report = {'url':url,'domain':'pages.example.org','stages':[], 'result':'RUNNING'}
            self._write_report()
            return folder
    with tempfile.TemporaryDirectory() as tmp:
        tester = Fake(Path(tmp))
        novel = tester.analyze(root)
        assert novel.episode_count == 6, novel.episode_count
        assert root in tester.browser_calls
        assert page2 in tester.browser_calls
        assert tester._report['toc_pagination']['pages'] == 2
        assert tester._report['toc_pagination']['complete']
        assert tester._report['toc_pagination']['mode'] == 'browser'


def test_gui_export_and_url_reset():
    """Run under xvfb-run on Linux, real Tk on Windows; all network mocked."""
    import tkinter as tk
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        app = t.App()
        original_client = t.NarouClient
        url = app.url_var.get().strip()
        novel = t.NovelInfo(ncode='NTEST123', title='超長編タイトル' * 350,
            author='TEST AUTHOR', root_url=url,
            chapters=[t.Chapter(number=1,title='第一章超長い' * 100,episodes=[
                t.Episode(index=i,title='第' + str(i) + '話漢字' * 120,
                          url=url + f'{i}/', chapter_title='CH 1') for i in (1,2,3)])],site='Narou')
        class FakeClient:
            fetched = 0
            def __init__(self, delay=1.0, stop_event=None): pass
            def fetch_episode(self, ep):
                FakeClient.fetched += 1
                body = '本文テスト' * 70 + f' END_{ep.index}'
                return {'index':ep.index, 'title':ep.title, 'url':ep.url,
                        'body':body, 'chapter_title':ep.chapter_title,
                        'subchapter_title':'', 'preface':'', 'afterword':''}
        app.out_var.set(tmp)
        app.folder_name_var.set('サブフォルダ' * 180)
        app.novel = novel
        app._loaded_url = url
        app.episode_spec_var.set('1-3')
        app.output_episode_var.set(True)
        app.output_chapter_var.set(True)
        app.output_all_var.set(True)
        def wait_for_worker():
            deadline = time.monotonic() + 16
            def check():
                if app.worker is not None and not app.worker.is_alive():
                    app.after(100, app.quit)  # Let queued GUI updates finish.
                elif time.monotonic() > deadline:
                    app.quit()
                    raise AssertionError('Export worker hung')
                else:
                    app.after(40, check)
            app.after(40, check)
            app.mainloop()
            assert app.worker is not None and not app.worker.is_alive(), 'Export worker hung'
        try:
            with patch.object(t, 'NarouClient', FakeClient), patch.object(t.messagebox, 'showinfo', lambda *args: None),patch.object(t.messagebox,'showerror',lambda *args: (_ for _ in ()).throw(AssertionError(args))):
                app.download_selected()
                wait_for_worker()
                folder1 = app.last_result_folder
                assert folder1 and (folder1/'_TRANG_THAI_HOAN_TAT.txt').exists()
                assert (folder1/'TAT_CA_EPISODE_DA_CHON.txt').exists()
                assert len(list((folder1/'episodes').glob('*.txt'))) == 3
                assert len(list((folder1/'chapters').glob('*.txt'))) == 1
                assert FakeClient.fetched == 3
                qa = json.loads((folder1/'03_QA_KIEM_TRA_XUAT.json').read_text('utf-8'))
                assert qa['expected_selected'] == qa['written'] == 3
                assert all(len(x['body_sha256']) == 64 for x in qa['episodes'])
                app.download_selected()
                wait_for_worker()
                folder2 = app.last_result_folder
                assert folder2 and folder2 != folder1
                assert FakeClient.fetched == 3, 'second run should reuse verified cache'
                assert (folder1/'TAT_CA_EPISODE_DA_CHON.txt').exists()
                assert (folder2/'_TRANG_THAI_HOAN_TAT.txt').exists()
                app.url_var.set('https://ncode.syosetu.com/n0001zz/')
                app.update()
                assert app.novel is None and app._loaded_url == ''
                assert str(app.btn_download.cget('state')) == 'disabled'
        finally:
            app.destroy()


if __name__ == '__main__':
    test_narrative_blocks()
    test_short_windows_names_and_run_collision()
    test_cache_checksum_and_rejection()
    test_dynamic_browser_pagination_complete()
    print('V094 CORE REGRESSION: PASS')
    import sys
    if '--gui' in sys.argv:
        test_gui_export_and_url_reset()
        print('V094 GUI + OUTPUT + CACHE + URL REGRESSION: PASS')
