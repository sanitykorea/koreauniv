#!/usr/bin/env python3
"""Korea University scholarship notice watcher.

Fetches the scholarship notice board, diffs it against the last known
state, and regenerates a static site (docs/) with an HTML page and an
RSS feed so new announcements can be followed as soon as they appear.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.korea.ac.kr/ko/568/subview.do"
SOURCE_NAME = "고려대학교 장학금 공지"
SITE_BASE_URL = "https://sanitykorea.github.io/koreauniv"

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "seen.json"
SITE_DIR = ROOT / "docs"
INDEX_FILE = SITE_DIR / "index.html"
FEED_FILE = SITE_DIR / "feed.xml"

MAX_ITEMS_KEPT = 200
NEW_BADGE_HOURS = 48
MIN_TITLE_LEN = 4
MAX_STRUCTURED_ROWS = 80
DATE_RE = re.compile(r"(20\d{2})[.\-/년]\s?(\d{1,2})[.\-/월]\s?(\d{1,2})")
SKIP_TITLES = {"다음", "이전", "처음", "마지막", "목록", "검색", "more", "list"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# Common containers for the main content area across Korean university/gov CMS themes.
CONTAINER_SELECTORS = [
    "main", "#container", "#content", ".content", "article",
    ".board-list", ".bbs-list", ".artclList", "#board_list",
]


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def extract_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def pick_title_anchor(el):
    anchors = [a for a in el.find_all("a", href=True) if a.get_text(strip=True)]
    if not anchors:
        return None
    return max(anchors, key=lambda a: len(a.get_text(strip=True)))


def rows_to_items(rows, base_url: str):
    out = []
    seen_href = set()
    for row in rows:
        a = pick_title_anchor(row)
        if a is None:
            continue
        title = a.get_text(strip=True)
        if len(title) < MIN_TITLE_LEN or title.lower() in SKIP_TITLES or title.isdigit():
            continue
        href = a["href"].strip()
        if not href or href.startswith("javascript:") or href == "#":
            continue
        href = urljoin(base_url, href)
        if href in seen_href:
            continue
        seen_href.add(href)
        row_text = row.get_text(" ", strip=True)
        out.append((href, title, extract_date(row_text)))
    return out


def find_scope(soup: BeautifulSoup):
    for sel in CONTAINER_SELECTORS:
        found = soup.select(sel)
        if found:
            return found[0]
    return soup


def print_diagnostics(html: str) -> None:
    """Dump enough of the fetched page into the workflow log to redesign the
    parser without needing direct access to the live site."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else "(no <title>)"
    print(f"[debug] page title: {title}")
    print(f"[debug] html length: {len(html)} chars")
    for sel in CONTAINER_SELECTORS:
        print(f"[debug] container selector {sel!r}: {len(soup.select(sel))} match(es)")

    scope = find_scope(soup)
    scope_desc = getattr(scope, "name", None) or "document-root"
    print(f"[debug] scope used for search: <{scope_desc}>")
    for tag in ("table", "tbody", "tr", "ul", "ol", "li", "a"):
        print(f"[debug] scope count <{tag}>: {len(scope.find_all(tag))}")

    snippet = scope.prettify()
    print(f"[debug] ---- scope HTML snippet (first 6000 of {len(snippet)} chars) ----")
    print(snippet[:6000])
    print("[debug] ---- end snippet ----")


def parse_structured(html: str, base_url: str):
    """Best-effort, selector-light board parser.

    Tries table rows first, then list items, scoped to the main content
    area when one of the common container selectors matches. Returns None
    when nothing resembling a notice list is found, so the caller can fall
    back to whole-page change detection instead of guessing.
    """
    soup = BeautifulSoup(html, "lxml")
    scope = find_scope(soup)

    for tag in ("tr", "li"):
        rows = scope.find_all(tag)
        rows = [r for r in rows if r.find("a", href=True)]
        if len(rows) < 3:
            continue
        items = rows_to_items(rows, base_url)
        if 3 <= len(items) <= MAX_STRUCTURED_ROWS:
            return items
    return None


def load_state() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"initialized": False, "page_hash": None, "items": {}}


def save_state(state: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def render_index(items: list[dict], mode: str, last_checked: str) -> str:
    def badge(item):
        try:
            seen_dt = datetime.fromisoformat(item["first_seen"])
        except ValueError:
            return ""
        age_h = (datetime.now(timezone.utc) - seen_dt).total_seconds() / 3600
        return ' <span class="new">NEW</span>' if age_h <= NEW_BADGE_HOURS else ""

    rows = "\n".join(
        f'<li class="item"><a href="{escape(it["href"])}" target="_blank" '
        f'rel="noopener">{escape(it["title"])}</a>{badge(it)}'
        f'<div class="meta">{escape(it.get("posted_date") or "")} · 감지일 '
        f'{escape(it["first_seen"][:10])}</div></li>'
        for it in items
    )
    mode_note = (
        ""
        if mode == "structured"
        else (
            '<p class="warn">⚠️ 목록 구조를 자동으로 인식하지 못해 전체 페이지 변경 '
            "여부만 추적 중입니다. scripts/check_scholarship.py의 파싱 로직을 "
            "점검해주세요.</p>"
        )
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SOURCE_NAME} 알리미</title>
<link rel="alternate" type="application/rss+xml" title="{SOURCE_NAME}" href="feed.xml">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    max-width: 720px; margin: 0 auto; padding: 24px 16px; line-height: 1.5; }}
  h1 {{ font-size: 1.3rem; }}
  h1 a {{ color: inherit; }}
  .sub {{ color: #666; font-size: .85rem; margin-bottom: 24px; }}
  ul {{ list-style: none; padding: 0; }}
  .item {{ padding: 14px 0; border-bottom: 1px solid #e5e5e5; }}
  .item a {{ font-weight: 600; text-decoration: none; }}
  .item a:hover {{ text-decoration: underline; }}
  .meta {{ font-size: .8rem; color: #888; margin-top: 4px; }}
  .new {{ background: #d6294c; color: #fff; font-size: .65rem; padding: 2px 6px;
    border-radius: 999px; margin-left: 6px; vertical-align: middle; }}
  .warn {{ background: #fff3cd; color: #6b5200; padding: 10px 12px; border-radius: 8px;
    font-size: .85rem; }}
  footer {{ margin-top: 32px; font-size: .75rem; color: #999; }}
  footer a {{ color: inherit; }}
</style>
</head>
<body>
<h1><a href="{SOURCE_URL}" target="_blank" rel="noopener">{SOURCE_NAME}</a> 알리미</h1>
<p class="sub">고려대학교 장학금 공지사항 페이지를 주기적으로 확인해 새 글이 올라오면
이 페이지와 <a href="feed.xml">RSS 피드</a>가 업데이트됩니다.</p>
{mode_note}
<ul>
{rows}
</ul>
<footer>마지막 확인: {escape(last_checked)} (UTC) · 원본:
<a href="{SOURCE_URL}" target="_blank" rel="noopener">korea.ac.kr</a></footer>
</body>
</html>
"""


def render_feed(items: list[dict], last_checked: str) -> str:
    def entry(it):
        return f"""  <item>
    <title>{escape(it["title"])}</title>
    <link>{escape(it["href"])}</link>
    <guid isPermaLink="true">{escape(it["href"])}</guid>
    <pubDate>{escape(it["first_seen"])}</pubDate>
  </item>"""

    items_xml = "\n".join(entry(it) for it in items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{SOURCE_NAME} 알리미</title>
  <link>{SITE_BASE_URL}/</link>
  <description>고려대학교 장학금 공지사항 새 글 알림</description>
  <lastBuildDate>{escape(last_checked)}</lastBuildDate>
{items_xml}
</channel>
</rss>
"""


def main() -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state()
    was_initialized = state.get("initialized", False)

    try:
        html = fetch_html(SOURCE_URL)
    except requests.RequestException as exc:
        print(f"[error] failed to fetch source: {exc}", file=sys.stderr)
        return 1

    parsed = parse_structured(html, SOURCE_URL)
    mode = "structured" if parsed is not None else "fallback-hash"
    if parsed is None:
        print_diagnostics(html)

    items_state: dict = state.get("items", {})
    new_titles: list[str] = []

    if parsed is not None:
        for href, title, posted_date in parsed:
            if href in items_state:
                items_state[href]["last_seen"] = now
                if posted_date and not items_state[href].get("posted_date"):
                    items_state[href]["posted_date"] = posted_date
            else:
                items_state[href] = {
                    "href": href,
                    "title": title,
                    "posted_date": posted_date,
                    "first_seen": now,
                    "last_seen": now,
                }
                if was_initialized:
                    new_titles.append(title)
    else:
        digest = hashlib.sha256(
            BeautifulSoup(html, "lxml").get_text(" ", strip=True).encode("utf-8")
        ).hexdigest()
        if was_initialized and state.get("page_hash") and state["page_hash"] != digest:
            new_titles.append("(전체 페이지 변경 감지 - 구조화 파싱 실패)")
        state["page_hash"] = digest

    all_items = sorted(items_state.values(), key=lambda it: it["first_seen"], reverse=True)
    all_items = all_items[:MAX_ITEMS_KEPT]
    items_state = {it["href"]: it for it in all_items}

    state["items"] = items_state
    state["initialized"] = True
    state["last_checked"] = now
    state["mode"] = mode
    save_state(state)

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(render_index(all_items, mode, now), encoding="utf-8")
    FEED_FILE.write_text(render_feed(all_items[:50], now), encoding="utf-8")

    if new_titles:
        print(f"[info] {len(new_titles)}건의 새 공지를 감지했습니다:")
        for t in new_titles:
            print(f"  - {t}")
    elif was_initialized:
        print("[info] 새 공지가 없습니다.")
    else:
        print(f"[info] 초기 데이터를 저장했습니다 ({mode} 모드, {len(all_items)}건).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
