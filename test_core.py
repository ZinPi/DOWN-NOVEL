import json
import threading
from bs4 import BeautifulSoup

from truyen_nhat_txt_tool import (
    KakuyomuClient,
    Episode,
    count_chars_no_whitespace,
    detect_site,
    extract_block_text,
    normalize_kakuyomu_url,
    normalize_narou_url,
    normalize_gamelive_url,
    parse_episode_spec,
    sanitize_filename,
)


def test_common():
    root, ncode = normalize_narou_url("https://ncode.syosetu.com/n7230gm/15/")
    assert root == "https://ncode.syosetu.com/n7230gm/"
    assert ncode == "N7230GM"

    r18_root, r18_ncode = normalize_narou_url("https://novel18.syosetu.com/n4940gb/12/")
    assert r18_root == "https://novel18.syosetu.com/n4940gb/"
    assert r18_ncode == "N4940GB"
    assert detect_site(r18_root) == "Narou R18"

    kroot, kid = normalize_kakuyomu_url(
        "https://kakuyomu.jp/works/2912051604187380632/episodes/2912051604187499803"
    )
    assert kroot == "https://kakuyomu.jp/works/2912051604187380632"
    assert kid == "2912051604187380632"
    assert detect_site(root) == "Narou"
    assert detect_site(kroot) == "Kakuyomu"
    groot, gid = normalize_gamelive_url("https://gamelive.online/vi/app/stories/6a8fe2d342bb7c4da6b8ccac")
    assert groot == "https://gamelive.online/vi/app/stories/6a8fe2d342bb7c4da6b8ccac"
    assert gid == "6a8fe2d342bb7c4da6b8ccac"
    assert detect_site(groot) == "GameLive"
    assert detect_site("https://example.com/novel/abc") == "Auto Web"

    html = '<div class="p-novel__text"><p><ruby>漢字<rt>かんじ</rt></ruby>です。</p><p>次の行。</p></div>'
    soup = BeautifulSoup(html, "html.parser")
    txt = extract_block_text(soup.div)
    assert txt == "漢字です。\n次の行。", repr(txt)
    assert count_chars_no_whitespace("あ い\nう") == 3
    assert sanitize_filename('a:b*c?d"e<f>g|h') == "a_b_c_d_e_f_g_h"
    assert sanitize_filename("COM4") == "_COM4"
    assert sanitize_filename("LPT9") == "_LPT9"
    assert parse_episode_spec("1-32", 40) == list(range(1, 33))
    assert parse_episode_spec("50", 100) == [50]
    assert parse_episode_spec("1,3,10-12", 20) == [1, 3, 10, 11, 12]
    assert parse_episode_spec("32-30", 40) == [30, 31, 32]
    assert parse_episode_spec("90~92", 100) == [90, 91, 92]


def test_kakuyomu_next_data():
    work_id = "2912051604187380632"
    ep1 = "2912051604187499803"
    ep2 = "2912051604187499804"
    state = {
        f"Work:{work_id}": {
            "__typename": "Work",
            "id": work_id,
            "title": "Truyện thử Kakuyomu",
            "author": {"__ref": "UserAccount:1"},
            "tableOfContentsV2": [{"__ref": "TOC:1"}],
        },
        "UserAccount:1": {"__typename": "UserAccount", "activityName": "Tác giả thử"},
        "TOC:1": {
            "__typename": "TableOfContents",
            "chapter": {"__ref": "Chapter:1"},
            "episodeUnions": [{"__ref": f"Episode:{ep1}"}, {"__ref": f"Episode:{ep2}"}],
        },
        "Chapter:1": {"__typename": "Chapter", "id": "1", "level": 1, "title": "第一章"},
        f"Episode:{ep1}": {"__typename": "Episode", "id": ep1, "title": "第1話"},
        f"Episode:{ep2}": {"__typename": "Episode", "id": ep2, "title": "第2話"},
    }
    payload = {
        "query": {"workId": work_id},
        "props": {"pageProps": {"__APOLLO_STATE__": state}},
    }
    work_html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload, ensure_ascii=False)}</script></html>'
    ep_html = '<div class="widget-episodeBody js-episode-body"><p>本文一。</p><p><ruby>剣<rt>けん</rt></ruby>。</p></div>'

    class FakeKakuyomu(KakuyomuClient):
        def __init__(self):
            super().__init__(delay=1.0, stop_event=threading.Event())
        def get(self, url: str) -> str:
            if "/episodes/" in url:
                return ep_html
            return work_html

    client = FakeKakuyomu()
    novel = client.load_toc(f"https://kakuyomu.jp/works/{work_id}")
    assert novel.site == "Kakuyomu"
    assert novel.title == "Truyện thử Kakuyomu"
    assert novel.author == "Tác giả thử"
    assert novel.episode_count == 2
    assert len(novel.chapters) == 1
    assert novel.chapters[0].title == "第一章"
    assert novel.chapters[0].episodes[0].index == 1
    assert novel.chapters[0].episodes[0].source_id == ep1

    data = client.fetch_episode(novel.chapters[0].episodes[0])
    assert data["body"] == "本文一。\n剣。", repr(data["body"])



def test_narou_r18_parser_and_cookie():
    from truyen_nhat_txt_tool import NarouClient

    toc_html = """
    <html><head><title>R18 Test</title></head><body>
      <h1 class="p-novel__title">R18 テスト作品</h1>
      <div class="p-novel__author">作者：テスト作者</div>
      <div class="p-eplist">
        <div class="p-eplist__chapter-title">第一章</div>
        <div class="p-eplist__sublist"><a class="p-eplist__subtitle" href="/n4940gb/1/">第1話</a></div>
        <div class="p-eplist__sublist"><a class="p-eplist__subtitle" href="/n4940gb/2/">第2話</a></div>
        <div class="p-eplist__chapter-title">第二章</div>
        <div class="p-eplist__sublist"><a class="p-eplist__subtitle" href="/n4940gb/3/">第3話</a></div>
        <div class="p-eplist__chapter-title">第一章</div>
        <div class="p-eplist__sublist"><a class="p-eplist__subtitle" href="/n4940gb/4/">第4話</a></div>
      </div>
    </body></html>
    """
    ep_html = """
    <html><body>
      <h1 class="p-novel__title p-novel__title--rensai">第1話</h1>
      <div class="js-novel-text p-novel__text"><p>本文テスト。</p></div>
    </body></html>
    """

    class FakeNarou18(NarouClient):
        def get(self, url: str) -> str:
            self._prepare_session_for_url(url)
            return ep_html if url.rstrip('/').endswith(('/1', '/2', '/3', '/4')) else toc_html

    client = FakeNarou18(delay=1.0, stop_event=threading.Event())
    client._prepare_session_for_url("https://novel18.syosetu.com/n4940gb/")
    assert any(c.name == "over18" and c.value == "yes" for c in client.session.cookies)

    novel = client.load_toc("https://novel18.syosetu.com/n4940gb/")
    assert novel.site == "Narou R18"
    assert novel.ncode == "N4940GB"
    assert novel.episode_count == 4
    # Hai chương cùng tên nhưng không liền nhau phải là hai chương riêng.
    assert [ch.title for ch in novel.chapters] == ["第一章", "第二章", "第一章"]
    assert [len(ch.episodes) for ch in novel.chapters] == [2, 1, 1]

    data = client.fetch_episode(novel.chapters[0].episodes[0])
    assert data["body"] == "本文テスト。"

def test_auto_web_static_analyzer():
    from auto_web import AutoWebAnalyzer, AutoWebClient
    import tempfile
    from pathlib import Path

    root = "https://example.test/works/demo"
    pages = {
        root: """
        <html><head><title>Demo Novel</title><meta name='author' content='Demo Author'></head><body>
          <h1>Demo Novel</h1>
          <div class='toc'>
            <a href='/works/demo/episodes/1'>第1話 はじまり</a>
            <a href='/works/demo/episodes/2'>第2話 つづき</a>
            <a href='/works/demo/episodes/3'>第3話 おわり</a>
          </div>
        </body></html>
        """,
        "https://example.test/works/demo/episodes/1": "<html><body><h1>第1話</h1><div class='chapter-content'><p>本文一です。" + "あ"*250 + "</p><p>次の段落。</p></div></body></html>",
        "https://example.test/works/demo/episodes/2": "<html><body><h1>第2話</h1><div class='chapter-content'><p>本文二です。" + "い"*260 + "</p><p>次の段落。</p></div></body></html>",
        "https://example.test/works/demo/episodes/3": "<html><body><h1>第3話</h1><div class='chapter-content'><p>本文三です。" + "う"*270 + "</p><p>次の段落。</p></div></body></html>",
    }

    class FakeAnalyzer(AutoWebAnalyzer):
        def __init__(self):
            super().__init__(delay=0.5, stop_event=threading.Event())
        def _http_get(self, url):
            if url not in pages:
                raise AssertionError(url)
            return pages[url], url, 200
        @staticmethod
        def _profile_path(domain: str):
            return Path(tempfile.gettempdir()) / "truyen_auto_test_profile.json"
        def _begin_report(self, url: str):
            folder = Path(tempfile.gettempdir()) / "truyen_auto_test_report"
            folder.mkdir(parents=True, exist_ok=True)
            self.last_report_folder = str(folder)
            self.last_report_path = str(folder / "AUTO_ANALYSIS_REPORT.json")
            self._report = {"url": url, "domain": "example.test", "stages": [], "result": "RUNNING"}
            self._write_report()
            return folder

    analyzer = FakeAnalyzer()
    novel = analyzer.analyze(root)
    assert novel.site.startswith("Auto Web")
    assert novel.title == "Demo Novel"
    assert novel.author == "Demo Author"
    assert novel.episode_count == 3
    assert novel.chapters[0].episodes[0].url.endswith("/episodes/1")
    profile = json.loads(Path(novel.profile_path).read_text(encoding="utf-8"))
    assert profile["body_selector"] in {".chapter-content", "div.chapter-content"}
    assert profile["browser_required"] is False
    assert json.loads(Path(novel.analysis_report).read_text(encoding="utf-8"))["result"] == "PASS"


def main():
    test_common()
    test_kakuyomu_next_data()
    test_narou_r18_parser_and_cookie()
    test_auto_web_static_analyzer()
    print("TEST CORE NAROU + NAROU R18 + KAKUYOMU + GAMELIVE + AUTO WEB + CHON EPISODE: PASS")


if __name__ == "__main__":
    main()




def test_auto_web_pagination_over_7_pages():
    from auto_web import AutoWebAnalyzer
    import tempfile
    from pathlib import Path

    pages = {}
    root = "https://paged.test/work/demo"
    episode_urls = []
    for page_no in range(1, 13):
        base = root if page_no == 1 else f"{root}?page={page_no}"
        links = []
        for j in range(2):
            ep_no = (page_no - 1) * 2 + j + 1
            ep_url = f"https://paged.test/work/demo/episodes/{ep_no}"
            episode_urls.append(ep_url)
            links.append(f"<a href='/work/demo/episodes/{ep_no}'>第{ep_no}話</a>")
            pages[ep_url] = f"<html><body><h1>第{ep_no}話</h1><div class='chapter-content'><p>{'本'*260}</p><p>終わり。</p></div></body></html>"
        next_html = "" if page_no == 12 else f"<a rel='next' href='/work/demo?page={page_no+1}'>次へ</a>"
        pages[base] = f"<html><head><title>Long Novel</title><meta name='author' content='Author'></head><body><div class='toc'>{''.join(links)}{next_html}</div></body></html>"

    class FakeAnalyzer(AutoWebAnalyzer):
        def __init__(self):
            super().__init__(delay=0.5, stop_event=threading.Event())
        def _http_get(self, url):
            if url not in pages:
                raise AssertionError(url)
            return pages[url], url, 200
        @staticmethod
        def _profile_path(domain: str):
            return Path(tempfile.gettempdir()) / "truyen_auto_paged_profile_v2.json"
        def _begin_report(self, url: str):
            folder = Path(tempfile.gettempdir()) / "truyen_auto_paged_report_v2"
            folder.mkdir(parents=True, exist_ok=True)
            self.last_report_folder = str(folder)
            self.last_report_path = str(folder / "AUTO_ANALYSIS_REPORT.json")
            self._report = {"url": url, "domain": "paged.test", "stages": [], "result": "RUNNING"}
            self._write_report()
            return folder

    analyzer = FakeAnalyzer()
    novel = analyzer.analyze(root)
    assert novel.episode_count == 24, novel.episode_count
    report = json.loads(Path(novel.analysis_report).read_text(encoding="utf-8"))
    assert report["toc_pagination"]["pages"] == 12
    assert report["toc_pagination"]["complete"] is True


def test_auto_web_rejects_blog_cluster():
    from auto_web import _discover_links, _novel_likeness, NOVEL_LIKENESS_MIN
    html = "<html><body>" + "".join(
        f"<a href='/blog/post/{20260100+i}'>Tin tức ngày {i}</a>" for i in range(1, 9)
    ) + "</body></html>"
    links, groups = _discover_links(html, "https://blog.test/")
    score, detail = _novel_likeness(links, groups)
    assert score < NOVEL_LIKENESS_MIN, (score, detail)


def test_body_selector_prefers_clean_inner_block():
    from auto_web import _discover_body_selector
    htmls = []
    for c in ("あ", "い", "う"):
        htmls.append(
            "<html><body><main>"
            "<div class='chapter-content'><p>" + c*320 + "</p><p>本文続き。</p></div>"
            "<div class='comment-list'><p>comment " + "x"*700 + "</p><p>more</p></div>"
            "</main></body></html>"
        )
    selector, rows = _discover_body_selector(htmls)
    assert selector in {".chapter-content", "div.chapter-content"}, (selector, rows[:5])


def test_profile_version_rejected():
    from auto_web import AutoWebAnalyzer, PROFILE_VERSION
    import tempfile
    from pathlib import Path

    profile_path = Path(tempfile.gettempdir()) / "truyen_stale_profile.json"
    profile_path.write_text(json.dumps({
        "profile_version": PROFILE_VERSION - 1,
        "domain": "stale.test",
        "browser_required": False,
        "body_selector": ".body"
    }), encoding="utf-8")

    class FakeAnalyzer(AutoWebAnalyzer):
        @staticmethod
        def _profile_path(domain: str):
            return profile_path

    assert FakeAnalyzer().load_profile("stale.test") is None


if __name__ == "__main__":
    test_auto_web_pagination_over_7_pages()
    test_auto_web_rejects_blog_cluster()
    test_body_selector_prefers_clean_inner_block()
    test_profile_version_rejected()


def test_browser_auto_scroll_algorithm():
    from auto_web import AutoWebAnalyzer

    class FakePage:
        def __init__(self):
            self.loaded = 4
            self.height = 1200
        def evaluate(self, script):
            if script.lstrip().startswith("() => ({"):
                return {"h": self.height, "links": self.loaded}
            if self.loaded < 20:
                self.loaded += 4
                self.height += 700
            return None
        def wait_for_timeout(self, ms):
            return None

    page = FakePage()
    stats = AutoWebAnalyzer()._auto_scroll_page(page)
    assert stats["links"] == 20, stats
    assert stats["stable_rounds"] >= 3, stats


def test_tales_like_order_and_403_browser_fallback():
    from auto_web import AutoWebAnalyzer, AutoAnalysisError
    import tempfile
    from pathlib import Path

    root = "https://tales-like.test/user/workid"
    ordered = [
        ("a1", "第1話：開始①"),
        ("a2", "第1話：開始②"),
        ("b1", "第2話：続き"),
        ("c1", "２章第１話：再開①"),
        ("c2", "２章第１話：再開②"),
        ("d1", "２章第２話：終盤"),
    ]
    root_html = "<html><head><title>TALES Like</title><meta name='author' content='Author'></head><body>" + "".join(
        f"<a href='/user/workid/episodes/{eid}'>{title}</a>" for eid, title in ordered
    ) + "</body></html>"
    pages = {}
    for eid, title in ordered:
        pages[f"https://tales-like.test/user/workid/episodes/{eid}"] = (
            f"<html><body><h1>{title}</h1><div class='episode-body'><p>{'本'*280}</p><p>続き。</p></div></body></html>"
        )

    class FakeAnalyzer(AutoWebAnalyzer):
        def __init__(self):
            super().__init__(delay=0.5, stop_event=threading.Event())
        def _http_get(self, url):
            if url == root:
                raise AutoAnalysisError("Website trả HTTP 403. Tool dừng an toàn; không vượt đăng nhập/anti-bot/rate-limit.")
            if url not in pages:
                raise AssertionError(url)
            return pages[url], url, 200
        def _browser_collect(self, urls, folder, progress=None):
            assert urls == [root]
            return [root_html], []
        @staticmethod
        def _profile_path(domain: str):
            return Path(tempfile.gettempdir()) / "truyen_tales_like_profile_v2.json"
        def _begin_report(self, url: str):
            folder = Path(tempfile.gettempdir()) / "truyen_tales_like_report_v2"
            folder.mkdir(parents=True, exist_ok=True)
            self.last_report_folder = str(folder)
            self.last_report_path = str(folder / "AUTO_ANALYSIS_REPORT.json")
            self._report = {"url": url, "domain": "tales-like.test", "stages": [], "result": "RUNNING"}
            self._write_report()
            return folder

    analyzer = FakeAnalyzer()
    novel = analyzer.analyze(root)
    got = [ep.url.rsplit('/',1)[-1] for ep in novel.chapters[0].episodes]
    assert got == [x[0] for x in ordered], got
    assert novel.episode_count == len(ordered)
    profile = json.loads(Path(novel.profile_path).read_text(encoding='utf-8'))
    assert profile['browser_required'] is True


if __name__ == "__main__":
    test_tales_like_order_and_403_browser_fallback()


def test_ui_dispatch_accepts_keyword_args():
    from truyen_nhat_txt_tool import App

    calls = []
    class Dummy:
        def after(self, delay, callback):
            assert delay == 0
            callback()

    def target(*args, **kwargs):
        calls.append((args, kwargs))

    App.ui(Dummy(), target, "x", state="normal", enabled=True)
    assert calls == [(("x",), {"state": "normal", "enabled": True})], calls


if __name__ == "__main__":
    test_ui_dispatch_accepts_keyword_args()
