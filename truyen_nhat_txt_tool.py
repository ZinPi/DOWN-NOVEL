from __future__ import annotations

import csv
import hashlib
import tempfile
import datetime
import json
import os
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from auto_web import AutoAnalysisError, AutoStopRequested, AutoWebClient
from text_preserve import narrative_text


APP_NAME = "Truyện Nhật TXT Tool"
APP_VERSION = "0.9.4"
NAROU_HOSTS = {"ncode.syosetu.com", "novel18.syosetu.com"}
DEFAULT_DELAY = 2.2
TIMEOUT = 30
MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class Episode:
    index: int
    title: str
    url: str
    chapter_title: str = ""
    chapter_no: int = 0
    subchapter_title: str = ""
    source_id: str = ""


@dataclass
class Chapter:
    number: int
    title: str
    episodes: list[Episode] = field(default_factory=list)


@dataclass
class NovelInfo:
    ncode: str
    title: str
    author: str
    root_url: str
    chapters: list[Chapter]
    site: str = "Narou"
    site_total_chars: int = 0
    site_episode_count: int = 0

    @property
    def episode_count(self) -> int:
        return sum(len(ch.episodes) for ch in self.chapters)


class StopRequested(Exception):
    pass


class FetchError(RuntimeError):
    pass


def normalize_narou_url(url: str) -> tuple[str, str]:
    """Chuẩn hóa URL Narou thường hoặc Narou R18 thành URL gốc và N-code."""
    url = url.strip()
    if not url:
        raise ValueError("URL đang trống.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    if host not in NAROU_HOSTS:
        raise ValueError("URL Narou phải thuộc ncode.syosetu.com hoặc novel18.syosetu.com.")

    parts = [x for x in p.path.split("/") if x]
    if not parts or not re.fullmatch(r"n[0-9a-z]+", parts[0], re.I):
        raise ValueError("Không tìm thấy N-code hợp lệ trong URL.")

    ncode = parts[0].lower()
    return f"https://{host}/{ncode}/", ncode.upper()


def normalize_kakuyomu_url(url: str) -> tuple[str, str]:
    """Chuẩn hóa URL tác phẩm/episode Kakuyomu thành URL tác phẩm và Work ID."""
    url = url.strip()
    if not url:
        raise ValueError("URL đang trống.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    if host not in {"kakuyomu.jp", "www.kakuyomu.jp"}:
        raise ValueError("Không phải URL Kakuyomu hợp lệ.")

    m = re.search(r"/works/(\d+)", p.path)
    if not m:
        raise ValueError("Không tìm thấy Work ID trong URL Kakuyomu.")
    work_id = m.group(1)
    return f"https://kakuyomu.jp/works/{work_id}", work_id


def normalize_gamelive_url(url: str) -> tuple[str, str]:
    """Chuẩn hóa URL GameLive Studio story và lấy Story ID."""
    url = url.strip()
    if not url:
        raise ValueError("URL đang trống.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    if host not in {"gamelive.online", "www.gamelive.online"}:
        raise ValueError("Không phải URL GameLive hợp lệ.")

    m = re.search(r"/(?:[a-z]{2}/)?app/stories/([0-9a-fA-F]{16,64})", p.path)
    if not m:
        raise ValueError("Không tìm thấy Story ID trong URL GameLive.")
    story_id = m.group(1)
    lang = "vi"
    ml = re.match(r"/([a-z]{2})/app/stories/", p.path, re.I)
    if ml:
        lang = ml.group(1).lower()
    return f"https://gamelive.online/{lang}/app/stories/{story_id}", story_id


class GameLiveLoginRequired(FetchError):
    pass


def detect_site(url: str) -> str:
    """Nhận diện adapter có sẵn; domain khác chuyển sang Auto Web Analyzer."""
    raw = url.strip()
    if not raw:
        raise ValueError("URL đang trống.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    host = urlparse(raw).netloc.lower().split(":")[0]
    if not host:
        raise ValueError("URL website không hợp lệ.")
    if host == "ncode.syosetu.com":
        return "Narou"
    if host == "novel18.syosetu.com":
        return "Narou R18"
    if host in {"kakuyomu.jp", "www.kakuyomu.jp"}:
        return "Kakuyomu"
    if host in {"gamelive.online", "www.gamelive.online"}:
        return "GameLive"
    return "Auto Web"


def sanitize_filename(text: str, max_len: int = 100) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\\/:*?\"<>|]", "_", text)
    text = re.sub(r"[\x00-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "khong_ten"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")

    # Tên đặc biệt không dùng được trên Windows.
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", text.split(".", 1)[0]):
        text = "_" + text
    return text


def safe_windows_name(text: str, max_utf16_units: int = 64) -> str:
    """Keep any single filename component short enough for Windows incl. Japanese UTF-16 names."""
    if max_utf16_units < 4:
        raise ValueError("Giới hạn tên quá ngắn.")
    safe = sanitize_filename(text, max_len=240)
    while len(safe.encode("utf-16-le")) // 2 > max_utf16_units:
        safe = safe[:-1]
    return sanitize_filename(safe, max_len=240)


def make_run_directory(base: Path, run_id: str = "") -> Path:
    """Reserve a new output folder atomically; previous results never get overwritten."""
    base.mkdir(parents=True, exist_ok=True)
    run_id = safe_windows_name(run_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S"), 42)
    for suffix in range(0, 10000):
        name = f"RUN_{run_id}" + (f"_{suffix:03d}" if suffix else "")
        destination = base / name
        try:
            destination.mkdir(exist_ok=False)
            return destination
        except FileExistsError:
            continue
    raise OSError("Không thể tạo thư mục phiên tải mới, vui lòng chọn thư mục khác.")


def atomic_write_text(path: Path, data: str, encoding: str = "utf-8-sig") -> None:
    """Use a temporary sibling and rename only after the text is fully written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".__writing_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_episode_payload(data: dict, expected_url: str) -> tuple[bool, str]:
    if not isinstance(data, dict) or data.get("url") != expected_url:
        return False, "URL không trùng episode yêu cầu"
    body = data.get("body")
    if not isinstance(body, str) or len(body.strip()) < 10:
        return False, "Thân truyện trống hoặc ngắn bất thường (dưới 10 ký tự)"
    if "\x00" in body:
        return False, "Nội dung chứa byte NUL"
    return True, "OK"


def strip_ruby(soup_or_tag: Tag) -> None:
    """Bỏ furigana <rt>/<rp>, giữ chữ Kanji gốc."""
    for x in soup_or_tag.select("rt, rp"):
        x.decompose()


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+\n", "\n", text)
    text = re.sub(r"\n[ \t\u3000]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_block_text(block: Tag | None) -> str:
    if block is None:
        return ""

    # Clone để bỏ ruby mà không làm thay đổi BeautifulSoup gốc.
    clone = BeautifulSoup(str(block), "html.parser")
    root = clone.find()
    if root is None:
        return ""
    strip_ruby(root)

    return narrative_text(root)


def count_chars_no_whitespace(text: str) -> int:
    """Đếm ký tự phần văn bản sau khi loại toàn bộ whitespace."""
    return sum(1 for ch in text if not ch.isspace())


def count_chars_plain(text: str) -> int:
    """Đếm độ dài TXT sạch, có tính ký tự xuống dòng."""
    return len(text)


def parse_episode_spec(spec: str, max_episode: int) -> list[int]:
    """
    Đọc cách chọn episode/tập nhỏ kiểu: 1-32, 50, 1,3,5-7.
    Trả về danh sách số episode tăng dần, không trùng.
    """
    raw = unicodedata.normalize("NFKC", (spec or "").strip())
    raw = raw.replace("–", "-").replace("—", "-").replace("~", "-").replace("～", "-")
    raw = re.sub(r"\s+", "", raw)
    if not raw:
        raise ValueError("Ô chọn episode đang trống. Ví dụ: 1-32 hoặc 1,3,10-25")
    if max_episode < 1:
        raise ValueError("Truyện chưa có episode để chọn.")

    selected: set[int] = set()
    for token in raw.split(","):
        if not token:
            continue
        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise ValueError(f"Phạm vi episode không hợp lệ: {token}")
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                start, end = end, start
            if start < 1 or end > max_episode:
                raise ValueError(f"Episode {start}-{end} nằm ngoài phạm vi 1-{max_episode}.")
            selected.update(range(start, end + 1))
        else:
            if not token.isdigit():
                raise ValueError(f"Số episode không hợp lệ: {token}")
            number = int(token)
            if number < 1 or number > max_episode:
                raise ValueError(f"Episode {number} nằm ngoài phạm vi 1-{max_episode}.")
            selected.add(number)

    if not selected:
        raise ValueError("Không đọc được episode nào. Ví dụ: 1-32 hoặc 1,3,10-25")
    return sorted(selected)


class NarouClient:
    def __init__(self, delay: float = DEFAULT_DELAY, stop_event: threading.Event | None = None):
        self.delay = max(1.0, float(delay))
        self.stop_event = stop_event or threading.Event()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
                "Cache-Control": "no-cache",
            }
        )
        self._last_request = 0.0

    def _sleep_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_request
        target = self.delay + random.uniform(0.0, 0.35)
        remain = target - elapsed
        while remain > 0:
            if self.stop_event.is_set():
                raise StopRequested()
            step = min(0.2, remain)
            time.sleep(step)
            remain -= step

    def _prepare_session_for_url(self, url: str) -> None:
        """Chuẩn bị cookie cần thiết theo nguồn trước khi request."""
        host = urlparse(url).netloc.lower().split(":")[0]
        if host == "novel18.syosetu.com":
            # Narou R18 dùng cookie xác nhận tuổi. Đặt ở domain cha để redirect nội bộ
            # trong hệ thống syosetu vẫn nhận được trạng thái xác nhận.
            self.session.cookies.set("over18", "yes", domain=".syosetu.com", path="/")

    def get(self, url: str) -> str:
        self._prepare_session_for_url(url)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            if self.stop_event.is_set():
                raise StopRequested()

            self._sleep_if_needed()
            try:
                r = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
                self._last_request = time.monotonic()
            except requests.RequestException as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    time.sleep(attempt * 2)
                    continue
                raise FetchError(f"Lỗi mạng khi tải {url}: {e}") from e

            if r.status_code == 200:
                final = urlparse(r.url)
                if final.netloc.lower().split(":")[0] == "nl.syosetu.com" and "/redirect/ageauth/" in final.path:
                    raise FetchError(
                        "Narou R18 vẫn chuyển về trang xác nhận tuổi. "
                        "Hãy thử lại; nếu lỗi lặp lại, website có thể đã thay đổi cơ chế age-gate."
                    )
                # Narou/Syosetu hiện dùng UTF-8. Requests đôi khi suy đoán nhầm với trang Nhật ngắn.
                r.encoding = "utf-8"
                return r.text

            # Không cố vượt anti-bot / rate-limit / đăng nhập.
            if r.status_code in (401, 403, 429):
                raise FetchError(
                    f"Trang trả HTTP {r.status_code}. Tool đã dừng an toàn, không vượt cơ chế bảo vệ. "
                    "Hãy thử lại sau và kiểm tra trang bằng trình duyệt."
                )

            if 500 <= r.status_code < 600 and attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
                continue

            raise FetchError(f"Không tải được {url} (HTTP {r.status_code}).")

        raise FetchError(str(last_exc) if last_exc else "Không tải được trang.")

    def load_toc(self, url: str, progress: Callable[[str], None] | None = None) -> NovelInfo:
        root_url, ncode = normalize_narou_url(url)
        current_url = root_url
        seen_pages: set[str] = set()
        episodes: list[Episode] = []
        current_chapter = ""
        title = ""
        author = ""
        episode_index_seen: set[int] = set()

        while current_url and current_url not in seen_pages:
            seen_pages.add(current_url)
            if progress:
                progress(f"Đang đọc mục lục: {current_url}")

            html = self.get(current_url)
            soup = BeautifulSoup(html, "html.parser")

            if not title:
                title_el = soup.select_one("h1.p-novel__title, .p-novel__title")
                if title_el:
                    title = title_el.get_text(" ", strip=True)
                else:
                    title = soup.title.get_text(" ", strip=True) if soup.title else ncode

                author_el = soup.select_one(".p-novel__author")
                if author_el:
                    author = re.sub(r"^作者[:：]?\s*", "", author_el.get_text(" ", strip=True))

            eplist = soup.select_one(".p-eplist")
            if eplist is None:
                # Truyện ngắn hoặc URL trực tiếp có本文.
                body = soup.select_one("div.js-novel-text.p-novel__text, div.p-novel__body")
                if body is not None:
                    ep_title_el = soup.select_one(".p-novel__subtitle-episode, h1.p-novel__title")
                    ep_title = ep_title_el.get_text(" ", strip=True) if ep_title_el else title
                    episodes.append(Episode(index=1, title=ep_title, url=root_url, chapter_title=""))
                    break
                raise FetchError("Không tìm thấy mục lục hoặc本文. Có thể cấu trúc trang đã thay đổi.")

            # Duyệt theo đúng thứ tự DOM để giữ ranh giới chương lớn.
            for node in eplist.find_all("div", recursive=True):
                classes = set(node.get("class", []))

                if "p-eplist__chapter-title" in classes:
                    current_chapter = node.get_text(" ", strip=True)
                    continue

                if "p-eplist__sublist" not in classes:
                    continue

                a = node.select_one("a.p-eplist__subtitle")
                if not a or not a.get("href"):
                    continue

                href = urljoin(root_url, a["href"])
                subtitle = a.get_text(" ", strip=True)
                m = re.search(r"/(\d+)/?$", urlparse(href).path)
                idx = int(m.group(1)) if m else len(episodes) + 1

                if idx in episode_index_seen:
                    continue
                episode_index_seen.add(idx)

                episodes.append(
                    Episode(
                        index=idx,
                        title=subtitle or f"Episode {idx}",
                        url=href,
                        chapter_title=current_chapter,
                    )
                )

            next_el = soup.select_one("a.c-pager__item--next")
            next_url = urljoin(root_url, next_el.get("href")) if next_el and next_el.get("href") else ""
            if next_url in seen_pages:
                next_url = ""
            current_url = next_url

        if not episodes:
            raise FetchError("Không tìm thấy episode nào trong mục lục.")

        episodes.sort(key=lambda e: e.index)

        # Giữ ranh giới chương theo thứ tự liên tục. Hai chương khác nhau nhưng trùng tên
        # không bị gộp nhầm chỉ vì có cùng tiêu đề.
        chapters: list[Chapter] = []
        current_key: str | None = None
        current_chapter_obj: Chapter | None = None
        for ep in episodes:
            key = ep.chapter_title.strip() or "Mở đầu / không có tên chương"
            if current_chapter_obj is None or key != current_key:
                current_chapter_obj = Chapter(number=len(chapters) + 1, title=key)
                chapters.append(current_chapter_obj)
                current_key = key
            ep.chapter_no = current_chapter_obj.number
            current_chapter_obj.episodes.append(ep)

        site_name = "Narou R18" if urlparse(root_url).netloc.lower() == "novel18.syosetu.com" else "Narou"
        return NovelInfo(
            ncode=ncode,
            title=title or ncode,
            author=author,
            root_url=root_url,
            chapters=chapters,
            site=site_name,
        )

    def fetch_episode(self, episode: Episode) -> dict:
        """Tải một episode và trích xuất cả preface/body/afterword để cache được tái sử dụng."""
        html = self.get(episode.url)
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one(
            ".p-novel__subtitle-episode, h1.p-novel__title.p-novel__title--rensai, h1.p-novel__title"
        )
        title = title_el.get_text(" ", strip=True) if title_el else episode.title

        chapter_el = soup.select_one(".p-novel__subtitle-chapter")
        chapter_title = chapter_el.get_text(" ", strip=True) if chapter_el else episode.chapter_title

        pre = soup.select_one("div.js-novel-text.p-novel__text.p-novel__text--preface")
        preface = extract_block_text(pre)

        body = soup.select_one(
            "div.js-novel-text.p-novel__text:not(.p-novel__text--preface):not(.p-novel__text--afterword)"
        )
        if body is None:
            candidates = soup.select("div.p-novel__text")
            candidates = [
                x
                for x in candidates
                if "p-novel__text--preface" not in x.get("class", [])
                and "p-novel__text--afterword" not in x.get("class", [])
            ]
            body = candidates[0] if candidates else soup.select_one("div.p-novel__body")

        body_text = extract_block_text(body)
        if not body_text:
            raise FetchError(f"Không tìm thấy本文 ở episode {episode.index}: {episode.url}")

        aft = soup.select_one("div.js-novel-text.p-novel__text.p-novel__text--afterword")
        afterword = extract_block_text(aft)

        return {
            "index": episode.index,
            "title": title,
            "chapter_title": chapter_title,
            "url": episode.url,
            "preface": preface,
            "body": body_text,
            "afterword": afterword,
            "chars_no_whitespace": count_chars_no_whitespace(body_text),
            "chars_plain": count_chars_plain(body_text),
        }



class KakuyomuClient(NarouClient):
    """Client cho Kakuyomu. Dùng cùng cơ chế tải tuần tự/an toàn với Narou."""

    @staticmethod
    def _resolve(state: dict, obj):
        if isinstance(obj, dict) and "__ref" in obj:
            return state.get(obj["__ref"], {})
        return obj if isinstance(obj, dict) else {}

    def load_toc(self, url: str, progress: Callable[[str], None] | None = None) -> NovelInfo:
        root_url, work_id = normalize_kakuyomu_url(url)
        if progress:
            progress(f"Đang đọc mục lục Kakuyomu: {root_url}")

        html = self.get(root_url)
        soup = BeautifulSoup(html, "html.parser")
        episodes: list[Episode] = []
        title = ""
        author = ""
        site_total_chars = 0
        site_episode_count = 0

        # Cách chính: Kakuyomu nhúng mục lục trong __NEXT_DATA__ / __APOLLO_STATE__.
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script:
            try:
                payload = json.loads(next_script.get_text())
                page_props = payload.get("props", {}).get("pageProps", {})
                state = page_props.get("__APOLLO_STATE__", {})
                work = state.get(f"Work:{work_id}", {})
                if not work:
                    for value in state.values():
                        if isinstance(value, dict) and value.get("__typename") == "Work" and str(value.get("id", "")) == work_id:
                            work = value
                            break

                title = str(work.get("title") or "").strip()
                try:
                    site_total_chars = int(work.get("totalCharacterCount") or 0)
                except (TypeError, ValueError):
                    site_total_chars = 0
                try:
                    site_episode_count = int(work.get("publicEpisodeCount") or 0)
                except (TypeError, ValueError):
                    site_episode_count = 0
                author_obj = self._resolve(state, work.get("author"))
                author = str(
                    work.get("alternateAuthorName")
                    or author_obj.get("activityName")
                    or author_obj.get("name")
                    or author_obj.get("screenName")
                    or ""
                ).strip()

                toc_refs = work.get("tableOfContentsV2") or work.get("tableOfContents") or []
                current_chapter = ""
                current_subchapter = ""
                ordinal = 0

                for toc_ref in toc_refs:
                    toc_item = self._resolve(state, toc_ref)
                    chapter_obj = self._resolve(state, toc_item.get("chapter"))
                    if chapter_obj:
                        level = int(chapter_obj.get("level") or 1)
                        chapter_name = str(chapter_obj.get("title") or "").strip()
                        if level <= 1:
                            current_chapter = chapter_name
                            current_subchapter = ""
                        else:
                            current_subchapter = chapter_name

                    union_refs = toc_item.get("episodeUnions") or []
                    for ep_ref in union_refs:
                        ep_obj = self._resolve(state, ep_ref)
                        if not ep_obj or ep_obj.get("__typename") not in {None, "Episode"}:
                            continue
                        ep_id = str(ep_obj.get("id") or "").strip()
                        ep_title = str(ep_obj.get("title") or "").strip()
                        if not ep_id:
                            continue
                        ordinal += 1
                        episodes.append(
                            Episode(
                                index=ordinal,
                                title=ep_title or f"Episode {ordinal}",
                                url=f"https://kakuyomu.jp/works/{work_id}/episodes/{ep_id}",
                                chapter_title=current_chapter,
                                subchapter_title=current_subchapter,
                                source_id=ep_id,
                            )
                        )
            except Exception as e:
                if progress:
                    progress(f"Không đọc được JSON Kakuyomu, chuyển sang parser HTML dự phòng: {e}")

        # Fallback: đọc trực tiếp link episode trong HTML, vẫn giữ thứ tự trang.
        if not episodes:
            if not title:
                h = soup.select_one("h1, #workTitle")
                title = h.get_text(" ", strip=True) if h else f"Kakuyomu_{work_id}"
            seen: set[str] = set()
            ordinal = 0
            current_chapter = ""
            for node in soup.find_all(["h2", "h3", "a"]):
                if node.name in {"h2", "h3"}:
                    text = node.get_text(" ", strip=True)
                    if text and ("章" in text or "部" in text):
                        current_chapter = text
                    continue
                href = node.get("href") or ""
                m = re.search(rf"/works/{re.escape(work_id)}/episodes/(\d+)", href)
                if not m:
                    continue
                ep_id = m.group(1)
                if ep_id in seen:
                    continue
                seen.add(ep_id)
                ordinal += 1
                episodes.append(
                    Episode(
                        index=ordinal,
                        title=node.get_text(" ", strip=True) or f"Episode {ordinal}",
                        url=f"https://kakuyomu.jp/works/{work_id}/episodes/{ep_id}",
                        chapter_title=current_chapter,
                        source_id=ep_id,
                    )
                )

        if not episodes:
            raise FetchError("Không tìm thấy episode Kakuyomu. Có thể cấu trúc trang đã thay đổi hoặc tác phẩm không công khai.")

        chapters: list[Chapter] = []
        current_key: tuple[str, str] | None = None
        current_chapter_obj: Chapter | None = None
        for ep in episodes:
            main_title = ep.chapter_title.strip() or "Mở đầu / không có tên chương"
            # Chương cấp 2 không tách thành file riêng: giữ trong cùng chương cấp 1.
            key = (main_title, "main")
            if current_key != key or current_chapter_obj is None:
                current_chapter_obj = Chapter(number=len(chapters) + 1, title=main_title)
                chapters.append(current_chapter_obj)
                current_key = key
            ep.chapter_no = current_chapter_obj.number
            current_chapter_obj.episodes.append(ep)

        return NovelInfo(
            ncode=f"KAKUYOMU_{work_id}",
            title=title or f"Kakuyomu_{work_id}",
            author=author,
            root_url=root_url,
            chapters=chapters,
            site="Kakuyomu",
            site_total_chars=site_total_chars,
            site_episode_count=site_episode_count,
        )

    def fetch_episode(self, episode: Episode) -> dict:
        html = self.get(episode.url)
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one("h1, h2")
        title = episode.title
        if title_el:
            candidate = title_el.get_text(" ", strip=True)
            if candidate and candidate != title:
                # Trên Kakuyomu h1 đôi khi là tên tác phẩm, nên chỉ lấy khi gần với tiêu đề episode.
                if episode.title in candidate or candidate in episode.title:
                    title = candidate

        body = soup.select_one("div.widget-episodeBody.js-episode-body, .widget-episodeBody, .js-episode-body")
        body_text = extract_block_text(body)
        if not body_text:
            raise FetchError(f"Không tìm thấy本文 Kakuyomu ở episode {episode.index}: {episode.url}")

        return {
            "index": episode.index,
            "title": title,
            "chapter_title": episode.chapter_title,
            "subchapter_title": episode.subchapter_title,
            "url": episode.url,
            "preface": "",
            "body": body_text,
            "afterword": "",
            "chars_no_whitespace": count_chars_no_whitespace(body_text),
            "chars_plain": count_chars_plain(body_text),
        }


class GameLiveClient:
    """GameLive Studio client qua Chrome profile thật.

    GameLive yêu cầu đăng nhập và nội dung được render bằng ứng dụng web, nên không dùng
    requests để vượt phiên đăng nhập. Tool mở Chrome bằng Playwright với một profile riêng
    nằm trong LOCALAPPDATA. Người dùng tự đăng nhập trên trang GameLive; cookie/session được
    Chrome giữ lại cho lần chạy sau.
    """

    EPISODE_SCAN_JS = r"""
() => {
  const W = window.innerWidth || 1, H = window.innerHeight || 1;
  const nodes = [...document.querySelectorAll('a,button,[role="button"],div')];
  const by = new Map();
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 80 || r.height < 20 || r.bottom < 0 || r.top > H) continue;
    if (r.left < W * 0.56) continue;
    const text = String(el.innerText || el.textContent || '').replace(/\r/g,'').trim();
    if (!text || text.length > 2400) continue;
    const m = text.match(/^Tập\s*(\d+)\s*(?:[·:\-]|\s)/i);
    if (!m) continue;
    const n = Number(m[1]);
    if (!Number.isFinite(n) || n < 1) continue;
    const first = text.split(/\n+/).map(x=>x.trim()).filter(Boolean)[0] || `Tập ${n}`;
    const clickable = el.closest('button,a,[role="button"]') || el;
    const cr = clickable.getBoundingClientRect();
    const style = getComputedStyle(clickable);
    const score = text.length + (style.cursor === 'pointer' ? -150 : 0) + (cr.width * cr.height / 2500);
    const old = by.get(n);
    if (!old || score < old.score) by.set(n, {index:n,title:first,text,score});
  }
  return [...by.values()].sort((a,b)=>a.index-b.index).map(({score,...x})=>x);
}
"""

    CLICK_EPISODE_JS = r"""
(index) => {
  const W = window.innerWidth || 1, H = window.innerHeight || 1;
  const nodes = [...document.querySelectorAll('a,button,[role="button"],div')];
  let best = null;
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 80 || r.height < 20 || r.bottom < 0 || r.top > H || r.left < W * 0.56) continue;
    const text = String(el.innerText || el.textContent || '').trim();
    const m = text.match(/^Tập\s*(\d+)\s*(?:[·:\-]|\s)/i);
    if (!m || Number(m[1]) !== Number(index)) continue;
    const clickable = el.closest('button,a,[role="button"]') || el;
    const cr = clickable.getBoundingClientRect();
    const style = getComputedStyle(clickable);
    const score = text.length + (style.cursor === 'pointer' ? -150 : 0) + (cr.width * cr.height / 2500);
    if (!best || score < best.score) best = {el:clickable, score};
  }
  if (!best) return false;
  best.el.scrollIntoView({block:'center', inline:'nearest'});
  best.el.click();
  return true;
}
"""

    SCROLL_EPISODE_PANEL_JS = r"""
(position) => {
  const W=window.innerWidth||1,H=window.innerHeight||1;
  const els=[...document.querySelectorAll('*')];
  let best=null;
  for(const el of els){
    const r=el.getBoundingClientRect();
    if(!r || r.width<180 || r.height<180 || r.left<W*0.55) continue;
    if(el.scrollHeight <= el.clientHeight + 80) continue;
    const st=getComputedStyle(el);
    if(!/(auto|scroll)/i.test(st.overflowY||'')) continue;
    const txt=String(el.innerText||'');
    const hits=(txt.match(/Tập\s*\d+/gi)||[]).length;
    if(hits<1) continue;
    const score=hits*1000 + Math.min(el.scrollHeight,10000) - r.width;
    if(!best || score>best.score) best={el,score};
  }
  if(!best) return {ok:false,scrollTop:0,max:0,clientHeight:0};
  const el=best.el, max=Math.max(0,el.scrollHeight-el.clientHeight);
  if(position==='top') el.scrollTop=0;
  else if(position==='bottom') el.scrollTop=max;
  else if(typeof position==='number') el.scrollTop=Math.max(0,Math.min(max,position));
  el.dispatchEvent(new Event('scroll',{bubbles:true}));
  return {ok:true,scrollTop:el.scrollTop,max,clientHeight:el.clientHeight};
}
"""

    MAIN_SIGNATURE_JS = r"""
() => {
  const W=window.innerWidth||1,H=window.innerHeight||1;
  const els=[...document.querySelectorAll('main,article,[role="main"],section,div')];
  let best='';
  for(const el of els){
    const r=el.getBoundingClientRect();
    if(!r || r.width<420 || r.height<100 || r.right<W*0.25 || r.left>W*0.78) continue;
    const t=String(el.innerText||'').trim();
    if(t.length>best.length && t.length<250000) best=t;
  }
  return best.slice(0,900)+'|'+best.slice(-900);
}
"""

    EXTRACT_MAIN_TEXT_JS = r"""
() => {
  const W=window.innerWidth||1,H=window.innerHeight||1;
  const selectors=['[data-message-author-role="assistant"]','article','[class*="prose"]','[class*="markdown"]','[class*="message"]','main','[role="main"]','section','div'];
  const seen=new Set(), candidates=[];
  for(const sel of selectors){
    for(const el of document.querySelectorAll(sel)){
      if(seen.has(el)) continue; seen.add(el);
      const r=el.getBoundingClientRect();
      if(!r || r.width<360 || r.height<80 || r.right<W*0.20 || r.left>W*0.80) continue;
      if(r.left < W*0.10 && r.width > W*0.82) continue;
      let t=String(el.innerText||el.textContent||'').replace(/\r/g,'').trim();
      if(t.length<180 || t.length>220000) continue;
      if((t.match(/Xóa tập\s*\d+/gi)||[]).length>=2) continue;
      if((t.match(/Tập\s*\d+\s*[·:]/gi)||[]).length>=4) continue;
      if(/ĐIỀU HƯỚNG[\s\S]*Thanh toán[\s\S]*Bạn bè/i.test(t)) continue;
      if(/TÀI LIỆU[\s\S]*Bible[\s\S]*Xóa tập/i.test(t)) continue;
      const depth=(()=>{let d=0,n=el;while(n&&n.parentElement){d++;n=n.parentElement;}return d;})();
      const area=Math.max(1,r.width*r.height);
      const controls=(t.match(/Đề xuất bước tiếp theo|Hỏi, hoặc yêu cầu tập tiếp theo|Chốt hạ hoàn thành|Viết tiếp Season/gi)||[]).length;
      const score=t.length - controls*800 + depth*12 - area/20000;
      candidates.push({t,score,len:t.length,area,depth});
    }
  }
  candidates.sort((a,b)=>b.score-a.score);
  return candidates.length ? candidates[0].t : '';
}
"""

    def __init__(self, delay: float = DEFAULT_DELAY, stop_event: threading.Event | None = None):
        self.delay = max(0.8, float(delay))
        self.stop_event = stop_event or threading.Event()
        self._pw = None
        self.context = None
        self.page = None
        base = os.environ.get("LOCALAPPDATA")
        if base:
            self.profile_dir = Path(base) / "Truyen_Nhat_TXT_Tool" / "GameLive_Profile"
        else:
            self.profile_dir = Path.home() / ".truyen_nhat_txt_tool" / "GameLive_Profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise StopRequested()

    def _start(self, url: str | None = None):
        if self.context is not None and self.page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise FetchError(
                "GameLive cần thư viện Playwright. Hãy chạy INSTALL_AND_RUN.bat của bản mới để cài tự động."
            ) from e

        self._pw = sync_playwright().start()
        launch_errors = []
        context = None
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = dict(
                    user_data_dir=str(self.profile_dir),
                    headless=False,
                    viewport={"width": 1500, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                if channel:
                    kwargs["channel"] = channel
                context = self._pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as e:
                launch_errors.append(f"{channel or 'chromium'}: {e}")
        if context is None:
            self._pw.stop()
            self._pw = None
            raise FetchError(
                "Không mở được Chrome cho GameLive. Máy cần Chrome/Edge; nếu vẫn lỗi hãy chạy "
                "'python -m playwright install chromium'. Chi tiết: " + " | ".join(launch_errors[-2:])
            )
        self.context = context
        pages = context.pages
        self.page = pages[0] if pages else context.new_page()
        if url:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(2200)

    def close(self):
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def _scan_episodes(self) -> list[dict]:
        self._check_stop()
        if not self.page:
            return []
        try:
            raw = self.page.evaluate(self.EPISODE_SCAN_JS) or []
        except Exception:
            raw = []
        out = []
        seen = set()
        for row in raw:
            try:
                idx = int(row.get("index"))
            except Exception:
                continue
            if idx in seen:
                continue
            seen.add(idx)
            title = clean_text(str(row.get("title") or f"Tập {idx}"))
            out.append({"index": idx, "title": title})
        out.sort(key=lambda x: x["index"])
        return out

    def _collect_all_episodes(self) -> list[dict]:
        """Quét toàn bộ cột TÀI LIỆU, kể cả danh sách dài có scrollbar/virtualization."""
        if not self.page:
            return []
        found: dict[int, dict] = {}
        try:
            state = self.page.evaluate(self.SCROLL_EPISODE_PANEL_JS, "top")
        except Exception:
            state = {"ok": False}
        loops = 0
        last_top = -1
        while loops < 120:
            self._check_stop()
            loops += 1
            self.page.wait_for_timeout(180)
            for row in self._scan_episodes():
                found[int(row["index"])] = row
            if not state or not state.get("ok"):
                break
            top = int(state.get("scrollTop") or 0)
            max_top = int(state.get("max") or 0)
            client_h = int(state.get("clientHeight") or 500)
            if top >= max_top - 3:
                break
            next_top = min(max_top, top + max(220, int(client_h * 0.72)))
            if next_top == top or top == last_top:
                break
            last_top = top
            state = self.page.evaluate(self.SCROLL_EPISODE_PANEL_JS, next_top)
        try:
            self.page.evaluate(self.SCROLL_EPISODE_PANEL_JS, "top")
            self.page.wait_for_timeout(180)
        except Exception:
            pass
        return [found[k] for k in sorted(found)]

    def _login_required(self) -> bool:
        if not self.page:
            return True
        try:
            body = self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return True
        if re.search(r"Đăng nhập để viết bộ truyện|Đăng nhập\s*$", body, re.I | re.M):
            return not bool(self._scan_episodes())
        return False

    def open_login(self, url: str, progress: Callable[[str], None] | None = None, timeout_sec: int = 600) -> bool:
        root_url, _ = normalize_gamelive_url(url)
        try:
            self._start(root_url)
            if progress:
                progress("Chrome GameLive đã mở. Hãy đăng nhập trực tiếp trong cửa sổ Chrome này.")
                progress("Tool sẽ tự nhận phiên đăng nhập khi danh sách Tập xuất hiện; không cần nhập mật khẩu vào tool.")
            end = time.monotonic() + timeout_sec
            while time.monotonic() < end:
                self._check_stop()
                if self.page is None or self.page.is_closed():
                    raise FetchError("Cửa sổ GameLive đã bị đóng trước khi xác nhận đăng nhập.")
                eps = self._scan_episodes()
                if eps:
                    if progress:
                        progress(f"Đã nhận phiên đăng nhập GameLive: đọc được {len(eps)} tập. Đang lưu profile...")
                    self.page.wait_for_timeout(1200)
                    return True
                self.page.wait_for_timeout(1000)
            raise FetchError("Quá 10 phút chưa nhận được danh sách Tập sau đăng nhập GameLive.")
        finally:
            self.close()

    def _story_title(self, story_id: str) -> str:
        if not self.page:
            return f"GameLive_{story_id}"
        try:
            data = self.page.evaluate("""() => {
              const meta=document.querySelector('meta[property="og:title"]')?.content||'';
              const heads=[...document.querySelectorAll('h1,h2')].map(x=>(x.innerText||'').trim()).filter(Boolean);
              return {meta,heads,title:document.title||''};
            }""")
            candidates = [data.get("meta", ""), *data.get("heads", []), data.get("title", "")]
            for c in candidates:
                c = clean_text(str(c))
                if not c:
                    continue
                if re.fullmatch(r"Game\s*Live|Studio tiểu thuyết", c, re.I):
                    continue
                return c[:180]
        except Exception:
            pass
        return f"GameLive_{story_id}"

    def load_toc(self, url: str, progress: Callable[[str], None] | None = None) -> NovelInfo:
        root_url, story_id = normalize_gamelive_url(url)
        try:
            if progress:
                progress(f"Đang mở GameLive bằng profile đăng nhập: {root_url}")
            self._start(root_url)
            self._check_stop()
            eps = self._collect_all_episodes()
            if not eps and self._login_required():
                raise GameLiveLoginRequired(
                    "GameLive chưa có phiên đăng nhập. Bấm 'MỞ / ĐĂNG NHẬP GAMELIVE', đăng nhập trong Chrome rồi thử PHÂN TÍCH TRUYỆN lại."
                )
            if not eps:
                self.page.wait_for_timeout(2500)
                eps = self._collect_all_episodes()
            if not eps:
                self._write_debug("toc_no_episodes")
                raise FetchError(
                    "Đã đăng nhập nhưng không đọc được danh sách Tập GameLive. Tool đã lưu file chẩn đoán trong profile GameLive."
                )

            title = self._story_title(story_id)
            episodes = [
                Episode(
                    index=int(row["index"]),
                    title=row["title"] or f"Tập {row['index']}",
                    url=f"{root_url}#episode-{int(row['index'])}",
                    chapter_title="GameLive / Tập",
                    source_id=str(row["index"]),
                )
                for row in eps
            ]
            chapter = Chapter(number=1, title="GameLive / Tập", episodes=episodes)
            for ep in chapter.episodes:
                ep.chapter_no = 1
            if progress:
                progress(f"GameLive: đọc được {len(episodes)} tập ({episodes[0].index}–{episodes[-1].index}).")
            return NovelInfo(
                ncode=f"GAMELIVE_{story_id}",
                title=title,
                author="",
                root_url=root_url,
                chapters=[chapter],
                site="GameLive",
                site_episode_count=len(episodes),
            )
        finally:
            self.close()

    def _ensure_story_page(self, root_url: str):
        if self.context is None or self.page is None:
            self._start(root_url)
        else:
            same_story = False
            try:
                same_story = normalize_gamelive_url(self.page.url.split('#')[0])[1] == normalize_gamelive_url(root_url)[1]
            except Exception:
                same_story = False
            if not same_story:
                self.page.goto(root_url, wait_until="domcontentloaded", timeout=60000)
                self.page.wait_for_timeout(2200)
        if self._login_required():
            raise GameLiveLoginRequired(
                "Phiên đăng nhập GameLive đã hết. Bấm 'MỞ / ĐĂNG NHẬP GAMELIVE' rồi đăng nhập lại."
            )

    def _click_episode(self, index: int):
        if not self.page:
            raise FetchError("Trang GameLive chưa mở.")
        self._check_stop()
        before = ""
        try:
            before = self.page.evaluate(self.MAIN_SIGNATURE_JS)
        except Exception:
            pass
        ok = self.page.evaluate(self.CLICK_EPISODE_JS, index)
        if not ok:
            # Danh sách bên phải có scrollbar; tìm Tập mục tiêu qua từng vùng hiển thị.
            try:
                state = self.page.evaluate(self.SCROLL_EPISODE_PANEL_JS, "top")
            except Exception:
                state = {"ok": False}
            loops = 0
            while not ok and state and state.get("ok") and loops < 120:
                self._check_stop()
                loops += 1
                self.page.wait_for_timeout(160)
                ok = bool(self.page.evaluate(self.CLICK_EPISODE_JS, index))
                if ok:
                    break
                top = int(state.get("scrollTop") or 0)
                max_top = int(state.get("max") or 0)
                client_h = int(state.get("clientHeight") or 500)
                if top >= max_top - 3:
                    break
                state = self.page.evaluate(
                    self.SCROLL_EPISODE_PANEL_JS,
                    min(max_top, top + max(220, int(client_h * 0.72))),
                )
        if not ok:
            raise FetchError(f"Không tìm thấy Tập {index} ở cột TÀI LIỆU GameLive.")
        self.page.wait_for_timeout(350)
        deadline = time.monotonic() + 18
        changed = False
        while time.monotonic() < deadline:
            self._check_stop()
            self.page.wait_for_timeout(300)
            try:
                sig = self.page.evaluate(self.MAIN_SIGNATURE_JS)
                if sig and sig != before:
                    changed = True
                    break
            except Exception:
                pass
        # SPA có thể giữ lại text signature khá giống nhau; sau click vẫn chờ để render ổn định.
        self.page.wait_for_timeout(1000 if changed else 1800)

    @staticmethod
    def _redact_probe_text(text: str, max_len: int = 160000) -> str:
        """Giữ dữ liệu đủ để nhận diện API nhưng che các trường đăng nhập phổ biến."""
        raw = str(text or "")
        # JSON / query / form-like secrets. Không lưu Cookie/Authorization headers ở probe.
        patterns = [
            r'(?i)("?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|authorization|cookie|password|passwd|secret|api[_-]?key)"?\s*[:=]\s*)"[^"\\]*(?:\\.[^"\\]*)*"',
            r'(?i)((?:access[_-]?token|refresh[_-]?token|id[_-]?token|authorization|cookie|password|passwd|secret|api[_-]?key)\s*=)[^&\s]+',
        ]
        for pat in patterns:
            raw = re.sub(pat, lambda m: m.group(1) + '"<REDACTED>"' if ':' in m.group(1) else m.group(1) + '<REDACTED>', raw)
        if len(raw) > max_len:
            raw = raw[:max_len] + f"\n...[TRUNCATED {len(raw)-max_len} chars]"
        return raw

    def probe_api(
        self,
        url: str,
        episode_indices: list[int] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Bắt XHR/fetch khi chuyển vài Tập để tìm endpoint GameLive thật.

        Không lưu Cookie/Authorization headers. Kết quả chỉ dùng để xác định request nào
        mang danh sách/nội dung Tập; bước sau có thể gọi endpoint bằng chính Playwright
        context để dùng phiên đăng nhập hiện tại mà không cần lấy token ra ngoài.
        """
        root_url, story_id = normalize_gamelive_url(url)
        targets = [int(x) for x in (episode_indices or [1, 2, 3]) if int(x) > 0]
        targets = list(dict.fromkeys(targets))[:5]
        if not targets:
            targets = [1, 2, 3]

        events: list[dict] = []
        active_episode = {"value": 0}
        try:
            self._start(root_url)
            if self._login_required():
                raise GameLiveLoginRequired(
                    "GameLive chưa có phiên đăng nhập. Bấm 'MỞ / ĐĂNG NHẬP GAMELIVE' trước rồi chạy DÒ API."
                )
            page = self.page
            if page is None:
                raise FetchError("Không mở được trang GameLive để dò API.")

            def on_response(response):
                try:
                    req = response.request
                    rtype = str(req.resource_type or "")
                    headers = response.headers or {}
                    ctype = str(headers.get("content-type", ""))
                    u = str(response.url or "")
                    if rtype not in {"xhr", "fetch"} and not re.search(r"json|javascript|text/plain|graphql", ctype, re.I):
                        return
                    if "gamelive.online" not in u:
                        return
                    body = ""
                    if re.search(r"json|javascript|text|graphql", ctype, re.I):
                        try:
                            body = response.text()
                        except Exception:
                            body = ""
                    post = ""
                    try:
                        post = str(req.post_data or "")
                    except Exception:
                        post = ""
                    item = {
                        "episode": int(active_episode["value"] or 0),
                        "method": str(req.method or "GET"),
                        "resource_type": rtype,
                        "status": int(response.status),
                        "url": u,
                        "content_type": ctype,
                        "post_data": self._redact_probe_text(post, 50000),
                        "body_sample": self._redact_probe_text(body, 160000),
                    }
                    hay = "\n".join([item["url"], item["post_data"], item["body_sample"]])
                    score = 0
                    ep = item["episode"]
                    if story_id.lower() in hay.lower(): score += 25
                    if ep and re.search(rf"(?<!\d){ep}(?!\d)", hay): score += 20
                    if rtype in {"xhr", "fetch"}: score += 10
                    if re.search(r"episode|chapter|story|stories|document|content|message|novel", hay, re.I): score += 20
                    if len(item["body_sample"]) > 500: score += 10
                    if re.search(r"application/json|graphql", ctype, re.I): score += 10
                    item["score"] = score
                    events.append(item)
                except Exception:
                    pass

            page.on("response", on_response)
            if progress:
                progress("DÒ API: đã gắn bộ bắt XHR/fetch. Tool sẽ mở vài Tập để xem request nào mang nội dung.")

            # Chỉ dùng các Tập thực sự có trong danh sách.
            available = {int(x["index"]) for x in self._collect_all_episodes()}
            targets = [x for x in targets if x in available]
            if not targets:
                targets = sorted(available)[:3]
            if not targets:
                raise FetchError("Không tìm thấy Tập nào để dò API.")

            # Bỏ request do lần load đầu; chỉ giữ các request phát sinh khi đổi Tập.
            events.clear()
            for idx in targets:
                self._check_stop()
                active_episode["value"] = idx
                if progress:
                    progress(f"DÒ API: đang mở Tập {idx}...")
                self._click_episode(idx)
                # Cho các request lazy/follow-up có thời gian hoàn thành.
                page.wait_for_timeout(900)
            active_episode["value"] = 0
            page.wait_for_timeout(700)
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

            # Dedupe nhẹ nhưng giữ request khác POST body / episode.
            unique = []
            seen = set()
            for e in events:
                key = (e.get("episode"), e.get("method"), e.get("url"), e.get("post_data"))
                if key in seen:
                    continue
                seen.add(key)
                unique.append(e)
            unique.sort(key=lambda x: (-int(x.get("score", 0)), int(x.get("episode", 0))))

            folder = self.profile_dir.parent / "GameLive_API_Probe"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            report = {
                "tool_version": APP_VERSION,
                "story_url": root_url,
                "story_id": story_id,
                "episodes_probed": targets,
                "note": "Không lưu Cookie/Authorization headers. Các trường token/password phổ biến đã được che.",
                "candidate_count": len(unique),
                "candidates": unique,
            }
            out = folder / f"gamelive_api_probe_{story_id}_{stamp}.json"
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            # File nhỏ chỉ chứa top endpoint để người dùng dễ gửi/đọc.
            summary = {
                "story_id": story_id,
                "episodes_probed": targets,
                "top_candidates": [
                    {k: e.get(k) for k in ("score", "episode", "method", "status", "resource_type", "url", "content_type", "post_data")}
                    for e in unique[:20]
                ],
            }
            small = folder / f"gamelive_api_probe_SUMMARY_{story_id}_{stamp}.json"
            small.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            if progress:
                progress(f"DÒ API xong: bắt được {len(unique)} request ứng viên.")
                for e in unique[:6]:
                    progress(f"  score={e.get('score',0):>2} | Tập {e.get('episode',0)} | {e.get('method')} {e.get('status')} | {e.get('url')}")
                progress(f"Đã lưu báo cáo: {small}")
            return {"folder": str(folder), "report": str(out), "summary": str(small), "count": len(unique), "top": unique[:10]}
        finally:
            self.close()

    def _write_debug(self, label: str) -> Path:
        folder = self.profile_dir.parent / "GameLive_Debug"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = folder / f"{stamp}_{sanitize_filename(label, 50)}"
        try:
            if self.page:
                self.page.screenshot(path=str(base.with_suffix('.png')), full_page=True)
                base.with_suffix('.html').write_text(self.page.content(), encoding='utf-8')
        except Exception:
            pass
        return folder

    def fetch_episode(self, episode: Episode) -> dict:
        root_url = episode.url.split("#", 1)[0]
        self._ensure_story_page(root_url)
        self._click_episode(episode.index)
        self._check_stop()
        try:
            body_text = clean_text(str(self.page.evaluate(self.EXTRACT_MAIN_TEXT_JS) or ""))
        except Exception as e:
            self._write_debug(f"episode_{episode.index}_extract_error")
            raise FetchError(f"Không đọc được nội dung Tập {episode.index}: {e}") from e

        if len(body_text) < 180:
            folder = self._write_debug(f"episode_{episode.index}_too_short")
            raise FetchError(
                f"Nội dung Tập {episode.index} đọc được quá ngắn. Ảnh/HTML chẩn đoán đã lưu tại: {folder}"
            )

        # Loại một số dòng điều khiển UI thường nằm cuối phần chat nhưng không đụng nội dung truyện.
        lines = [x.rstrip() for x in body_text.splitlines()]
        filtered = []
        ui_exact = {
            "Đề xuất bước tiếp theo",
            "Hỏi, hoặc yêu cầu tập tiếp theo...",
            "Hỏi, hoặc yêu cầu tập tiếp theo…",
            "Gửi",
        }
        for line in lines:
            if line.strip() in ui_exact:
                continue
            filtered.append(line)
        body_text = clean_text("\n".join(filtered))

        return {
            "index": episode.index,
            "title": episode.title or f"Tập {episode.index}",
            "chapter_title": episode.chapter_title,
            "subchapter_title": "",
            "url": episode.url,
            "preface": "",
            "body": body_text,
            "afterword": "",
            "chars_no_whitespace": count_chars_no_whitespace(body_text),
            "chars_plain": count_chars_plain(body_text),
        }


def build_episode_text(data: dict, include_preface: bool, include_afterword: bool) -> str:
    blocks: list[str] = [f"【{data['index']}　{data['title']}】"]
    if include_preface and data.get("preface"):
        blocks += ["[前書き]", data["preface"]]
    blocks.append(data["body"])
    if include_afterword and data.get("afterword"):
        blocks += ["[後書き]", data["afterword"]]
    return "\n\n".join(x for x in blocks if x).strip() + "\n"


CACHE_FORMAT_VERSION = 2


def read_cache(path: Path, expected_url: str) -> dict | None:
    try:
        if not path.exists():
            return None
        wrapped = json.loads(path.read_text(encoding="utf-8"))
        if wrapped.get("cache_version") != CACHE_FORMAT_VERSION:
            return None  # Do not trust caches from an earlier extractor version.
        data = wrapped.get("episode")
        valid, _reason = validate_episode_payload(data, expected_url)
        if not valid:
            return None
        expected_hash = hashlib.sha256(data["body"].encode("utf-8")).hexdigest()
        if wrapped.get("body_sha256") != expected_hash:
            return None
        return data
    except Exception:
        return None


def write_cache(path: Path, data: dict) -> None:
    valid, reason = validate_episode_payload(data, data.get("url", ""))
    if not valid:
        raise FetchError(f"Không lưu cache không hợp lệ: {reason}")
    wrapper = {
        "cache_version": CACHE_FORMAT_VERSION,
        "source_url": data["url"],
        "body_sha256": hashlib.sha256(data["body"].encode("utf-8")).hexdigest(),
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "episode": data,
    }
    atomic_write_text(path, json.dumps(wrapper, ensure_ascii=False, indent=2), encoding="utf-8")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1240x830")
        self.minsize(1040, 720)

        self.novel: NovelInfo | None = None
        self._loaded_url = ""
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_result_folder: Path | None = None
        self.last_auto_report_folder: Path | None = None

        self.url_var = tk.StringVar(value="https://novel18.syosetu.com/n4940gb/")
        self.out_var = tk.StringVar(value=str(Path.home() / "Downloads" / "Truyen_Nhat_TXT"))
        self.folder_name_var = tk.StringVar(value="")
        self.delay_var = tk.DoubleVar(value=DEFAULT_DELAY)
        self.include_preface_var = tk.BooleanVar(value=False)
        self.include_afterword_var = tk.BooleanVar(value=False)
        self.output_episode_var = tk.BooleanVar(value=False)
        self.output_chapter_var = tk.BooleanVar(value=True)
        self.output_all_var = tk.BooleanVar(value=False)
        self.use_cache_var = tk.BooleanVar(value=True)
        self.episode_spec_var = tk.StringVar(value="1")
        self.selected_episode_numbers: set[int] = set()
        self.progress_var = tk.DoubleVar(value=0)
        self.status_var = tk.StringVar(value="Sẵn sàng. Dán URL truyện rồi bấm Phân tích truyện; sau đó chọn theo số episode/tập nhỏ.")
        self.novel_info_var = tk.StringVar(value="Chưa phân tích tác phẩm")
        self.source_var = tk.StringVar(value="Narou R18")

        self._setup_style()
        self._build_ui()
        self.url_var.trace_add("write", self._on_url_changed)

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#F4F7FB"
        card = "#FFFFFF"
        ink = "#172033"
        muted = "#657084"
        border = "#D9E1EC"
        accent = "#2563EB"
        accent_hover = "#1D4ED8"
        danger = "#DC2626"

        self.configure(bg=bg)
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("TLabel", background=bg, foreground=ink, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=card, foreground=ink, font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=card, foreground=muted, font=("Segoe UI", 9))
        style.configure("Info.TLabel", background=card, foreground=ink, font=("Segoe UI Semibold", 10))
        style.configure("Header.TLabel", background="#172033", foreground="#FFFFFF", font=("Segoe UI Semibold", 19))
        style.configure("HeaderSub.TLabel", background="#172033", foreground="#C9D4E7", font=("Segoe UI", 9))
        style.configure("Section.TLabelframe", background=card, bordercolor=border, relief="solid", borderwidth=1)
        style.configure("Section.TLabelframe.Label", background=card, foreground=ink, font=("Segoe UI Semibold", 10))
        style.configure("TCheckbutton", background=card, foreground=ink, font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#FFFFFF", bordercolor=border, padding=6)
        style.configure("TSpinbox", fieldbackground="#FFFFFF", bordercolor=border, padding=4)
        style.configure("TButton", font=("Segoe UI Semibold", 9), padding=(12, 7))
        style.configure("Accent.TButton", background=accent, foreground="#FFFFFF", bordercolor=accent, focuscolor=accent)
        style.map("Accent.TButton", background=[("active", accent_hover), ("pressed", accent_hover)], foreground=[("disabled", "#D7E3FF")])
        style.configure("Danger.TButton", background="#FFF1F2", foreground=danger, bordercolor="#FECACA")
        style.map("Danger.TButton", background=[("active", "#FFE4E6")])
        style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=ink, rowheight=27, bordercolor=border, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#EEF3F9", foreground=ink, font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview.Heading", background=[("active", "#E5EDF7")])
        style.configure("Horizontal.TProgressbar", background=accent, troughcolor="#E8EEF7", bordercolor="#E8EEF7")

    def _build_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = tk.Frame(self, bg="#172033", height=76)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_wrap = tk.Frame(header, bg="#172033")
        title_wrap.grid(row=0, column=0, sticky="w", padx=20, pady=13)
        ttk.Label(title_wrap, text="TRUYỆN NHẬT TXT TOOL", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_wrap,
            text="Narou • Narou R18 • Kakuyomu • GameLive • Auto Web  |  tự phân tích site mới, test 3 episode rồi tải TXT",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            header, text=f"v{APP_VERSION}", bg="#2563EB", fg="white",
            font=("Segoe UI Semibold", 9), padx=10, pady=5,
        ).grid(row=0, column=1, padx=20, pady=18, sticky="e")

        content = ttk.Frame(self)
        content.grid(row=1, column=0, sticky="nsew", padx=14, pady=12)
        content.grid_rowconfigure(3, weight=1)
        content.grid_columnconfigure(0, weight=1)

        # Source card
        source = ttk.LabelFrame(content, text="1  Nguồn truyện & thư mục lưu", style="Section.TLabelframe")
        source.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        source.grid_columnconfigure(1, weight=1)

        ttk.Label(source, text="URL", style="Card.TLabel").grid(row=0, column=0, padx=(12, 8), pady=(11, 6), sticky="w")
        self.url_entry = ttk.Entry(source, textvariable=self.url_var, font=("Segoe UI", 10))
        self.url_entry.grid(row=0, column=1, padx=(0, 8), pady=(11, 6), sticky="ew")
        self.url_entry.bind("<KeyRelease>", lambda _e: self._refresh_source_badge())
        self.source_badge = tk.Label(source, textvariable=self.source_var, bg="#FFF3E0", fg="#9A4F00", font=("Segoe UI Semibold", 9), padx=10, pady=5)
        self.source_badge.grid(row=0, column=2, padx=(0, 8), pady=(11, 6))
        self.btn_load = ttk.Button(source, text="PHÂN TÍCH", style="Accent.TButton", command=self.load_toc)
        self.btn_load.grid(row=0, column=3, padx=(0, 12), pady=(11, 6))

        ttk.Label(source, text="Lưu tại", style="Card.TLabel").grid(row=1, column=0, padx=(12, 8), pady=(4, 11), sticky="w")
        ttk.Entry(source, textvariable=self.out_var).grid(row=1, column=1, padx=(0, 8), pady=(4, 11), sticky="ew")
        ttk.Button(source, text="Chọn…", command=self.choose_output).grid(row=1, column=2, padx=(0, 8), pady=(4, 11))
        ttk.Button(source, text="Mở thư mục", command=self.open_output_folder).grid(row=1, column=3, padx=(0, 12), pady=(4, 11))

        ttk.Label(source, text="Tên thư mục", style="Card.TLabel").grid(row=2, column=0, padx=(12, 8), pady=(0, 11), sticky="w")
        ttk.Entry(source, textvariable=self.folder_name_var).grid(row=2, column=1, padx=(0, 8), pady=(0, 11), sticky="ew")
        ttk.Label(source, text="Tự đặt (để trống = mã truyện); không dùng tiêu đề dài", style="CardMuted.TLabel").grid(
            row=2, column=2, columnspan=2, padx=(0, 12), pady=(0, 11), sticky="w"
        )

        # Novel info strip
        info = ttk.Frame(content, style="Card.TFrame", padding=(12, 8))
        info.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(info, textvariable=self.novel_info_var, style="Info.TLabel").pack(side="left", fill="x", expand=True)

        # Options
        opts = ttk.LabelFrame(content, text="2  Tùy chọn xuất", style="Section.TLabelframe")
        opts.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for c in range(4):
            opts.grid_columnconfigure(c, weight=1 if c < 3 else 0)

        ttk.Checkbutton(opts, text="Mỗi chương lớn = 1 TXT", variable=self.output_chapter_var).grid(row=0, column=0, padx=12, pady=(9, 4), sticky="w")
        ttk.Checkbutton(opts, text="Mỗi episode = 1 TXT", variable=self.output_episode_var).grid(row=0, column=1, padx=12, pady=(9, 4), sticky="w")
        ttk.Checkbutton(opts, text="Gộp tất cả = 1 TXT", variable=self.output_all_var).grid(row=0, column=2, padx=12, pady=(9, 4), sticky="w")
        ttk.Checkbutton(opts, text="Dùng cache", variable=self.use_cache_var).grid(row=0, column=3, padx=12, pady=(9, 4), sticky="w")

        ttk.Checkbutton(opts, text="Lấy 前書き (Narou)", variable=self.include_preface_var).grid(row=1, column=0, padx=12, pady=(3, 9), sticky="w")
        ttk.Checkbutton(opts, text="Lấy 後書き (Narou)", variable=self.include_afterword_var).grid(row=1, column=1, padx=12, pady=(3, 9), sticky="w")
        ttk.Label(opts, text="Delay/request", style="CardMuted.TLabel").grid(row=1, column=2, padx=(12, 4), pady=(3, 9), sticky="e")
        ttk.Spinbox(opts, from_=1.0, to=15.0, increment=0.2, textvariable=self.delay_var, width=7).grid(row=1, column=3, padx=12, pady=(3, 9), sticky="w")

        # Work area
        work = ttk.Panedwindow(content, orient="horizontal")
        work.grid(row=3, column=0, sticky="nsew", pady=(0, 8))

        left = ttk.LabelFrame(work, text="3  Chương / phạm vi episode", style="Section.TLabelframe")
        right = ttk.LabelFrame(work, text="Nhật ký xử lý", style="Section.TLabelframe")
        work.add(left, weight=3)
        work.add(right, weight=2)

        columns = ("stt", "chuong", "so_tap", "pham_vi", "da_chon")
        self.chapter_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="none", height=15)
        self.chapter_tree.heading("stt", text="#")
        self.chapter_tree.heading("chuong", text="Tên chương")
        self.chapter_tree.heading("so_tap", text="Tập")
        self.chapter_tree.heading("pham_vi", text="Episode")
        self.chapter_tree.heading("da_chon", text="Đã chọn")
        self.chapter_tree.column("stt", width=44, anchor="center", stretch=False)
        self.chapter_tree.column("chuong", width=305, anchor="w")
        self.chapter_tree.column("so_tap", width=60, anchor="center", stretch=False)
        self.chapter_tree.column("pham_vi", width=90, anchor="center", stretch=False)
        self.chapter_tree.column("da_chon", width=78, anchor="center", stretch=False)
        self.chapter_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.chapter_tree.yview)
        tree_scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.chapter_tree.configure(yscrollcommand=tree_scroll.set)

        self.log_box = tk.Text(
            right, wrap="word", height=18, font=("Cascadia Mono", 9),
            bg="#0F172A", fg="#D9E4F2", insertbackground="#FFFFFF",
            selectbackground="#334155", relief="flat", padx=10, pady=9,
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_box.configure(state="disabled")

        # Selection + actions
        controls = ttk.Frame(content, style="Card.TFrame", padding=(10, 8))
        controls.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        controls.grid_columnconfigure(6, weight=1)
        ttk.Label(controls, text="Episode", style="Card.TLabel").grid(row=0, column=0, padx=(2, 6), sticky="w")
        ttk.Entry(controls, textvariable=self.episode_spec_var, width=18).grid(row=0, column=1, padx=(0, 5))
        ttk.Button(controls, text="Áp dụng", command=self.apply_episode_spec).grid(row=0, column=2, padx=4)
        ttk.Button(controls, text="Tập 1", command=self.select_episode_1).grid(row=0, column=3, padx=4)
        ttk.Button(controls, text="Tất cả", command=self.select_all_episodes).grid(row=0, column=4, padx=4)
        ttk.Button(controls, text="Xóa lọc", command=self.clear_episode_filter).grid(row=0, column=5, padx=4)
        ttk.Label(controls, text="VD: 1-32  •  50  •  1,3,10-25", style="CardMuted.TLabel").grid(row=0, column=6, padx=10, sticky="w")

        self.btn_open = ttk.Button(controls, text="Mở kết quả", command=self.open_result_folder, state="disabled")
        self.btn_open.grid(row=0, column=7, padx=4)
        self.btn_cancel = ttk.Button(controls, text="Dừng", style="Danger.TButton", command=self.cancel, state="disabled")
        self.btn_cancel.grid(row=0, column=8, padx=4)
        self.btn_download = ttk.Button(controls, text="TẢI + GHÉP TXT", style="Accent.TButton", command=self.download_selected, state="disabled")
        self.btn_download.grid(row=0, column=9, padx=(4, 2))

        # GameLive helper row
        tools = ttk.Frame(content)
        tools.grid(row=5, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(tools, text="GameLive:", style="Muted.TLabel").pack(side="left", padx=(2, 6))
        self.btn_gamelive_login = ttk.Button(tools, text="Mở / đăng nhập", command=self.open_gamelive_login)
        self.btn_gamelive_login.pack(side="left", padx=3)
        self.btn_gamelive_probe = ttk.Button(tools, text="Dò API", command=self.probe_gamelive_api)
        self.btn_gamelive_probe.pack(side="left", padx=3)
        self.btn_auto_report = ttk.Button(tools, text="Mở báo cáo Auto", command=self.open_auto_report, state="disabled")
        self.btn_auto_report.pack(side="left", padx=(12, 3))
        ttk.Label(tools, text="Site lạ: tool tự phân tích HTML/DOM, test 3 episode; không chắc chắn thì dừng và xuất báo cáo.", style="Muted.TLabel").pack(side="left", padx=10)

        # Progress/status
        bottom = ttk.Frame(content)
        bottom.grid(row=6, column=0, sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bottom, variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(2, 10))
        ttk.Label(bottom, textvariable=self.status_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", padx=2, pady=(4, 0))

        self._refresh_source_badge()

    def _invalidate_novel(self):
        """Never allow a previous TOC to be downloaded after URL changes or analysis fails."""
        self.novel = None
        self._loaded_url = ""
        self.selected_episode_numbers.clear()
        self.novel_info_var.set("Chưa phân tích URL hiện tại")
        self.episode_spec_var.set("")
        for iid in self.chapter_tree.get_children():
            self.chapter_tree.delete(iid)
        self.btn_download.configure(state="disabled")

    def _on_url_changed(self, *_):
        self._refresh_source_badge()
        if self._loaded_url and self.url_var.get().strip() != self._loaded_url:
            self._invalidate_novel()
            self.status_var.set("Đã đổi URL. Bấm PHÂN TÍCH lại trước khi tải.")

    def _refresh_source_badge(self):
        raw = self.url_var.get().strip()
        label = "Chưa nhận diện"
        bg, fg = "#EEF2F7", "#526174"
        try:
            site = detect_site(raw)
            label = site
            if site == "Narou R18":
                bg, fg = "#FFF3E0", "#9A4F00"
            elif site == "Narou":
                bg, fg = "#E8F5E9", "#17662A"
            elif site == "Kakuyomu":
                bg, fg = "#F3E8FF", "#6B21A8"
            elif site == "GameLive":
                bg, fg = "#E0F2FE", "#075985"
            elif site == "Auto Web":
                bg, fg = "#E8EEF9", "#1E3A8A"
        except Exception:
            pass
        self.source_var.set(label)
        if hasattr(self, "source_badge"):
            self.source_badge.configure(bg=bg, fg=fg)

    def ui(self, fn: Callable, *args, **kwargs):
        """Chạy callback an toàn trên UI thread, hỗ trợ cả positional và keyword args."""
        self.after(0, lambda: fn(*args, **kwargs))

    def log(self, msg: str):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg.rstrip() + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.ui(_append)

    def set_busy(self, busy: bool):
        def _set():
            self.btn_load.configure(state="disabled" if busy else "normal")
            self.url_entry.configure(state="disabled" if busy else "normal")
            self.btn_gamelive_login.configure(state="disabled" if busy else "normal")
            self.btn_gamelive_probe.configure(state="disabled" if busy else "normal")
            self.btn_download.configure(state="disabled" if busy or self.novel is None or self._loaded_url != self.url_var.get().strip() else "normal")
            self.btn_cancel.configure(state="normal" if busy else "disabled")

        self.ui(_set)

    def choose_output(self):
        folder = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.home()))
        if folder:
            self.out_var.set(folder)

    def _all_episode_indices(self) -> list[int]:
        if self.novel is None:
            return []
        return [ep.index for ch in self.novel.chapters for ep in ch.episodes]

    def _refresh_episode_counts(self):
        if self.novel is None:
            return
        selected = self.selected_episode_numbers
        for ch in self.novel.chapters:
            iid = str(ch.number)
            if not self.chapter_tree.exists(iid):
                continue
            first = ch.episodes[0].index
            last = ch.episodes[-1].index
            selected_count = sum(1 for ep in ch.episodes if ep.index in selected)
            self.chapter_tree.item(
                iid,
                values=(ch.number, ch.title, len(ch.episodes), f"{first}–{last}", f"{selected_count}/{len(ch.episodes)}"),
            )

    def clear_episode_filter(self):
        self.selected_episode_numbers.clear()
        self.episode_spec_var.set("")
        self._refresh_episode_counts()
        self.status_var.set("Đã xóa lọc episode. Nhập số episode cần tải rồi bấm Áp dụng.")

    def select_all_episodes(self):
        indices = self._all_episode_indices()
        if not indices:
            return
        self.episode_spec_var.set(f"{min(indices)}-{max(indices)}")
        self.apply_episode_spec()

    def select_episode_1(self):
        if not self._all_episode_indices():
            return
        self.episode_spec_var.set("1")
        self.apply_episode_spec()

    def apply_episode_spec(self):
        """Chọn episode từ ô nhập: 1-32, 50 hoặc 1,3,10-25."""
        if self.novel is None:
            messagebox.showwarning(APP_NAME, "Hãy bấm PHÂN TÍCH TRUYỆN trước.")
            return
        indices = self._all_episode_indices()
        if not indices:
            messagebox.showwarning(APP_NAME, "Không tìm thấy episode nào trong truyện.")
            return
        try:
            numbers = parse_episode_spec(self.episode_spec_var.get(), max(indices))
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e))
            return

        available = set(indices)
        missing = [n for n in numbers if n not in available]
        if missing:
            preview = ", ".join(str(n) for n in missing[:12])
            more = "…" if len(missing) > 12 else ""
            messagebox.showwarning(APP_NAME, f"Không tìm thấy episode: {preview}{more}")
            return

        self.selected_episode_numbers = set(numbers)
        self._refresh_episode_counts()
        first, last = min(numbers), max(numbers)
        if len(numbers) == last - first + 1:
            pretty = f"{first}-{last}" if first != last else str(first)
        else:
            pretty = self.episode_spec_var.get().strip()
        self.status_var.set(
            f"Đã chọn {len(numbers)} episode ({pretty}). Bấm TẢI + GHÉP TXT EPISODE ĐÃ CHỌN."
        )

    def open_output_folder(self):
        raw = self.out_var.get().strip()
        if not raw:
            messagebox.showwarning(APP_NAME, "Chưa chọn thư mục lưu.")
            return
        folder = Path(raw).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Không mở được thư mục lưu:\n{e}")

    def open_auto_report(self):
        folder = self.last_auto_report_folder
        if folder is None or not folder.exists():
            messagebox.showwarning(APP_NAME, "Chưa có báo cáo Auto Web.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Không mở được thư mục báo cáo:\n{e}")

    def cancel(self):
        self.stop_event.set()
        self.status_var.set("Đang dừng an toàn…")

    def open_result_folder(self):
        folder = self.last_result_folder
        if folder is None or not folder.exists():
            messagebox.showwarning(APP_NAME, "Chưa có thư mục kết quả.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Không mở được thư mục:\n{e}")

    def open_gamelive_login(self):
        if self.worker and self.worker.is_alive():
            return
        raw = self.url_var.get().strip()
        try:
            site = detect_site(raw)
            if site != "GameLive":
                raise ValueError("Nút này dùng cho URL gamelive.online/app/stories/...")
            normalize_gamelive_url(raw)
        except Exception as e:
            messagebox.showwarning(APP_NAME, str(e))
            return

        self.stop_event.clear()
        self.set_busy(True)
        self.status_var.set("Đang mở Chrome GameLive. Hãy đăng nhập trong cửa sổ vừa mở...")
        self.log("=== ĐĂNG NHẬP GAMELIVE ===")

        def job():
            try:
                client = GameLiveClient(delay=self.delay_var.get(), stop_event=self.stop_event)
                client.open_login(raw, progress=self.log)
                self.log("Đã lưu phiên đăng nhập GameLive trong profile riêng của tool.")
                self.ui(self.status_var.set, "Đăng nhập GameLive đã sẵn sàng. Bấm PHÂN TÍCH TRUYỆN.")
                self.ui(messagebox.showinfo, APP_NAME, "Đã nhận và lưu phiên đăng nhập GameLive.\n\nBây giờ bấm PHÂN TÍCH TRUYỆN.")
            except StopRequested:
                self.log("Đã dừng đăng nhập GameLive.")
                self.ui(self.status_var.set, "Đã dừng")
            except Exception as e:
                self.log(f"LỖI GAMELIVE: {e}")
                self.ui(messagebox.showerror, APP_NAME, str(e))
                self.ui(self.status_var.set, "Chưa đăng nhập được GameLive")
            finally:
                self.set_busy(False)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def probe_gamelive_api(self):
        if self.worker and self.worker.is_alive():
            return
        raw = self.url_var.get().strip()
        try:
            if detect_site(raw) != "GameLive":
                raise ValueError("DÒ API chỉ dùng cho URL GameLive.")
            normalize_gamelive_url(raw)
        except Exception as e:
            messagebox.showwarning(APP_NAME, str(e))
            return

        # Nếu đã PHÂN TÍCH, ưu tiên 3 episode đầu người dùng đang chọn; nếu chưa thì 1,2,3.
        targets = [1, 2, 3]
        try:
            if self.novel and self.novel.site == "GameLive":
                groups = self._get_selected_episode_groups()
                nums = [ep.index for _ch, eps in groups for ep in eps]
                if nums:
                    targets = nums[:3]
        except Exception:
            pass

        self.stop_event.clear()
        self.set_busy(True)
        self.status_var.set("Đang dò request/API GameLive...")
        self.log("=== DÒ API GAMELIVE ===")
        self.log("Không lưu Cookie/Authorization. Tool chỉ bắt URL, method, POST body đã che secret và mẫu response.")

        def job():
            try:
                client = GameLiveClient(delay=self.delay_var.get(), stop_event=self.stop_event)
                result = client.probe_api(raw, targets, progress=self.log)
                folder = Path(result["folder"])
                self.last_result_folder = folder
                self.ui(self.btn_open.configure, state="normal")
                self.ui(self.status_var.set, f"Dò API xong: {result['count']} request ứng viên")
                self.ui(
                    messagebox.showinfo,
                    APP_NAME,
                    "Đã dò xong API GameLive.\n\n"
                    f"Báo cáo nhỏ:\n{result['summary']}\n\n"
                    "Gửi file SUMMARY này cho mình để xác định endpoint và làm chế độ API FAST.",
                )
            except StopRequested:
                self.log("Đã dừng dò API.")
                self.ui(self.status_var.set, "Đã dừng")
            except GameLiveLoginRequired as e:
                self.log(f"GAMELIVE CẦN ĐĂNG NHẬP: {e}")
                self.ui(messagebox.showwarning, APP_NAME, str(e))
                self.ui(self.status_var.set, "GameLive cần đăng nhập trước")
            except Exception as e:
                self.log(f"LỖI DÒ API: {e}")
                self.ui(messagebox.showerror, APP_NAME, str(e))
                self.ui(self.status_var.set, "Dò API thất bại")
            finally:
                self.set_busy(False)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def load_toc(self):
        if self.worker and self.worker.is_alive():
            return

        self.stop_event.clear()
        self._invalidate_novel()
        self.set_busy(True)
        self.progress_var.set(0)
        self.status_var.set("Đang phân tích mục lục…")
        self.log("=== PHÂN TÍCH TRUYỆN ===")

        url = self.url_var.get().strip()
        delay = self.delay_var.get()

        def job():
            try:
                site = detect_site(url)
                if site == "Kakuyomu":
                    client = KakuyomuClient(delay=delay, stop_event=self.stop_event)
                elif site == "GameLive":
                    client = GameLiveClient(delay=delay, stop_event=self.stop_event)
                elif site == "Auto Web":
                    client = AutoWebClient(delay=delay, stop_event=self.stop_event)
                    self.log("AUTO WEB: domain chưa có adapter riêng; bắt đầu phân tích tự động an toàn.")
                else:  # Narou + Narou R18
                    client = NarouClient(delay=delay, stop_event=self.stop_event)
                novel = client.load_toc(url, progress=self.log)
                if novel.site_episode_count and novel.episode_count != novel.site_episode_count:
                    raise FetchError(
                        f"TOC KHÔNG ĐỦ: nguồn báo {novel.site_episode_count} episode "
                        f"nhưng tool mới tìm thấy {novel.episode_count}. Không cho tải thiếu."
                    )
                report_path = str(getattr(novel, "analysis_report", "") or "")
                if report_path:
                    self.last_auto_report_folder = Path(report_path).parent
                    self.ui(self.btn_auto_report.configure, state="normal")

                def populate():
                    if url != self.url_var.get().strip():
                        self._invalidate_novel()
                        self.status_var.set("URL thay đổi trong khi phân tích; hãy phân tích lại.")
                        return
                    self.novel = novel
                    self._loaded_url = url
                    for item in self.chapter_tree.get_children():
                        self.chapter_tree.delete(item)

                    for ch in novel.chapters:
                        first = ch.episodes[0].index
                        last = ch.episodes[-1].index
                        self.chapter_tree.insert(
                            "",
                            "end",
                            iid=str(ch.number),
                            values=(ch.number, ch.title, len(ch.episodes), f"{first}–{last}", "0/" + str(len(ch.episodes))),
                        )

                    if novel.chapters and novel.chapters[0].episodes:
                        first_ep = novel.chapters[0].episodes[0].index
                        last_ep = novel.chapters[0].episodes[-1].index
                        self.episode_spec_var.set(f"{first_ep}-{last_ep}" if first_ep != last_ep else str(first_ep))
                        self.apply_episode_spec()
                    author_text = f" | Tác giả: {novel.author}" if novel.author else ""
                    website_count = ""
                    if novel.site_total_chars:
                        website_count = f" | Website báo {novel.site_total_chars:,} ký tự"
                    display_title = novel.title[:110] + "…" if len(novel.title) > 110 else novel.title
                    self.novel_info_var.set(
                        f"[{novel.site}] {display_title} | {novel.ncode}{author_text} | "
                        f"{len(novel.chapters)} chương lớn | {novel.episode_count} episode{website_count}"
                    )
                    self.status_var.set("Đã đọc xong. Nhập số episode cần lấy, ví dụ 1-32 hoặc 1,3,10-25, rồi bấm Áp dụng.")
                    self.btn_download.configure(state="normal")

                self.ui(populate)
                self.log(f"Tên truyện : {novel.title}")
                self.log(f"Tác giả    : {novel.author or '(không đọc được)'}")
                self.log(f"Website    : {novel.site}")
                self.log(f"Mã truyện  : {novel.ncode}")
                self.log(f"Chương lớn : {len(novel.chapters)}")
                self.log(f"Episode    : {novel.episode_count}")
                if novel.site_episode_count:
                    self.log(f"Website báo: {novel.site_episode_count} episode")
                if novel.site_total_chars:
                    self.log(f"Website báo: {novel.site_total_chars:,} ký tự toàn tác phẩm")
                self.log("Mặc định đã chọn các episode thuộc chương lớn đầu tiên.")
            except (StopRequested, AutoStopRequested):
                self.log("Đã dừng.")
                self.ui(self.status_var.set, "Đã dừng")
            except AutoAnalysisError as e:
                report = str(getattr(e, "report_path", "") or "")
                if report:
                    self.last_auto_report_folder = Path(report).parent
                    self.ui(self.btn_auto_report.configure, state="normal")
                    self.log(f"AUTO WEB REPORT: {report}")
                self.log(f"AUTO WEB FAIL: {e}")
                self.ui(messagebox.showwarning, APP_NAME, str(e))
                self.ui(self.status_var.set, "Auto Web không đủ tin cậy — đã xuất báo cáo")
            except GameLiveLoginRequired as e:
                self.log(f"GAMELIVE CẦN ĐĂNG NHẬP: {e}")
                self.ui(messagebox.showwarning, APP_NAME, str(e))
                self.ui(self.status_var.set, "GameLive cần đăng nhập trước")
            except Exception as e:
                self.log(f"LỖI: {e}")
                self.ui(messagebox.showerror, APP_NAME, str(e))
                self.ui(self.status_var.set, "Có lỗi khi đọc mục lục")
            finally:
                try:
                    if 'client' in locals() and hasattr(client, "close"):
                        client.close()
                except Exception:
                    pass
                self.set_busy(False)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def _get_selected_episode_groups(self) -> list[tuple[Chapter, list[Episode]]]:
        if self.novel is None:
            return []
        indices = self._all_episode_indices()
        if not indices:
            return []
        numbers = parse_episode_spec(self.episode_spec_var.get(), max(indices))
        available = set(indices)
        missing = [n for n in numbers if n not in available]
        if missing:
            raise ValueError("Có episode không tồn tại trong truyện: " + ", ".join(map(str, missing[:12])))
        selected = set(numbers)
        self.selected_episode_numbers = selected
        groups: list[tuple[Chapter, list[Episode]]] = []
        for ch in self.novel.chapters:
            eps = [ep for ep in ch.episodes if ep.index in selected]
            if eps:
                groups.append((ch, eps))
        return groups

    def download_selected(self):
        if self.novel is None or self._loaded_url != self.url_var.get().strip():
            messagebox.showwarning(APP_NAME, "Hãy PHÂN TÍCH URL hiện tại trước khi tải.")
            return

        try:
            groups = self._get_selected_episode_groups()
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        if not groups:
            messagebox.showwarning(APP_NAME, "Hãy nhập ít nhất một episode/tập nhỏ cần tải.")
            return
        self._refresh_episode_counts()
        chapters = [ch for ch, _eps in groups]

        if not (self.output_episode_var.get() or self.output_chapter_var.get() or self.output_all_var.get()):
            messagebox.showwarning(APP_NAME, "Hãy chọn ít nhất một kiểu xuất TXT.")
            return

        if not self.out_var.get().strip():
            messagebox.showwarning(APP_NAME, "Hãy chọn thư mục lưu.")
            return

        out_root = Path(self.out_var.get().strip()).expanduser()
        custom_name = safe_windows_name(self.folder_name_var.get().strip() or self.novel.ncode, 48)
        include_preface = self.include_preface_var.get()
        include_afterword = self.include_afterword_var.get()
        output_episode = self.output_episode_var.get()
        output_chapter = self.output_chapter_var.get()
        output_all = self.output_all_var.get()
        use_cache = self.use_cache_var.get()
        delay = self.delay_var.get()

        self.stop_event.clear()
        self.set_busy(True)
        self.progress_var.set(0)
        self.log("=== TẢI VÀ GHÉP TXT ===")

        def job():
            try:
                if self.novel.site == "Kakuyomu":
                    client = KakuyomuClient(delay=delay, stop_event=self.stop_event)
                elif self.novel.site == "GameLive":
                    client = GameLiveClient(delay=delay, stop_event=self.stop_event)
                elif str(self.novel.site).startswith("Auto Web"):
                    client = AutoWebClient(
                        delay=delay,
                        stop_event=self.stop_event,
                        profile_path=str(getattr(self.novel, "profile_path", "") or ""),
                    )
                else:  # Narou + Narou R18
                    client = NarouClient(delay=delay, stop_event=self.stop_event)
                base_folder = out_root / custom_name
                run_token = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                novel_folder = make_run_directory(base_folder, run_token)
                episode_folder = novel_folder / "episodes"
                chapter_folder = novel_folder / "chapters"
                cache_folder = base_folder / ".cache" / safe_windows_name(self.novel.ncode, 48)
                atomic_write_text(novel_folder / "_TRANG_THAI_DANG_TAI.txt", "Đang tải hoặc chưa hoàn thành QA.\n")
                if output_episode:
                    episode_folder.mkdir(parents=True, exist_ok=True)
                if output_chapter:
                    chapter_folder.mkdir(parents=True, exist_ok=True)
                if use_cache:
                    cache_folder.mkdir(parents=True, exist_ok=True)

                selected_eps = [ep for _ch, eps in groups for ep in eps]
                total = len(selected_eps)
                done = 0
                manifest_rows: list[dict] = []
                chapter_summary_rows: list[dict] = []
                all_parts: list[str] = []

                for ch, chapter_eps in groups:
                    chapter_parts: list[str] = []
                    ch_header = ch.title
                    chapter_parts.append(ch_header)
                    all_parts.append(ch_header)
                    ch_chars = 0
                    ch_plain = 0
                    ch_episode_count = 0
                    last_subchapter = ""

                    for ep in chapter_eps:
                        if self.stop_event.is_set():
                            raise StopRequested()

                        self.ui(
                            self.status_var.set,
                            f"Đang xử lý {done + 1}/{total} | Chương {ch.number} | Episode {ep.index}: {ep.title[:75]}",
                        )

                        cache_path = cache_folder / f"{ep.index:05d}.json"
                        data = read_cache(cache_path, ep.url) if use_cache else None

                        if data is not None:
                            self.log(f"[CACHE] {ep.index}: {ep.title}")
                        else:
                            self.log(f"[TẢI]   {ep.index}: {ep.title}")
                            data = client.fetch_episode(ep)
                            valid, reason = validate_episode_payload(data, ep.url)
                            if not valid:
                                raise FetchError(f"Episode {ep.index} có nội dung không hợp lệ: {reason}")
                            if use_cache:
                                write_cache(cache_path, data)

                        # Đảm bảo cache cũ vẫn có 2 trường đếm mới.
                        data["chars_no_whitespace"] = int(
                            data.get("chars_no_whitespace", count_chars_no_whitespace(data["body"]))
                        )
                        data["chars_plain"] = int(data.get("chars_plain", count_chars_plain(data["body"])))

                        ep_text = build_episode_text(data, include_preface, include_afterword)

                        subchapter = str(data.get("subchapter_title") or ep.subchapter_title or "").strip()
                        if subchapter and subchapter != last_subchapter:
                            chapter_parts.append(subchapter)
                            all_parts.append(subchapter)
                            last_subchapter = subchapter

                        if output_episode:
                            ep_name = f"{data['index']:04d}_{safe_windows_name(data['title'], 68)}.txt"
                            atomic_write_text(episode_folder / ep_name, ep_text)

                        chapter_parts.append(ep_text.rstrip())
                        all_parts.append(ep_text.rstrip())

                        ch_chars += data["chars_no_whitespace"]
                        ch_plain += data["chars_plain"]
                        ch_episode_count += 1

                        manifest_rows.append(
                            {
                                "episode": data["index"],
                                "chapter_no": ch.number,
                                "chapter_title": ch.title,
                                "episode_title": data["title"],
                                "chars_no_whitespace": data["chars_no_whitespace"],
                                "chars_plain": data["chars_plain"],
                                "url": data["url"],
                                "body_sha256": hashlib.sha256(data["body"].encode("utf-8")).hexdigest(),
                            }
                        )

                        done += 1
                        self.ui(self.progress_var.set, (done / total) * 100 if total else 100)

                    if output_chapter:
                        selected_indices = [ep.index for ep in chapter_eps]
                        full_chapter = len(chapter_eps) == len(ch.episodes)
                        contiguous = selected_indices == list(range(selected_indices[0], selected_indices[-1] + 1))
                        if full_chapter:
                            suffix = ""
                        elif contiguous:
                            suffix = f"_EP{selected_indices[0]:04d}-{selected_indices[-1]:04d}"
                        else:
                            suffix = "_EP_DA_CHON"
                        ch_name = f"{ch.number:02d}_{safe_windows_name(ch.title, 65)}{suffix}.txt"
                        chapter_text = "\n\n".join(chapter_parts).strip() + "\n"
                        atomic_write_text(chapter_folder / ch_name, chapter_text)

                    chapter_summary_rows.append(
                        {
                            "chapter_no": ch.number,
                            "chapter_title": ch.title,
                            "episodes": ch_episode_count,
                            "first_episode": chapter_eps[0].index,
                            "last_episode": chapter_eps[-1].index,
                            "chars_no_whitespace": ch_chars,
                            "chars_plain": ch_plain,
                        }
                    )
                    self.log(
                        f"=> Chương {ch.number:02d}: {ch_episode_count} episode | "
                        f"{ch_chars:,} ký tự本文 (bỏ whitespace)"
                    )

                if output_all:
                    all_name = "TAT_CA_EPISODE_DA_CHON.txt"
                    atomic_write_text(novel_folder / all_name, "\n\n".join(all_parts).strip() + "\n")

                # Manifest từng episode.
                manifest_path = novel_folder / "01_DANH_SACH_EPISODE.tsv"
                with manifest_path.open("w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(
                        f,
                        fieldnames=[
                            "episode",
                            "chapter_no",
                            "chapter_title",
                            "episode_title",
                            "chars_no_whitespace",
                            "chars_plain",
                            "url",
                            "body_sha256",
                        ],
                        delimiter="\t",
                    )
                    writer.writeheader()
                    writer.writerows(manifest_rows)

                # Báo cáo từng chương.
                chapter_report = novel_folder / "02_BAO_CAO_CHUONG.tsv"
                with chapter_report.open("w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(
                        f,
                        fieldnames=[
                            "chapter_no",
                            "chapter_title",
                            "episodes",
                            "first_episode",
                            "last_episode",
                            "chars_no_whitespace",
                            "chars_plain",
                        ],
                        delimiter="\t",
                    )
                    writer.writeheader()
                    writer.writerows(chapter_summary_rows)

                total_chars = sum(int(r["chars_no_whitespace"]) for r in manifest_rows)
                total_plain = sum(int(r["chars_plain"]) for r in manifest_rows)

                summary_lines = [
                    "BÁO CÁO TẢI TRUYỆN NHẬT",
                    "=" * 42,
                    f"Tác phẩm: {self.novel.title}",
                    f"Tác giả: {self.novel.author or '(không đọc được)'}",
                    f"Website: {self.novel.site}",
                    f"Mã truyện: {self.novel.ncode}",
                    f"URL: {self.novel.root_url}",
                    f"Số ký tự website báo (toàn tác phẩm): {self.novel.site_total_chars:,}" if self.novel.site_total_chars else "Số ký tự website báo: không có/không đọc được",
                    "",
                    f"Chương có episode đã tải: {len(chapters)}",
                    f"Episode đã tải: {len(manifest_rows)}",
                    f"Tổng ký tự本文 bỏ whitespace: {total_chars:,}",
                    f"Tổng ký tự TXT本文 có xuống dòng: {total_plain:,}",
                    f"前書き trong TXT: {'Có' if include_preface and self.novel.site.startswith('Narou') else 'Không/không áp dụng'}",
                    f"後書き trong TXT: {'Có' if include_afterword and self.novel.site.startswith('Narou') else 'Không/không áp dụng'}",
                    "",
                    "CHI TIẾT TỪNG CHƯƠNG",
                    "-" * 42,
                ]

                for row in chapter_summary_rows:
                    summary_lines.append(
                        f"Chương {int(row['chapter_no']):02d} | {row['chapter_title']} | "
                        f"{row['episodes']} episode ({row['first_episode']}–{row['last_episode']}) | "
                        f"{int(row['chars_no_whitespace']):,} ký tự本文"
                    )

                summary_lines += [
                    "",
                    "Ghi chú:",
                    "- 'Ký tự本文 bỏ whitespace' chỉ đếm phần thân truyện và bỏ khoảng trắng/xuống dòng.",
                    "- Furigana <rt>/<rp> được bỏ, chỉ giữ chữ gốc.",
                    "- Số đếm có thể chênh nhẹ so với số hiển thị nội bộ của website vì mỗi trang có quy tắc đếm riêng.",
                ]

                atomic_write_text(novel_folder / "00_THONG_TIN_VA_TONG_KET.txt", "\n".join(summary_lines) + "\n")

                # Local integrity gate; this verifies selected episode coverage, NOT remote source completeness.
                if self.novel.site_episode_count and self.novel.episode_count != self.novel.site_episode_count:
                    raise FetchError("QA FAIL: số episode phát hiện không khớp con số website công bố.")
                indices_written = [int(row["episode"]) for row in manifest_rows]
                indices_expected = [int(ep.index) for ep in selected_eps]
                urls_written = [str(row["url"]) for row in manifest_rows]
                if indices_written != indices_expected or len(set(urls_written)) != len(urls_written):
                    raise FetchError("QA FAIL: thứ tự episode/URL thiếu hoặc trùng. Kết quả được đánh dấu chưa hoàn chỉnh.")
                qa_report = {
                    "app_version": APP_VERSION,
                    "status": "LOCAL_EXPORT_PASS_REMOTE_COMPLETENESS_UNVERIFIED",
                    "novel_id": self.novel.ncode,
                    "source_url": self.novel.root_url,
                    "expected_selected": len(selected_eps),
                    "written": len(manifest_rows),
                    "episode_indices": indices_written,
                    "source_toc_count": self.novel.site_episode_count or None,
                    "toc_count_detected": self.novel.episode_count,
                    "toc_count_match": (self.novel.episode_count == self.novel.site_episode_count) if self.novel.site_episode_count else None,
                    "episodes": manifest_rows,
                    "warning": "Hash kiểm tra dữ liệu TXT/cache cục bộ; chưa chứng minh nội dung bằng 100% trang gốc.",
                }
                atomic_write_text(novel_folder / "03_QA_KIEM_TRA_XUAT.json", json.dumps(qa_report, ensure_ascii=False, indent=2), encoding="utf-8")
                (novel_folder / "_TRANG_THAI_DANG_TAI.txt").unlink(missing_ok=True)
                atomic_write_text(novel_folder / "_TRANG_THAI_HOAN_TAT.txt", "LOCAL EXPORT PASS. Chưa đối chiếu được nguồn gốc 100%.\n")

                self.last_result_folder = novel_folder
                self.log(f"HOÀN TẤT: {novel_folder}")
                self.log(f"Tổng: {len(manifest_rows)} episode | {total_chars:,} ký tự本文")
                self.ui(self.btn_open.configure, state="normal")
                self.ui(
                    self.status_var.set,
                    f"Hoàn tất — {len(manifest_rows)} episode — {len(chapters)} chương liên quan — {total_chars:,} ký tự本文",
                )
                self.ui(
                    messagebox.showinfo,
                    APP_NAME,
                    f"Đã tải và ghép TXT xong.\n\n"
                    f"Chương liên quan: {len(chapters)}\n"
                    f"Episode: {len(manifest_rows)}\n"
                    f"Ký tự本文: {total_chars:,}\n\n"
                    f"Thư mục:\n{novel_folder}",
                )
            except (StopRequested, AutoStopRequested):
                self.log("Đã dừng theo yêu cầu. File/cache đã tải trước đó vẫn được giữ.")
                self.ui(self.status_var.set, "Đã dừng. Có thể chạy lại và dùng cache để tiếp tục.")
            except Exception as e:
                self.log(f"LỖI: {e}")
                self.ui(messagebox.showerror, APP_NAME, str(e))
                self.ui(self.status_var.set, "Có lỗi khi tải")
            finally:
                try:
                    if 'client' in locals() and hasattr(client, "close"):
                        client.close()
                except Exception:
                    pass
                self.set_busy(False)

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()


if __name__ == "__main__":
    App().mainloop()
