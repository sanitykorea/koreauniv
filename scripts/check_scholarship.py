#!/usr/bin/env python3
"""Korea University scholarship notice watcher.

Fetches the scholarship notice board, diffs it against the last known
state, and regenerates a static site (docs/) with an HTML page and an
RSS feed so new announcements can be followed as soon as they appear.
"""
from __future__ import annotations

import hashlib
import json
import os
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

# Boards this watcher polls. Each gets its own id namespace (via "key", used
# as an id prefix so two boards can never collide on the same articleId) and
# an optional "keyword": when set, only titles containing that substring are
# kept - everything else on that board is fetched and parsed but discarded.
SOURCES = [
    {
        "key": "568",
        "name": "장학금 공지",
        "url": SOURCE_URL,
        "keyword": None,
    },
    {
        "key": "566",
        "name": "근로장학",
        "url": "https://www.korea.ac.kr/ko/566/subview.do",
        "keyword": "근로장학",
    },
]
PRIMARY_SOURCE_NAME = SOURCES[0]["name"]

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "seen.json"
SITE_DIR = ROOT / "docs"
INDEX_FILE = SITE_DIR / "index.html"
FEED_FILE = SITE_DIR / "feed.xml"

MAX_ITEMS_KEPT = 200
NEW_BADGE_HOURS = 48
MIN_TITLE_LEN = 4
MAX_STRUCTURED_ROWS = 80
PAGES_PER_RUN = 3
PAGE_QUERY_PARAM = "page"
TELEGRAM_GROUP_THRESHOLD = 8
DATE_RE = re.compile(r"(20\d{2})[.\-/년]\s?(\d{1,2})[.\-/월]\s?(\d{1,2})")
SKIP_TITLES = {"다음", "이전", "처음", "마지막", "목록", "검색", "more", "list"}

# Notices on this board are themselves prefixed with a bracketed category,
# e.g. "[국가근로] ..." or "[교외-9/22] ...". Pulling that out lets the
# rendered list use the board's own taxonomy as a tag instead of an
# invented one.
CATEGORY_RE = re.compile(r"^\[([^\]]{1,12})\]\s*")

# korea.ac.kr renders this board with a "portalBoard" widget: every title
# anchor shares the same dummy href="#1" and real navigation happens via
# onclick="jf_view('<articleId>','<boardId>','<siteId>')" submitting a
# hidden form. There is no plain GET URL for a single article, so hrefs
# can't be used to identify or de-dup rows here - the articleId (or the
# displayed board number as a fallback) has to be used instead.
ONCLICK_ARTICLE_ID_RE = re.compile(r"jf_view\(\s*'([^']+)'")

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


def fetch_html(url: str, page: int | None = None) -> str:
    params = {PAGE_QUERY_PARAM: page} if page and page > 1 else None
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def fetch_additional_pages(base_url: str, already_seen_ids: set[str], id_prefix: str) -> list[dict]:
    """Fetch a few more board pages beyond page 1 so a run doesn't miss
    notices when more than one page's worth appear between checks.

    Best-effort: if the site doesn't honor ?page=N the way expected (e.g.
    it needs the widget's POST-based paging instead), the extra request(s)
    just return page 1's content again, which nets zero new ids and stops
    the loop immediately - so this degrades safely either way.
    """
    collected: list[dict] = []
    seen = set(already_seen_ids)
    for page in range(2, PAGES_PER_RUN + 1):
        try:
            page_html = fetch_html(base_url, page=page)
        except requests.RequestException as exc:
            print(f"[warn] page {page} fetch failed, stopping pagination: {exc}")
            break
        page_items = extract_portal_board_rows(BeautifulSoup(page_html, "lxml"), id_prefix)
        if not page_items:
            break
        new_on_page = [it for it in page_items if it["id"] not in seen]
        if not new_on_page:
            break
        for it in new_on_page:
            seen.add(it["id"])
            collected.append(it)
    return collected


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
    """Generic fallback: only useful when rows carry real, distinct hrefs."""
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
        out.append({"id": href, "href": href, "title": title, "posted_date": extract_date(row_text)})
    return out


def extract_portal_board_rows(soup: BeautifulSoup, id_prefix: str) -> list[dict] | None:
    """Row extraction for korea.ac.kr's portalBoard widget (table.board-table).

    Each row's real identity is the onclick articleId, not the href (see
    ONCLICK_ARTICLE_ID_RE above). id_prefix (the board's SOURCES "key")
    namespaces ids so the same articleId format on two different boards
    can never collide. Returns a list (possibly empty, e.g. past the last
    page) of dicts with a stable "id" plus title/date/board-number, or
    None if this page has no such table at all.
    """
    table = soup.select_one("table.board-table")
    if table is None:
        return None
    tbody = table.find("tbody") or table
    rows = tbody.find_all("tr")

    items = []
    seen_ids = set()
    for row in rows:
        title_cell = row.select_one("td.td-title") or row.select_one('td[class*="title"]')
        if title_cell is None:
            continue
        a = title_cell.find("a")
        if a is None:
            continue
        title = a.get_text(strip=True)
        if len(title) < MIN_TITLE_LEN:
            continue

        num_cell = row.select_one("td.td-num")
        board_no = num_cell.get_text(strip=True) if num_cell else None

        m = ONCLICK_ARTICLE_ID_RE.search(a.get("onclick", ""))
        article_id = m.group(1) if m else None

        item_id = article_id or board_no
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        date_cell = row.select_one("td.td-date")
        raw_date = date_cell.get_text(strip=True) if date_cell else ""
        posted_date = extract_date(raw_date) or (raw_date or None)

        items.append(
            {
                "id": f"korea-{id_prefix}-{item_id}",
                "title": title,
                "posted_date": posted_date,
                "board_no": board_no,
            }
        )

    return items


def parse_korea_portal_board(soup: BeautifulSoup, id_prefix: str):
    """Wraps extract_portal_board_rows with a sanity gate on row count, used
    to decide whether this page even looks like the expected notice board
    (see parse_structured). Not used for paging past page 1 - there a low
    or zero row count is an expected end-of-list signal, not a parse failure.
    """
    items = extract_portal_board_rows(soup, id_prefix)
    if items is not None and 3 <= len(items) <= MAX_STRUCTURED_ROWS:
        return items
    return None


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


def parse_structured(html: str, base_url: str, id_prefix: str):
    """Board parser with a site-specific fast path and a generic fallback.

    Tries the korea.ac.kr portalBoard markup first (see
    parse_korea_portal_board). If that doesn't match - e.g. the board
    widget changes, or this script gets pointed at a different page -
    falls back to a selector-light generic table/list parser that relies
    on real per-row hrefs. Returns None when nothing resembling a notice
    list is found, so the caller can fall back to whole-page change
    detection instead of guessing.
    """
    soup = BeautifulSoup(html, "lxml")

    portal_items = parse_korea_portal_board(soup, id_prefix)
    if portal_items is not None:
        return portal_items

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
        state = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        state.setdefault("sources", {})
        state.pop("page_hash", None)  # migrated to per-source state["sources"][key]["page_hash"]
        return state
    return {"initialized": False, "items": {}, "sources": {}}


def save_state(state: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def split_category(title: str) -> tuple[str | None, str]:
    """Split a leading "[카테고리]" tag off a title, if the board put one there."""
    m = CATEGORY_RE.match(title)
    if not m:
        return None, title
    rest = title[m.end():].strip()
    return m.group(1), (rest or title)


def render_index(items: list[dict], mode: str, last_checked: str) -> str:
    def new_badge(item):
        if not item.get("is_new"):
            return ""
        try:
            seen_dt = datetime.fromisoformat(item["first_seen"])
        except ValueError:
            return ""
        age_h = (datetime.now(timezone.utc) - seen_dt).total_seconds() / 3600
        return '<span class="new">NEW</span>' if age_h <= NEW_BADGE_HOURS else ""

    def render_entry(it):
        category, display_title = split_category(it["title"])
        source = it.get("source")
        if not category and source and source != PRIMARY_SOURCE_NAME:
            category = source
        no_html = f'<span class="no">No.{escape(it["board_no"])}</span>' if it.get("board_no") else ""
        tag_html = f'<span class="tag">{escape(category)}</span>' if category else ""
        date_html = f'<span>{escape(it["posted_date"])}</span>' if it.get("posted_date") else ""
        link = it.get("href") or it.get("source_url") or SOURCE_URL
        return f"""<li class="entry">
  <div class="entry-top">{no_html}{tag_html}{new_badge(it)}</div>
  <a class="title" href="{escape(link)}" target="_blank" rel="noopener">{escape(display_title)}</a>
  <div class="entry-bottom">{date_html}<span class="seen">감지 {escape(it["first_seen"][:10])}</span></div>
</li>"""

    rows = "\n".join(render_entry(it) for it in items)
    mode_note = (
        ""
        if mode == "structured"
        else (
            '<p class="warn">⚠️ 목록 구조를 자동으로 인식하지 못해 전체 페이지 변경 '
            "여부만 추적 중입니다. scripts/check_scholarship.py의 파싱 로직을 "
            "점검해주세요.</p>"
        )
    )
    count_label = f"{len(items)}건 추적 중" if items else "아직 추적 중인 공지가 없습니다"
    body = f"<ul class=\"ledger\">{rows}</ul>" if items else '<p class="empty">첫 확인을 기다리는 중입니다.</p>'
    source_links = "".join(
        f'<span class="sep">·</span><a href="{escape(s["url"])}" target="_blank" rel="noopener">{escape(s["name"])} 게시판</a>'
        for s in SOURCES
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SOURCE_NAME} 알리미</title>
<link rel="alternate" type="application/rss+xml" title="{SOURCE_NAME}" href="feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --crimson: #8b0029;
    --crimson-deep: #5c0016;
    --ivory: #d6cabc;
    --ivory-deep: #b7a68e;
    --paper: #fbf8f4;
    --ink: #241d17;
    --ink-soft: #7d7266;
    --line: #e6dfd4;
    color-scheme: light;
  }}
  * {{ box-sizing: border-box; }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
  body {{
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: "Noto Sans KR", -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    line-height: 1.6;
    word-break: keep-all;
    overflow-wrap: break-word;
  }}
  .top-bar {{ height: 6px; background: var(--crimson); }}
  header {{ max-width: 640px; margin: 0 auto; padding: 40px 24px 26px; }}
  .eyebrow {{
    margin: 0 0 12px;
    font-size: .72rem;
    font-weight: 700;
    letter-spacing: .16em;
    color: var(--crimson-deep);
    text-transform: uppercase;
  }}
  h1 {{
    margin: 0 0 12px;
    font-family: "Noto Serif KR", Georgia, serif;
    font-weight: 900;
    font-size: 2rem;
    text-wrap: balance;
  }}
  .desc {{ margin: 0 0 18px; max-width: 480px; font-size: .93rem; color: var(--ink-soft); }}
  .quicklinks {{ margin: 0; font-size: .86rem; }}
  .quicklinks a {{
    color: var(--crimson-deep);
    font-weight: 700;
    text-decoration: none;
    border-bottom: 1px solid var(--ivory-deep);
    padding-bottom: 1px;
  }}
  .quicklinks a:hover {{ border-color: var(--crimson); }}
  .quicklinks .sep {{ margin: 0 10px; color: var(--ivory-deep); }}
  main {{ max-width: 640px; margin: 0 auto; padding: 0 24px 40px; }}
  .status {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
    font-size: .78rem;
    color: var(--ink-soft);
    padding-bottom: 10px;
    margin-bottom: 4px;
    border-bottom: 2px solid var(--ivory);
    font-variant-numeric: tabular-nums;
  }}
  .status strong {{ color: var(--crimson-deep); font-weight: 800; }}
  .warn {{
    background: #fdf3d8;
    border: 1px solid #eccf7a;
    color: #6b5200;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: .85rem;
    margin: 16px 0 0;
  }}
  ul.ledger {{ list-style: none; margin: 0; padding: 0; }}
  .entry {{
    padding: 16px 2px;
    border-bottom: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    gap: 7px;
  }}
  .entry:hover {{ background: linear-gradient(to right, rgba(139,0,41,.035), transparent 60%); }}
  .entry-top {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .no {{
    font-size: .7rem;
    font-weight: 700;
    color: #fff;
    background: var(--crimson);
    padding: 2px 7px;
    border-radius: 4px;
    font-variant-numeric: tabular-nums;
  }}
  .tag {{
    font-size: .7rem;
    font-weight: 700;
    color: var(--crimson-deep);
    background: var(--ivory);
    padding: 2px 9px;
    border-radius: 4px;
  }}
  .new {{
    font-size: .66rem;
    font-weight: 800;
    letter-spacing: .06em;
    color: var(--crimson);
    border: 1.5px solid var(--crimson);
    padding: 1px 7px;
    border-radius: 4px;
  }}
  .title {{
    font-family: "Noto Serif KR", Georgia, serif;
    font-weight: 700;
    font-size: 1.02rem;
    color: var(--ink);
    text-decoration: none;
    line-height: 1.45;
  }}
  .title:hover {{ color: var(--crimson-deep); text-decoration: underline; text-underline-offset: 3px; }}
  .entry-bottom {{
    font-size: .78rem;
    color: var(--ink-soft);
    display: flex;
    gap: 10px;
    font-variant-numeric: tabular-nums;
  }}
  .entry-bottom .seen::before {{ content: "· "; }}
  .empty {{
    text-align: center;
    padding: 48px 16px;
    font-family: "Noto Serif KR", Georgia, serif;
    font-style: italic;
    color: var(--ink-soft);
    font-size: 1rem;
  }}
  footer {{
    max-width: 640px;
    margin: 0 auto;
    padding: 28px 24px 40px;
    border-top: 1px solid var(--line);
    font-size: .74rem;
    color: var(--ink-soft);
  }}
  footer a {{ color: var(--crimson-deep); }}
</style>
</head>
<body>
<div class="top-bar"></div>
<header>
  <p class="eyebrow">Korea University · 학생지원팀 공지</p>
  <h1>{SOURCE_NAME} 알리미</h1>
  <p class="desc">장학금 공지사항 게시판과 '근로장학' 키워드가 포함된 일반 공지를 주기적으로
  확인해서 새 글이 올라오면 이 페이지와 RSS 피드가 자동으로 갱신됩니다.</p>
  <p class="quicklinks">
    <a href="feed.xml">RSS 구독</a>{source_links}
  </p>
</header>
<main>
  <div class="status">
    <span><strong>{escape(count_label)}</strong></span>
    <span>마지막 확인 {escape(last_checked[:16].replace("T", " "))} UTC</span>
  </div>
  {mode_note}
  {body}
</main>
<footer>
  본 페이지는 공식 고려대학교 사이트가 아니며, korea.ac.kr의 공지 게시판을 비공식으로
  감시해 알려주는 도구입니다.
</footer>
</body>
</html>
"""


def render_feed(items: list[dict], last_checked: str) -> str:
    def entry(it):
        link = it.get("href") or it.get("source_url") or SOURCE_URL
        return f"""  <item>
    <title>{escape(it["title"])}</title>
    <link>{escape(link)}</link>
    <guid isPermaLink="false">{escape(it["id"])}</guid>
    <pubDate>{escape(it["first_seen"])}</pubDate>
  </item>"""

    items_xml = "\n".join(entry(it) for it in items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{SOURCE_NAME} 알리미</title>
  <link>{SITE_BASE_URL}/</link>
  <description>고려대학교 장학금 공지 + '근로장학' 키워드 공지 새 글 알림</description>
  <lastBuildDate>{escape(last_checked)}</lastBuildDate>
{items_xml}
</channel>
</rss>
"""


def _send_telegram_message(token: str, chat_id: str, text: str) -> None:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] Telegram 알림 전송 실패: {exc}", file=sys.stderr)


def _telegram_entry_line(it: dict) -> str:
    category, title = split_category(it["title"])
    if not category and it.get("source") and it["source"] != PRIMARY_SOURCE_NAME:
        category = it["source"]
    prefix = f"[{escape(category)}] " if category else ""
    return f"• {prefix}{escape(title)}"


def notify_telegram(new_items: list[dict]) -> None:
    """Post newly-detected notices to a Telegram chat/channel, if configured.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment (set
    as GitHub Actions secrets) - silently does nothing when either is
    missing, so this stays fully optional. A single notification failure
    is logged, never raised, since it must not block state/site updates
    that already happened before this runs.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id or not new_items:
        return

    if len(new_items) > TELEGRAM_GROUP_THRESHOLD:
        lines = [f"🎓 <b>새 장학금 공지 {len(new_items)}건</b>", ""]
        lines.extend(_telegram_entry_line(it) for it in new_items)
        lines.append("")
        overview_link = new_items[0].get("source_url") or SOURCE_URL
        lines.append(f'<a href="{escape(overview_link)}">전체 공지 게시판 보기</a>')
        _send_telegram_message(token, chat_id, "\n".join(lines))
        return

    for it in new_items:
        category, title = split_category(it["title"])
        if not category and it.get("source") and it["source"] != PRIMARY_SOURCE_NAME:
            category = it["source"]
        lines = ["🎓 <b>새 장학금 공지</b>", ""]
        lines.append(f"[{escape(category)}] {escape(title)}" if category else escape(title))
        meta = [p for p in (it.get("posted_date"), f"No.{it['board_no']}" if it.get("board_no") else None) if p]
        if meta:
            lines.append(" · ".join(escape(p) for p in meta))
        link = it.get("href") or it.get("source_url") or SOURCE_URL
        lines.append(f'<a href="{escape(link)}">공지 확인하기</a>')
        _send_telegram_message(token, chat_id, "\n".join(lines))


def process_source(source: dict, was_initialized: bool, state_sources: dict) -> tuple[list[dict], bool]:
    """Fetch, paginate, and (if configured) keyword-filter one board.

    Returns (items, structure_recognized). items is [] when the fetch
    failed or the board's structure wasn't recognized on this run; the
    latter also updates state_sources[source["key"]] for hash-diff
    fallback tracking and prints diagnostics to the log.
    """
    try:
        html = fetch_html(source["url"])
    except requests.RequestException as exc:
        print(f"[error] [{source['name']}] 가져오기 실패: {exc}", file=sys.stderr)
        return [], True  # not a structure problem, just skip this run

    parsed = parse_structured(html, source["url"], source["key"])
    src_state = state_sources.setdefault(source["key"], {"page_hash": None})

    if parsed is None:
        print(f"[warn] [{source['name']}] 목록 구조를 인식하지 못했습니다.")
        print_diagnostics(html)
        digest = hashlib.sha256(
            BeautifulSoup(html, "lxml").get_text(" ", strip=True).encode("utf-8")
        ).hexdigest()
        if was_initialized and src_state.get("page_hash") and src_state["page_hash"] != digest:
            src_state["changed"] = True
        src_state["page_hash"] = digest
        return [], False

    if PAGES_PER_RUN > 1:
        extra = fetch_additional_pages(source["url"], {it["id"] for it in parsed}, source["key"])
        if extra:
            print(f"[info] [{source['name']}] 다음 페이지에서 {len(extra)}건을 추가로 확인했습니다.")
        parsed = parsed + extra

    if source.get("keyword"):
        before = len(parsed)
        parsed = [it for it in parsed if source["keyword"] in it["title"]]
        print(f"[info] [{source['name']}] {before}건 중 '{source['keyword']}' 포함 {len(parsed)}건 선별")

    for it in parsed:
        it["source"] = source["name"]
        it["source_url"] = source["url"]

    return parsed, True


def main() -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state()
    was_initialized = state.get("initialized", False)
    state_sources = state["sources"]

    items_state: dict = state.get("items", {})
    new_items: list[dict] = []
    any_structured = False
    any_recognized = False

    for source in SOURCES:
        parsed, recognized = process_source(source, was_initialized, state_sources)
        any_recognized = any_recognized or recognized
        if not parsed:
            continue
        any_structured = True

        for entry in parsed:
            item_id = entry["id"]
            if item_id in items_state:
                items_state[item_id]["last_seen"] = now
                if entry.get("posted_date") and not items_state[item_id].get("posted_date"):
                    items_state[item_id]["posted_date"] = entry["posted_date"]
            else:
                new_item = {
                    "id": item_id,
                    "href": entry.get("href"),
                    "board_no": entry.get("board_no"),
                    "title": entry["title"],
                    "posted_date": entry.get("posted_date"),
                    "source": entry.get("source"),
                    "source_url": entry.get("source_url"),
                    "first_seen": now,
                    "last_seen": now,
                    # Only items discovered after the initial baseline load
                    # should ever show the NEW badge - otherwise every
                    # pre-existing notice would look "new" on first deploy.
                    "is_new": was_initialized,
                }
                items_state[item_id] = new_item
                if was_initialized:
                    new_items.append(new_item)

    if not any_recognized:
        print("[error] 모든 게시판을 가져오지 못했습니다.", file=sys.stderr)
        return 1

    mode = "structured" if any_structured else "fallback-hash"
    fallback_sources = [s["name"] for s in SOURCES if state_sources.get(s["key"], {}).pop("changed", False)]

    def sort_key(it):
        board_no = it.get("board_no")
        board_no_num = int(board_no) if board_no and board_no.isdigit() else -1
        return (it.get("posted_date") or "", board_no_num, it["first_seen"])

    all_items = sorted(items_state.values(), key=sort_key, reverse=True)
    all_items = all_items[:MAX_ITEMS_KEPT]
    items_state = {it["id"]: it for it in all_items}

    state["items"] = items_state
    state["sources"] = state_sources
    state["initialized"] = True
    state["last_checked"] = now
    state["mode"] = mode
    save_state(state)

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(render_index(all_items, mode, now), encoding="utf-8")
    FEED_FILE.write_text(render_feed(all_items[:50], now), encoding="utf-8")

    if new_items:
        print(f"[info] {len(new_items)}건의 새 공지를 감지했습니다:")
        for it in new_items:
            print(f"  - [{it.get('source')}] {it['title']}")
        notify_telegram(new_items)
    if fallback_sources:
        names = ", ".join(fallback_sources)
        print(f"[info] 전체 페이지 변경 감지(구조화 파싱 실패): {names}")
        token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            _send_telegram_message(
                token,
                chat_id,
                f"⚠️ {escape(names)} 게시판에 변화가 감지됐지만 자동 인식에 실패했습니다.\n직접 확인해주세요.",
            )
    if not new_items and not fallback_sources:
        if was_initialized:
            print("[info] 새 공지가 없습니다.")
        else:
            print(f"[info] 초기 데이터를 저장했습니다 ({mode} 모드, {len(all_items)}건).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
