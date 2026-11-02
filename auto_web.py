from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from text_preserve import narrative_text


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MAX_RETRIES = 2
PROFILE_VERSION = 2
MIN_BODY_CHARS = 180
MAX_TOC_PAGES = 120
BROWSER_SCROLL_MAX_ROUNDS = 30
BROWSER_SCROLL_STABLE_ROUNDS = 3
NOVEL_LIKENESS_MIN = 0.58

NOISE_ATTR_RE = re.compile(
    r"(?:^|[-_\s])(comments?|commentlist|comment-list|nav|menu|sidebar|share|social|recommend(?:ed)?|related|"
    r"breadcrumb|pager|pagination|advert(?:isement)?|ads?|footer|header|cookie|modal|popup)(?:$|[-_\s])",
    re.I,
)


class AutoStopRequested(Exception):
    pass


class AutoAnalysisError(RuntimeError):
    def __init__(self, message: str, report_path: str = ""):
        super().__init__(message)
        self.report_path = report_path


@dataclass
class AutoEpisode:
    index: int
    title: str
    url: str
    chapter_title: str = ""
    chapter_no: int = 1
    subchapter_title: str = ""
    source_id: str = ""


@dataclass
class AutoChapter:
    number: int
    title: str
    episodes: list[AutoEpisode] = field(default_factory=list)


@dataclass
class AutoNovelInfo:
    ncode: str
    title: str
    author: str
    root_url: str
    chapters: list[AutoChapter]
    site: str = "Auto Web"
    site_total_chars: int = 0
    site_episode_count: int = 0
    profile_path: str = ""
    analysis_report: str = ""

    @property
    def episode_count(self) -> int:
        return sum(len(ch.episodes) for ch in self.chapters)


@dataclass
class SiteProfile:
    profile_version: int
    domain: str
    browser_required: bool
    body_selector: str
    title_selector: str = ""
    author_selector: str = ""
    episode_group_key: str = ""
    episode_count_tested: int = 0
    confidence: float = 0.0
    created_at: str = ""
    last_root_url: str = ""


@dataclass
class LinkCandidate:
    order: int
    title: str
    url: str
    score: float
    group_key: str
    number: int | None = None


def _clean_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+\n", "\n", text)
    text = re.sub(r"\n[ \t\u3000]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_ruby(root: Tag) -> None:
    for x in root.select("rt, rp"):
        x.decompose()


def _extract_text(block: Tag | None) -> str:
    if block is None:
        return ""
    clone = BeautifulSoup(str(block), "html.parser")
    root = clone.find()
    if root is None:
        return ""
    _strip_ruby(root)
    for bad in list(root.select("script,style,noscript,svg,canvas,nav,header,footer,aside,form")):
        bad.decompose()
    # Gỡ các khối giao diện thường lẫn vào wrapper lớn như main/article.
    # Chỉ dựa vào id/class có từ khóa UI rõ ràng, không xóa theo nội dung văn bản.
    for el in list(root.find_all(True)):
        if el is root or el.parent is None:
            continue
        marker = " ".join([str(el.get("id") or ""), *[str(x) for x in (el.get("class") or [])]])
        if marker.strip() and NOISE_ATTR_RE.search(marker):
            el.decompose()
    return narrative_text(root)


def _count_chars(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _valid_css_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,100}", value or ""))


def _selector_candidates(el: Tag) -> list[str]:
    out: list[str] = []
    el_id = str(el.get("id") or "").strip()
    if _valid_css_token(el_id):
        out.append(f"#{el_id}")
    tag = el.name or "div"
    classes = [str(x) for x in (el.get("class") or []) if _valid_css_token(str(x))]
    for cls in classes[:5]:
        out.append(f"{tag}.{cls}")
        out.append(f".{cls}")
    if tag in {"article", "main"}:
        out.append(tag)
    return out


def _episode_number(text: str, url: str) -> int | None:
    raw = text or ""
    patterns = [
        r"第\s*(\d+)\s*(?:話|章|節|回)",
        r"(?:chapter|episode|part)\s*[-#:]?\s*(\d+)",
        r"(?:chương|tập|phần)\s*[-#:]?\s*(\d+)",
        r"(?:chap|ep)\s*[-#:]?\s*(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.I)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    p = urlparse(url)
    nums = re.findall(r"(?<!\d)(\d{1,8})(?!\d)", p.path)
    if nums:
        try:
            return int(nums[-1])
        except Exception:
            return None
    qnums = [v for k, v in parse_qsl(p.query) if re.search(r"chapter|episode|part|page|p", k, re.I) and v.isdigit()]
    if qnums:
        return int(qnums[-1])
    return None


def _episode_text_score(text: str) -> float:
    t = _clean_text(text)
    score = 0.0
    if re.search(r"第\s*\d+\s*(?:話|章|節|回)", t): score += 8
    if re.search(r"\b(?:chapter|episode|chap|ep|part)\s*[-#:]?\s*\d+", t, re.I): score += 7
    if re.search(r"\b(?:chương|tập|phần)\s*[-#:]?\s*\d+", t, re.I): score += 7
    if re.fullmatch(r"\d{1,5}", t): score += 2
    if 1 <= len(t) <= 120: score += 1
    if len(t) > 220: score -= 4
    if re.search(r"đăng nhập|login|privacy|policy|home|trang chủ|トップ|ログイン|利用規約|作者|profile", t, re.I): score -= 8
    return score


def _group_key(url: str) -> str:
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        path_key = "/"
    elif len(parts) == 1:
        path_key = "/*"
    else:
        # Tập/chapter thường thay đổi ở segment cuối. Giữ parent path để nhóm.
        parent = parts[:-1]
        # Chuẩn hóa ID dài để profile không khóa vào một tác phẩm cụ thể.
        norm = []
        for seg in parent:
            if re.fullmatch(r"\d{6,}|[0-9a-f]{12,}", seg, re.I):
                norm.append("{id}")
            else:
                norm.append(seg.lower())
        path_key = "/" + "/".join(norm) + "/*"
    qkeys = sorted({k.lower() for k, _ in parse_qsl(p.query)})
    if qkeys:
        path_key += "?" + "&".join(qkeys)
    return path_key


def _same_host(url: str, root: str) -> bool:
    return urlparse(url).netloc.lower().split(":")[0] == urlparse(root).netloc.lower().split(":")[0]


def _clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def _find_title_author(soup: BeautifulSoup, fallback: str) -> tuple[str, str, str, str]:
    title = ""
    title_selector = ""
    meta = soup.select_one('meta[property="og:title"]')
    if meta and meta.get("content"):
        title = str(meta.get("content")).strip()
    if not title:
        for sel in ("h1", "[class*=title] h1", "[class*=title]"):
            el = soup.select_one(sel)
            if el:
                text = _clean_text(el.get_text(" ", strip=True))
                if 2 <= len(text) <= 300:
                    title = text
                    title_selector = sel
                    break
    if not title and soup.title:
        title = _clean_text(soup.title.get_text(" ", strip=True))
    title = title or fallback

    author = ""
    author_selector = ""
    meta_author = soup.select_one('meta[name="author"]')
    if meta_author and meta_author.get("content"):
        author = str(meta_author.get("content")).strip()
    if not author:
        for sel in ('[rel="author"]', '[class*="author"]', '[id*="author"]'):
            el = soup.select_one(sel)
            if el:
                text = _clean_text(el.get_text(" ", strip=True))
                text = re.sub(r"^(?:作者|著者|Author|Tác giả)\s*[:：]?\s*", "", text, flags=re.I)
                if 1 <= len(text) <= 160:
                    author = text
                    author_selector = sel
                    break
    return title, author, title_selector, author_selector


def _jsonld_metadata(soup: BeautifulSoup) -> tuple[str, str]:
    title = author = ""
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        expanded: list[dict] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                expanded.extend(x for x in item["@graph"] if isinstance(x, dict))
            if isinstance(item, dict):
                expanded.append(item)
        for item in expanded:
            if not title:
                title = str(item.get("name") or item.get("headline") or "").strip()
            if not author:
                a = item.get("author")
                if isinstance(a, dict): author = str(a.get("name") or "").strip()
                elif isinstance(a, list) and a and isinstance(a[0], dict): author = str(a[0].get("name") or "").strip()
                elif isinstance(a, str): author = a.strip()
            if title and author:
                return title, author
    return title, author


def _discover_links(html: str, root_url: str) -> tuple[list[LinkCandidate], list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(root_url).netloc.lower().split(":")[0]
    seen: set[str] = set()
    candidates: list[LinkCandidate] = []
    for order, a in enumerate(soup.find_all("a")):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = _clean_url(urljoin(root_url, href))
        p = urlparse(url)
        if p.scheme not in {"http", "https"} or p.netloc.lower().split(":")[0] != host:
            continue
        if url == _clean_url(root_url) or url in seen:
            continue
        seen.add(url)
        text = _clean_text(a.get_text(" ", strip=True))
        path_score = 0.0
        if re.search(r"/(?:episodes?|chapters?|chapter|novel|story|stories|read|viewer|part)(?:/|$)", p.path, re.I):
            path_score += 4
        if re.search(r"(?:episode|chapter|part|ep|ch)=\d+", p.query, re.I):
            path_score += 4
        num = _episode_number(text, url)
        if num is not None:
            path_score += 1
        score = _episode_text_score(text) + path_score
        candidates.append(LinkCandidate(order, text or f"Episode {order+1}", url, score, _group_key(url), num))

    groups: dict[str, list[LinkCandidate]] = {}
    for c in candidates:
        groups.setdefault(c.group_key, []).append(c)
    summaries = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        positive = sum(1 for x in rows if x.score >= 5)
        numbered = sum(1 for x in rows if x.number is not None)
        avg = sum(x.score for x in rows) / len(rows)
        score = len(rows) * 1.3 + avg * 2.2 + positive * 2.5 + numbered * 0.6
        if len(rows) > 400 and positive < max(3, len(rows) // 20):
            score -= 20
        summaries.append({"key": key, "count": len(rows), "score": round(score, 3), "positive": positive, "numbered": numbered})
    summaries.sort(key=lambda x: (-x["score"], -x["count"]))
    if not summaries:
        return [], []
    best_key = summaries[0]["key"]
    best = groups[best_key]
    # Với group ít dấu hiệu episode, yêu cầu ít nhất 3 link để giảm nhầm menu.
    if summaries[0]["positive"] == 0 and len(best) < 3:
        return [], summaries

    # Thứ tự DOM/mục lục là nguồn chuẩn. KHÔNG sort toàn cục theo số tập vì
    # nhiều site (ví dụ TALES) lặp số cho các phần ①②③ và reset số khi sang chương mới.
    best.sort(key=lambda x: x.order)
    return best, summaries


def _novel_likeness(links: list[LinkCandidate], group_summaries: list[dict]) -> tuple[float, dict]:
    """Chấm độ giống mục lục truyện để tránh nhận nhầm blog/news/archive."""
    if not links:
        return 0.0, {"reason": "no_links"}
    count = len(links)
    positive_ratio = sum(1 for x in links if x.score >= 5) / count
    numbered = [x.number for x in links if x.number is not None]
    numbered_ratio = len(numbered) / count
    episode_path_ratio = sum(
        1 for x in links
        if re.search(r"/(?:episodes?|chapters?|chapter|novel|story|stories|read|viewer|part)(?:/|$)", urlparse(x.url).path, re.I)
        or re.search(r"(?:episode|chapter|part|ep|ch)=\d+", urlparse(x.url).query, re.I)
    ) / count
    blog_ratio = sum(
        1 for x in links
        if re.search(r"/(?:blog|news|articles?|posts?|category|tags?|archive)(?:/|$)", urlparse(x.url).path, re.I)
    ) / count

    sequentiality = 0.0
    if len(numbered) >= 3:
        nums = sorted(set(numbered))
        if len(nums) >= 3:
            diffs = [b - a for a, b in zip(nums, nums[1:]) if b > a]
            if diffs:
                sequentiality = sum(1 for d in diffs if d == 1) / len(diffs)

    score = 0.0
    if count >= 3: score += 0.16
    if count >= 5: score += 0.08
    if count >= 10: score += 0.04
    score += positive_ratio * 0.28
    score += numbered_ratio * 0.18
    score += episode_path_ratio * 0.18
    score += sequentiality * 0.14
    score -= blog_ratio * 0.50
    if positive_ratio == 0 and episode_path_ratio == 0 and not (numbered_ratio >= 0.8 and sequentiality >= 0.8):
        score -= 0.18
    if count < 3 and positive_ratio < 1.0:
        score -= 0.25
    score = max(0.0, min(1.0, score))
    detail = {
        "score": round(score, 4), "count": count, "positive_ratio": round(positive_ratio, 4),
        "numbered_ratio": round(numbered_ratio, 4), "episode_path_ratio": round(episode_path_ratio, 4),
        "sequentiality": round(sequentiality, 4), "blog_ratio": round(blog_ratio, 4),
        "best_group": (group_summaries[0] if group_summaries else {}),
    }
    return score, detail


def _next_page_url(html: str, current_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ('a[rel="next"]', 'link[rel="next"]'):
        el = soup.select_one(sel)
        if el and el.get("href"):
            return _clean_url(urljoin(current_url, str(el.get("href"))))
    for a in soup.find_all("a"):
        text = _clean_text(a.get_text(" ", strip=True))
        if re.fullmatch(r"(?:次へ|次のページ|下一页|下一頁|Next|NEXT|Sau|Tiếp|›|»)", text, re.I):
            href = a.get("href")
            if href:
                return _clean_url(urljoin(current_url, str(href)))
    return ""


def _body_candidate_selectors(soup: BeautifulSoup) -> set[str]:
    selectors = {
        "article", "main", ".episode-body", ".episodeBody", ".chapter-content", ".chapterContent",
        ".novel-body", ".novelBody", ".entry-content", ".post-content", ".story-content", ".content-body",
        "[itemprop=articleBody]",
    }
    for el in soup.find_all(["article", "main", "section", "div"]):
        text = _extract_text(el)
        if len(text) < MIN_BODY_CHARS:
            continue
        p_count = len(el.find_all("p"))
        if p_count < 2 and len(text) < 700:
            continue
        selectors.update(_selector_candidates(el))
    return {x for x in selectors if x}


def _element_quality(el: Tag | None) -> tuple[float, str]:
    if el is None:
        return (-10**9, "")
    raw_text = _clean_text(el.get_text("\n", strip=True))
    text = _extract_text(el)
    n = len(text)
    if n < MIN_BODY_CHARS:
        return (-10**9, text)
    links = " ".join(a.get_text(" ", strip=True) for a in el.find_all("a"))
    link_ratio = len(links) / max(1, len(raw_text))
    p_count = len(el.find_all("p"))
    noise = len(re.findall(r"login|đăng nhập|privacy|menu|share|comment|目次|ログイン|ホーム|利用規約|関連記事|おすすめ", raw_text[:8000], re.I))
    purity = n / max(1, len(raw_text))

    # Wrapper penalty: nếu một child duy nhất chứa gần toàn bộ phần truyện thì ưu tiên child nhỏ hơn.
    wrapper_penalty = 0.0
    for child in el.find_all(recursive=False):
        if not isinstance(child, Tag):
            continue
        child_text = _extract_text(child)
        if len(child_text) >= max(MIN_BODY_CHARS, int(n * 0.84)):
            wrapper_penalty = 550.0
            break

    score = n + p_count * 45 + purity * 500 - link_ratio * max(1, len(raw_text)) * 2.8 - noise * 360 - wrapper_penalty
    if n > 180000:
        score -= (n - 180000) * 0.8
    return score, text


def _discover_body_selector(htmls: list[str]) -> tuple[str, list[dict]]:
    soups = [BeautifulSoup(x, "html.parser") for x in htmls]
    selector_set: set[str] = set()
    for soup in soups:
        selector_set.update(_body_candidate_selectors(soup))
    results: list[dict] = []
    for sel in selector_set:
        texts: list[str] = []
        scores: list[float] = []
        purities: list[float] = []
        ok = True
        for soup in soups:
            try:
                el = soup.select_one(sel)
            except Exception:
                ok = False; break
            score, body_text = _element_quality(el)
            if score <= -10**8 or el is None:
                ok = False; break
            raw_text = _clean_text(el.get_text("\n", strip=True))
            purity = len(body_text) / max(1, len(raw_text))
            scores.append(score); texts.append(body_text); purities.append(purity)
        if not ok:
            continue
        similarities = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, b = texts[i][:5000], texts[j][:5000]
                similarities.append(SequenceMatcher(None, a, b).ratio())
        similarity = max(similarities) if similarities else 0.0
        specificity_bonus = 0.0
        if sel.startswith("#"):
            specificity_bonus = 420.0
        elif "." in sel or "[" in sel:
            specificity_bonus = 300.0
        elif sel in {"main", "article"}:
            specificity_bonus = -280.0
        final = min(scores) + sum(scores) / len(scores) * 0.15 - similarity * 1500 + specificity_bonus + min(purities) * 240
        results.append({
            "selector": sel,
            "score": round(final, 2),
            "min_chars": min(len(x) for x in texts),
            "max_chars": max(len(x) for x in texts),
            "min_purity": round(min(purities), 4),
            "max_similarity": round(similarity, 4),
        })
    results.sort(key=lambda x: (-x["score"], x["max_chars"], -x["min_purity"]))
    return (results[0]["selector"] if results else ""), results[:30]


def _browser_base_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base) / "Truyen_Nhat_TXT_Tool" / "AutoWeb"
    else:
        root = Path.home() / ".truyen_nhat_txt_tool" / "AutoWeb"
    root.mkdir(parents=True, exist_ok=True)
    return root


class AutoWebAnalyzer:
    def __init__(self, delay: float = 1.5, stop_event=None):
        self.delay = max(0.5, float(delay))
        self.stop_event = stop_event
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,vi,en-US;q=0.8,en;q=0.6",
        })
        self._last_request = 0.0
        self.last_report_path = ""
        self.last_report_folder = ""
        self.last_profile_path = ""
        self._report: dict = {}

    def _check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise AutoStopRequested()

    def _sleep(self):
        elapsed = time.monotonic() - self._last_request
        remain = self.delay + random.uniform(0, 0.2) - elapsed
        while remain > 0:
            self._check_stop()
            step = min(0.2, remain); time.sleep(step); remain -= step

    def _http_get(self, url: str) -> tuple[str, str, int]:
        last = None
        for attempt in range(MAX_RETRIES):
            self._check_stop(); self._sleep()
            try:
                r = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
                self._last_request = time.monotonic()
            except requests.RequestException as exc:
                last = exc
                if attempt + 1 < MAX_RETRIES:
                    time.sleep(1.5 * (attempt + 1)); continue
                raise AutoAnalysisError(f"Lỗi mạng khi đọc {url}: {exc}", self.last_report_path) from exc
            if r.status_code in {401, 403, 429}:
                raise AutoAnalysisError(
                    f"Website trả HTTP {r.status_code}. Tool dừng an toàn; không vượt đăng nhập/anti-bot/rate-limit.",
                    self.last_report_path,
                )
            if r.status_code >= 500 and attempt + 1 < MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1)); continue
            if not r.ok:
                raise AutoAnalysisError(f"Không tải được {url} (HTTP {r.status_code}).", self.last_report_path)
            enc = r.encoding or r.apparent_encoding or "utf-8"
            r.encoding = enc
            return r.text, r.url, r.status_code
        raise AutoAnalysisError(str(last or "Không tải được trang."), self.last_report_path)

    @staticmethod
    def _profile_path(domain: str) -> Path:
        folder = _browser_base_dir() / "profiles"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", domain)
        return folder / f"{safe}.json"

    def load_profile(self, domain: str) -> SiteProfile | None:
        path = self._profile_path(domain)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if int(raw.get("profile_version") or 0) != PROFILE_VERSION:
                return None
            allowed = SiteProfile.__dataclass_fields__.keys()
            profile = SiteProfile(**{k: v for k, v in raw.items() if k in allowed})
            if profile.domain.lower() != domain.lower():
                return None
            return profile
        except Exception:
            return None

    def save_profile(self, profile: SiteProfile) -> Path:
        path = self._profile_path(profile.domain)
        path.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
        self.last_profile_path = str(path)
        return path

    def _begin_report(self, url: str) -> Path:
        host = urlparse(url).netloc.lower().split(":")[0] or "unknown"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        folder = _browser_base_dir() / "reports" / re.sub(r"[^A-Za-z0-9._-]+", "_", host) / stamp
        folder.mkdir(parents=True, exist_ok=True)
        self.last_report_folder = str(folder)
        self.last_report_path = str(folder / "AUTO_ANALYSIS_REPORT.json")
        self._report = {
            "tool": "Truyen Nhat TXT Tool Auto Web Analyzer",
            "profile_version": PROFILE_VERSION,
            "url": url,
            "domain": host,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stages": [],
            "result": "RUNNING",
        }
        self._write_report()
        return folder

    def _stage(self, name: str, status: str, detail: str = "", **extra):
        row = {"name": name, "status": status, "detail": detail}
        row.update(extra)
        self._report.setdefault("stages", []).append(row)
        self._write_report()

    def _write_report(self):
        if not self.last_report_path:
            return
        path = Path(self.last_report_path)
        path.write_text(json.dumps(self._report, ensure_ascii=False, indent=2), encoding="utf-8")
        txt = path.with_suffix(".txt")
        lines = [
            "AUTO WEB ANALYSIS REPORT",
            "=" * 64,
            f"URL: {self._report.get('url','')}",
            f"Domain: {self._report.get('domain','')}",
            f"Kết quả: {self._report.get('result','')}",
            "",
        ]
        for s in self._report.get("stages", []):
            lines.append(f"[{s.get('status')}] {s.get('name')}: {s.get('detail','')}")
        if self._report.get("failure_reason"):
            lines += ["", "LÝ DO THẤT BẠI", "-" * 64, str(self._report["failure_reason"])]
        if self._report.get("profile"):
            lines += ["", "SITE PROFILE", "-" * 64, json.dumps(self._report["profile"], ensure_ascii=False, indent=2)]
        txt.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    def _finish_fail(self, reason: str):
        self._report["result"] = "FAIL"
        self._report["failure_reason"] = reason
        self._write_report()
        raise AutoAnalysisError(f"{reason}\n\nBáo cáo: {self.last_report_path}", self.last_report_path)

    def _save_html(self, folder: Path, name: str, html: str):
        try:
            (folder / name).write_text(html, encoding="utf-8")
        except Exception:
            pass

    def _auto_scroll_page(self, page, progress=None, label: str = "") -> dict:
        """Scroll tới khi DOM/chiều cao ổn định để tải virtual/infinite lists."""
        stable = 0
        rounds = 0
        last_height = -1
        last_links = -1
        while rounds < BROWSER_SCROLL_MAX_ROUNDS and stable < BROWSER_SCROLL_STABLE_ROUNDS:
            self._check_stop()
            rounds += 1
            try:
                metrics = page.evaluate("""() => ({
                    h: Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0),
                    links: document.querySelectorAll('a[href]').length
                })""")
                height = int(metrics.get("h") or 0)
                links = int(metrics.get("links") or 0)
            except Exception:
                break
            if height == last_height and links == last_links:
                stable += 1
            else:
                stable = 0
            last_height, last_links = height, links
            try:
                page.evaluate("window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))")
                page.wait_for_timeout(650)
            except Exception:
                break
            if progress and rounds in {1, 5, 10, 20, 30}:
                progress(f"AUTO WEB: đang quét nội dung cuộn {label} · vòng {rounds} · {links} link")
        return {"rounds": rounds, "stable_rounds": stable, "height": last_height, "links": last_links}

    def _browser_collect(self, urls: list[str], folder: Path, progress=None) -> tuple[list[str], list[dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self._finish_fail("Trang cần JavaScript nhưng máy chưa có Playwright. Chạy INSTALL_AND_RUN.bat.")
            raise exc
        domain = urlparse(urls[0]).netloc.lower().split(":")[0]
        profile_dir = _browser_base_dir() / "browser_profiles" / re.sub(r"[^A-Za-z0-9._-]+", "_", domain)
        profile_dir.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        context = None
        launch_errors = []
        try:
            headless = os.environ.get("TRUYEN_TOOL_HEADLESS", "").strip() == "1"
            for channel in ("chrome", "msedge", None):
                try:
                    kwargs = dict(user_data_dir=str(profile_dir), headless=headless, viewport={"width": 1500, "height": 900}, args=["--disable-blink-features=AutomationControlled"])
                    if channel: kwargs["channel"] = channel
                    context = pw.chromium.launch_persistent_context(**kwargs)
                    break
                except Exception as exc:
                    launch_errors.append(f"{channel or 'playwright-chromium'}: {exc}")
            if context is None:
                # Fallback cho máy có Chromium/Chrome cài hệ thống nhưng Playwright chưa tải browser bundle.
                executable_candidates = [
                    shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome"),
                    shutil.which("msedge"), shutil.which("microsoft-edge"),
                ]
                for executable in [x for x in executable_candidates if x]:
                    try:
                        context = pw.chromium.launch_persistent_context(
                            user_data_dir=str(profile_dir), executable_path=executable, headless=headless,
                            viewport={"width": 1500, "height": 900}, args=["--disable-blink-features=AutomationControlled"]
                        )
                        break
                    except Exception as exc:
                        launch_errors.append(f"{executable}: {exc}")
            if context is None:
                self._finish_fail("Không mở được Chrome/Edge/Chromium cho Auto Web. " + " | ".join(launch_errors[-3:]))
            page = context.pages[0] if context.pages else context.new_page()
            call_index = len(self._report.get("browser_collect_runs", [])) + 1
            self._report.setdefault("browser_collect_runs", []).append({"index": call_index, "urls": urls})
            network: list[dict] = []

            def on_response(resp):
                try:
                    req = resp.request
                    if req.resource_type not in {"xhr", "fetch"}:
                        return
                    ctype = str(resp.headers.get("content-type") or "")
                    if not re.search(r"json|text|graphql", ctype, re.I):
                        return
                    body = resp.text()
                    if len(body) < 120:
                        return
                    sample = body[:50000]
                    score = 0
                    if re.search(r"episode|chapter|novel|story|content|body|text", sample, re.I): score += 20
                    if re.search(r"[ぁ-んァ-ン一-龯]{40,}", sample): score += 20
                    if len(sample) > 2000: score += 5
                    # Never persist live response bodies or token-bearing query strings by default.
                    if os.environ.get("TRUYEN_TOOL_CAPTURE_NETWORK", "") == "1":
                        purl = urlparse(resp.url)
                        redacted_url = purl._replace(query="[REDACTED]" if purl.query else "", fragment="").geturl()
                        network.append({"score": score, "status": resp.status, "method": req.method,
                                        "url": redacted_url, "content_type": ctype,
                                        "sample_length": len(body), "sample": "[REDACTED - body not persisted]"})
                except Exception:
                    pass

            page.on("response", on_response)
            htmls = []
            for i, url in enumerate(urls):
                self._check_stop()
                if progress: progress(f"AUTO WEB: Chrome đang mở trang kiểm tra {i+1}/{len(urls)}...")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1800)
                scroll_stats = self._auto_scroll_page(page, progress=progress, label=f"{i+1}/{len(urls)}")
                self._report.setdefault("browser_scroll", []).append({"url": url, **scroll_stats})
                self._write_report()
                body_text = ""
                try: body_text = page.locator("body").inner_text(timeout=3000)
                except Exception: pass
                if re.search(r"captcha|verify you are human|reCAPTCHA|hCaptcha|Turnstile", body_text, re.I):
                    try: page.screenshot(path=str(folder / f"browser_{call_index}_{i+1}_blocked.png"), full_page=True)
                    except Exception: pass
                    self._finish_fail("BLOCKED_BY_ANTI_BOT: Trang trình duyệt đang yêu cầu CAPTCHA/xác minh người dùng.")
                html = page.content()
                htmls.append(html)
                self._save_html(folder, f"browser_{call_index}_{i+1}.html", html)
                try: page.screenshot(path=str(folder / f"browser_{call_index}_{i+1}.png"), full_page=True)
                except Exception: pass
            try: page.remove_listener("response", on_response)
            except Exception: pass
            network.sort(key=lambda x: -int(x.get("score", 0)))
            (folder / "network_candidates.json").write_text(json.dumps(network[:80], ensure_ascii=False, indent=2), encoding="utf-8")
            return htmls, network
        finally:
            try:
                if context: context.close()
            except Exception: pass
            try: pw.stop()
            except Exception: pass

    def analyze(self, url: str, progress: Callable[[str], None] | None = None) -> AutoNovelInfo:
        raw = (url or "").strip()
        if not raw:
            raise ValueError("URL đang trống.")
        if not re.match(r"^https?://", raw, re.I):
            raw = "https://" + raw
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL website không hợp lệ.")
        folder = self._begin_report(raw)
        domain = parsed.netloc.lower().split(":")[0]
        existing_profile = self.load_profile(domain)
        if existing_profile:
            self._stage("Site Profile cũ", "INFO", f"Tìm thấy profile confidence={existing_profile.confidence:.2f}; sẽ kiểm tra lại trước khi dùng.")
            if progress: progress(f"AUTO WEB: tìm thấy Site Profile cũ của {domain}; đang kiểm tra lại.")
        if progress: progress(f"AUTO WEB: bắt đầu phân tích {domain}")

        # 1) Static HTML. Nếu public page chặn requests bằng 403 nhưng vẫn mở được
        # trong Chrome/Edge bình thường, chuyển sang Browser fallback thay vì FAIL ngay.
        # 401/429 vẫn dừng an toàn: không vượt đăng nhập và không né rate-limit.
        browser_required = False
        network_candidates: list[dict] = []
        try:
            html, final_url, status = self._http_get(raw)
            self._save_html(folder, "01_root_http.html", html)
            self._stage("HTTP/HTML", "PASS", f"HTTP {status}; {len(html):,} ký tự HTML", final_url=final_url)
        except AutoAnalysisError as exc:
            msg = str(exc)
            if "HTTP 403" in msg:
                self._stage("HTTP/HTML", "WARN", "HTTP 403 với requests; thử Chrome/Edge công khai, không vượt CAPTCHA/đăng nhập.")
                if progress:
                    progress("AUTO WEB: requests bị HTTP 403; chuyển sang Chrome/Edge để đọc trang công khai...")
                try:
                    browser_htmls, network_candidates = self._browser_collect([raw], folder, progress)
                    html = browser_htmls[0]
                    final_url = raw
                    status = 200
                    browser_required = True
                    self._stage("Browser fallback", "PASS", f"Chrome/Edge render được trang; {len(html):,} ký tự HTML")
                except AutoAnalysisError:
                    raise
                except Exception as browser_exc:
                    self._stage("Browser fallback", "FAIL", str(browser_exc))
                    self._finish_fail(f"HTTP 403 và Browser fallback thất bại: {browser_exc}")
            else:
                self._stage("HTTP/HTML", "FAIL", msg)
                self._finish_fail(msg.split("\n\nBáo cáo:", 1)[0])

        soup = BeautifulSoup(html, "html.parser")
        title, author, title_sel, author_sel = _find_title_author(soup, domain)
        jt, ja = _jsonld_metadata(soup)
        title = jt or title
        author = ja or author
        self._stage("Metadata", "PASS" if title else "WARN", f"title={title!r}; author={author or '(không thấy)'}")

        links, group_summaries = _discover_links(html, final_url)
        self._report["link_groups"] = group_summaries[:40]

        if not links:
            self._stage("Mục lục tĩnh", "FAIL", "Không tìm được cụm link episode đủ tin cậy; chuyển sang Chrome/Playwright.")
            browser_htmls, network_candidates = self._browser_collect([final_url], folder, progress)
            browser_required = True
            html = browser_htmls[0]
            soup = BeautifulSoup(html, "html.parser")
            title2, author2, title_sel2, author_sel2 = _find_title_author(soup, title)
            title, author = title2 or title, author2 or author
            title_sel, author_sel = title_sel2 or title_sel, author_sel2 or author_sel
            links, group_summaries = _discover_links(html, final_url)
            self._report["link_groups_browser"] = group_summaries[:40]
            if not links:
                self._stage("Mục lục trình duyệt", "FAIL", f"Không tìm được link episode. Network candidates: {len(network_candidates)}")
                if network_candidates:
                    self._finish_fail("DYNAMIC_APP_API_NOT_AUTO_RESOLVED: Có XHR/fetch ứng viên nhưng chưa đủ tin cậy để tự tạo endpoint tải truyện. Xem network_candidates.json.")
                self._finish_fail("Không tìm được danh sách episode trong HTML hoặc DOM sau render.")
            self._stage("Mục lục trình duyệt", "PASS", f"Tìm được {len(links)} link episode")
        else:
            self._stage("Mục lục tĩnh", "PASS", f"Tìm được {len(links)} link episode")

        # Follow explicit TOC pagination until the real end. Never silently PASS a partial TOC.
        if links:
            seen_pages = {_clean_url(final_url)}
            page_url = final_url
            page_html = html
            all_links = {x.url: x for x in links}
            preferred = links[0].group_key
            page_count = 1
            empty_match_pages = 0
            while True:
                nxt = _next_page_url(page_html, page_url)
                if not nxt:
                    break
                if nxt in seen_pages:
                    self._report["toc_pagination"] = {"pages": page_count, "complete": False, "reason": "cycle"}
                    self._finish_fail("TOC_PAGINATION_CYCLE: Mục lục lặp liên kết trang kế tiếp, không thể xác minh điểm cuối.")
                if not _same_host(nxt, final_url):
                    self._report["toc_pagination"] = {"pages": page_count, "complete": False, "reason": "different_host"}
                    self._finish_fail("TOC_PAGINATION_DIFFERENT_HOST: Link sang trang mục lục khác tên miền, yêu cầu kiểm tra thủ công.")
                if page_count >= MAX_TOC_PAGES:
                    self._report["toc_pagination"] = {"pages": page_count, "limit": MAX_TOC_PAGES, "next": nxt, "complete": False}
                    self._finish_fail(
                        f"TOC_PAGINATION_LIMIT_REACHED: Mục lục vẫn còn trang kế tiếp sau {MAX_TOC_PAGES} trang. "
                        "Tool dừng để không báo PASS khi mới lấy một phần truyện."
                    )
                seen_pages.add(nxt)
                try:
                    if browser_required:
                        page_html = self._browser_collect([nxt], folder, progress)[0][0]
                        page_url = nxt
                    else:
                        page_html, page_url, _ = self._http_get(nxt)
                except Exception as exc:
                    self._report["toc_pagination"] = {"pages": page_count, "failed_url": nxt, "complete": False}
                    self._finish_fail(f"TOC_PAGINATION_INCOMPLETE: Không đọc được trang mục lục kế tiếp {nxt}: {exc}")
                page_count += 1
                page_links, _ = _discover_links(page_html, page_url)
                matched = 0
                for x in page_links:
                    if x.group_key == preferred:
                        if x.url not in all_links:
                            all_links[x.url] = x
                        matched += 1
                if matched == 0:
                    empty_match_pages += 1
                else:
                    empty_match_pages = 0
                if empty_match_pages >= 1:
                    self._report["toc_pagination"] = {"pages": page_count, "complete": False, "reason": "empty_or_pattern_changed"}
                    self._finish_fail(
                        "TOC_PAGINATION_PATTERN_CHANGED: Trang mục lục kế tiếp không có link cùng mẫu episode. "
                        "Không báo PASS nếu trang cần tải không có dữ liệu hợp lệ."
                    )
            self._report["toc_pagination"] = {"pages": page_count, "complete": True, "episodes": len(all_links), "mode": "browser" if browser_required else "http", "scope": "explicit_next_links_only", "independently_verified_total": False}
            # dict giữ insertion order theo trang + DOM. Không sort theo số toàn cục.
            # Điều này giữ đúng site reset số tập theo chương hoặc chia ①②③.
            links = list(all_links.values())


        # Dedupe final links and cap absurd results.
        dedup: list[LinkCandidate] = []
        seen_urls: set[str] = set()
        for x in links:
            if x.url in seen_urls: continue
            seen_urls.add(x.url); dedup.append(x)
        links = dedup
        likeness_score, likeness_detail = _novel_likeness(links, group_summaries)
        self._report["novel_likeness"] = likeness_detail
        if likeness_score < NOVEL_LIKENESS_MIN:
            self._stage("Novel likeness gate", "FAIL", f"score={likeness_score:.2f} < {NOVEL_LIKENESS_MIN:.2f}")
            self._finish_fail(
                "LOW_NOVEL_LIKENESS: Cụm link trông giống blog/news/archive hơn mục lục truyện hoặc chưa đủ bằng chứng tuần tự. "
                "Tool không tự tải để tránh lấy nhầm nội dung."
            )
        self._stage("Novel likeness gate", "PASS", f"score={likeness_score:.2f}; links={len(links)}")
        if len(links) > 10000:
            self._finish_fail(f"LOW_CONFIDENCE_PROFILE: Phát hiện {len(links)} link giống episode, quá lớn để tự động chạy an toàn.")

        # 2) Test first/middle/last episode.
        sample_indices = sorted(set([0, len(links)//2, len(links)-1]))
        sample_links = [links[i] for i in sample_indices]
        sample_htmls: list[str] = []
        static_sample_ok = True
        for pos, link in enumerate(sample_links, 1):
            try:
                ep_html, _final, _status = self._http_get(link.url)
                sample_htmls.append(ep_html)
                self._save_html(folder, f"02_sample_{pos}_http.html", ep_html)
            except Exception as exc:
                static_sample_ok = False
                self._stage("Test episode HTTP", "WARN", f"{link.url}: {exc}")
                break

        selector = ""
        body_candidates: list[dict] = []
        # Nếu domain đã có Site Profile, thử selector cũ trên cả 3 episode trước.
        if existing_profile and existing_profile.body_selector and static_sample_ok and len(sample_htmls) == len(sample_links):
            old_tests = []
            for ep_html in sample_htmls:
                soup_old = BeautifulSoup(ep_html, "html.parser")
                try:
                    block_old = soup_old.select_one(existing_profile.body_selector)
                except Exception:
                    block_old = None
                old_score, old_body = _element_quality(block_old)
                old_tests.append(len(old_body) if old_score > -10**8 else 0)
            if old_tests and min(old_tests) >= MIN_BODY_CHARS:
                selector = existing_profile.body_selector
                self._stage("Dùng Site Profile cũ", "PASS", f"selector={selector}; test={old_tests}")
                if progress: progress(f"AUTO WEB: Site Profile cũ vẫn hợp lệ: {selector}")
            else:
                self._stage("Dùng Site Profile cũ", "WARN", f"Selector cũ không còn ổn định: {old_tests}; tự học lại.")
        if not selector and static_sample_ok and len(sample_htmls) == len(sample_links):
            selector, body_candidates = _discover_body_selector(sample_htmls)

        if not selector:
            self._stage("Tìm vùng nội dung tĩnh", "FAIL", "Không tìm được selector nội dung ổn định ở 3 episode; thử Chrome.")
            browser_required = True
            sample_htmls, more_network = self._browser_collect([x.url for x in sample_links], folder, progress)
            network_candidates.extend(more_network)
            selector, body_candidates = _discover_body_selector(sample_htmls)
        if not selector:
            self._report["body_candidates"] = body_candidates
            self._finish_fail("TEXT_NOT_RELIABLY_DETECTED: Không xác định được vùng nội dung truyện ổn định trên 3 episode mẫu.")

        self._report["body_candidates"] = body_candidates
        tests = []
        for link, ep_html in zip(sample_links, sample_htmls):
            s = BeautifulSoup(ep_html, "html.parser")
            try: el = s.select_one(selector)
            except Exception: el = None
            body = _extract_text(el)
            raw_body = _clean_text(el.get_text("\n", strip=True)) if el is not None else ""
            purity = len(body) / max(1, len(raw_body))
            tests.append({"url": link.url, "title": link.title, "chars": len(body), "purity": round(purity, 4), "pass": len(body) >= MIN_BODY_CHARS and purity >= 0.45})
        self._report["episode_tests"] = tests
        if not all(x["pass"] for x in tests):
            self._finish_fail("LOW_CONFIDENCE_PROFILE: Có episode mẫu quá ngắn hoặc vùng nội dung lẫn quá nhiều thành phần ngoài truyện.")
        self._stage("Test 3 episode", "PASS", "; ".join(f"{x['chars']} ký tự" for x in tests))

        confidence = 0.72
        if len(links) >= 5: confidence += 0.06
        if all(x.number is not None for x in links[:min(len(links), 20)]): confidence += 0.08
        if body_candidates and body_candidates[0].get("max_similarity", 1) < 0.9: confidence += 0.06
        confidence = min(0.95, confidence)
        profile = SiteProfile(
            profile_version=PROFILE_VERSION,
            domain=domain,
            browser_required=browser_required,
            body_selector=selector,
            title_selector=title_sel,
            author_selector=author_sel,
            episode_group_key=(links[0].group_key if links else ""),
            episode_count_tested=len(tests),
            confidence=round(confidence, 3),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            last_root_url=final_url,
        )
        profile_path = self.save_profile(profile)
        self._report["profile"] = asdict(profile)
        self._report["profile_path"] = str(profile_path)
        self._report["result"] = "PASS"
        self._report["completeness_warning"] = (
            "Đã quét các trang có liên kết Next rõ ràng; chưa thể xác minh rằng "
            "website không có các trang/tập khác bị ẩn bởi JavaScript hoặc virtual scrolling."
        )
        self._write_report()
        self._stage("Site Profile", "PASS", f"{profile_path}; confidence={profile.confidence:.2f}")

        episodes: list[AutoEpisode] = []
        for ordinal, link in enumerate(links, 1):
            episodes.append(AutoEpisode(index=ordinal, title=link.title or f"Episode {ordinal}", url=link.url, source_id=str(link.number or "")))
        chapter = AutoChapter(number=1, title="Tự động phát hiện", episodes=episodes)
        code_hash = hashlib.sha1(final_url.encode("utf-8", "ignore")).hexdigest()[:8].upper()
        ncode = f"AUTO_{re.sub(r'[^A-Za-z0-9]+','_',domain).strip('_').upper()}_{code_hash}"
        if progress:
            progress(f"AUTO WEB PASS: {len(episodes)} episode · body={selector} · confidence={profile.confidence:.2f}")
            progress(f"Site profile: {profile_path}")
            progress(f"Báo cáo: {self.last_report_path}")
        return AutoNovelInfo(
            ncode=ncode,
            title=title or domain,
            author=author,
            root_url=final_url,
            chapters=[chapter],
            site=f"Auto Web · {domain}",
            site_episode_count=0,  # Auto Web has no independently verified publisher total.
            profile_path=str(profile_path),
            analysis_report=self.last_report_path,
        )


class AutoWebClient:
    def __init__(self, delay: float = 1.5, stop_event=None, profile_path: str = ""):
        self.delay = delay
        self.stop_event = stop_event
        self.profile_path = profile_path
        self.analyzer = AutoWebAnalyzer(delay=delay, stop_event=stop_event)
        self.session = self.analyzer.session
        self.profile: SiteProfile | None = None
        self._pw = None
        self._context = None
        self._page = None
        if profile_path:
            try:
                raw = json.loads(Path(profile_path).read_text(encoding="utf-8"))
                if int(raw.get("profile_version") or 0) != PROFILE_VERSION:
                    raise ValueError("Site Profile cũ không tương thích")
                allowed = SiteProfile.__dataclass_fields__.keys()
                self.profile = SiteProfile(**{k: v for k, v in raw.items() if k in allowed})
            except Exception:
                self.profile = None

    def load_toc(self, url: str, progress=None) -> AutoNovelInfo:
        novel = self.analyzer.analyze(url, progress=progress)
        self.profile_path = novel.profile_path
        try:
            raw = json.loads(Path(self.profile_path).read_text(encoding="utf-8"))
            if int(raw.get("profile_version") or 0) != PROFILE_VERSION:
                raise ValueError("Site Profile cũ không tương thích")
            allowed = SiteProfile.__dataclass_fields__.keys()
            self.profile = SiteProfile(**{k: v for k, v in raw.items() if k in allowed})
        except Exception:
            pass
        return novel

    def _ensure_browser(self):
        if self._context is not None and self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise AutoAnalysisError("Trang này cần Playwright. Hãy chạy INSTALL_AND_RUN.bat.") from exc
        domain = self.profile.domain if self.profile else "generic"
        profile_dir = _browser_base_dir() / "browser_profiles" / re.sub(r"[^A-Za-z0-9._-]+", "_", domain)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        errors = []
        headless = os.environ.get("TRUYEN_TOOL_HEADLESS", "").strip() == "1"
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = dict(user_data_dir=str(profile_dir), headless=headless, viewport={"width": 1500, "height": 900}, args=["--disable-blink-features=AutomationControlled"])
                if channel: kwargs["channel"] = channel
                self._context = self._pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as exc:
                errors.append(str(exc))
        if self._context is None:
            for executable in [x for x in [shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome"), shutil.which("msedge"), shutil.which("microsoft-edge")] if x]:
                try:
                    self._context = self._pw.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir), executable_path=executable, headless=headless,
                        viewport={"width": 1500, "height": 900}, args=["--disable-blink-features=AutomationControlled"]
                    )
                    break
                except Exception as exc:
                    errors.append(str(exc))
        if self._context is None:
            raise AutoAnalysisError("Không mở được Chrome/Edge/Chromium: " + " | ".join(errors[-3:]))
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self):
        try:
            if self._context: self._context.close()
        except Exception: pass
        self._context = None; self._page = None
        try:
            if self._pw: self._pw.stop()
        except Exception: pass
        self._pw = None

    def _get_html(self, url: str) -> str:
        if self.stop_event is not None and self.stop_event.is_set():
            raise AutoStopRequested()
        if self.profile and self.profile.browser_required:
            self._ensure_browser()
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(1000)
            try:
                self.analyzer._auto_scroll_page(self._page, label="episode")
            except Exception:
                pass
            return self._page.content()
        html, _final, _status = self.analyzer._http_get(url)
        return html

    def fetch_episode(self, episode) -> dict:
        if self.profile is None:
            domain = urlparse(episode.url).netloc.lower().split(":")[0]
            self.profile = self.analyzer.load_profile(domain)
        if self.profile is None or not self.profile.body_selector:
            raise AutoAnalysisError("Không có Site Profile hợp lệ cho website này. Hãy PHÂN TÍCH lại URL tác phẩm.")
        html = self._get_html(episode.url)
        soup = BeautifulSoup(html, "html.parser")
        try:
            block = soup.select_one(self.profile.body_selector)
        except Exception as exc:
            raise AutoAnalysisError(f"Selector Site Profile không còn hợp lệ: {self.profile.body_selector}: {exc}") from exc
        body = _extract_text(block)
        if len(body) < MIN_BODY_CHARS:
            raise AutoAnalysisError(
                f"Nội dung episode {episode.index} quá ngắn ({len(body)} ký tự). Website có thể đã đổi cấu trúc. Hãy PHÂN TÍCH lại để tạo Site Profile mới."
            )
        return {
            "index": episode.index,
            "title": episode.title,
            "chapter_title": getattr(episode, "chapter_title", ""),
            "subchapter_title": getattr(episode, "subchapter_title", ""),
            "url": episode.url,
            "preface": "",
            "body": body,
            "afterword": "",
            "chars_no_whitespace": _count_chars(body),
            "chars_plain": len(body),
        }
