"""Year-in-books stats pipeline.

Fetches the Goodreads read-shelf RSS, windows to the last 12 months,
looks up genres via Google Books (and falls back to Goodreads page
scrape when needed), renders an editorial HTML report via Jinja2, then
prints/screenshots the HTML to PDF, web PNG, and 9:16 social card PNG via
headless Chromium (Playwright). The HTML is the single source of truth
for design — there's no separate chart-rendering pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config_loader import load_config


# -------- constants --------

GOODREADS_RSS_URL = "https://www.goodreads.com/review/list_rss/{user_id}?shelf=read"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
USER_AGENT = "Mozilla/5.0 (compatible; goodreads-tools/1.0)"

# BISAC top-level categories that are too generic to be useful as a genre bucket.
# When Google Books returns ONLY one of these for a book (no subcategory), we
# treat that book as having no genre data rather than dumping it into a giant
# "Fiction" bar that swamps the chart.
GENERIC_TOP_LEVELS = frozenset({
    "Fiction", "Nonfiction", "Non-Fiction",
    "Juvenile Fiction", "Juvenile Nonfiction",
    "Young Adult Fiction", "Young Adult Nonfiction",
})

# Tags that show up in Goodreads' shelf data but aren't genres — they're
# format, age category, or shelf-management labels. Compared lowercase.
GENRE_STOPWORDS = frozenset({
    # Format / medium
    "audiobook", "audio book", "audio", "audiobooks",
    "ebook", "e-book", "e book", "ebooks", "kindle", "print", "books",
    # Age category
    "adult", "adults", "young adult", "ya", "new adult",
    "middle grade", "middle-grade", "middle grades",
    "children", "childrens", "children's", "kids", "kid", "picture books",
    # Status / shelf-management
    "read", "reading", "currently reading", "currently-reading",
    "want to read", "want-to-read", "to-read", "to read",
    "owned", "owned-books", "favorites", "favourites", "default",
    "reread", "re-read", "rereads", "did not finish", "dnf",
    "library", "borrowed", "wishlist", "to-buy",
    # Generic top-levels (also filtered by GENERIC_TOP_LEVELS)
    "fiction", "nonfiction", "non-fiction", "non fiction",
})

# -------- data classes --------

@dataclass
class Book:
    title: str
    author: str
    isbn: Optional[str]
    num_pages: Optional[int]
    user_read_at: datetime
    user_rating: Optional[int]
    goodreads_book_id: Optional[str] = None


@dataclass
class Stats:
    today: datetime
    window_start: datetime
    window_end: datetime
    total_books: int
    total_pages: int
    books_missing_pages: int
    books_per_month: list  # [(label, count), ...] oldest first
    pages_per_month: list
    book_titles: list      # [(date, title, author), ...] reverse-chronological
    books: list            # in-window Book objects
    first_name: Optional[str] = None  # for personalizing the title line


# -------- fetch --------

def fetch_read_shelf(user_id: str, timeout: int = 30) -> tuple:
    """Return (books, first_name). first_name is parsed from the RSS channel
    title (e.g., 'Michael\\'s bookshelf: read' -> 'Michael') and is None if
    the channel title doesn't match the expected pattern."""
    url = GOODREADS_RSS_URL.format(user_id=user_id)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml-xml")

    first_name = None
    channel = soup.find("channel")
    if channel is not None:
        # Direct child <title> only — items also have <title> elements.
        for t in channel.find_all("title", recursive=False):
            first_name = _parse_first_name(t.text or "")
            break

    items = soup.find_all("item")
    books = []
    skipped_no_date: list = []
    for item in items:
        try:
            books.append(_parse_item(item))
        except _NoReadDate as e:
            skipped_no_date.append(e.title)
        except Exception as e:
            logging.warning("Skipping unparseable RSS item: %s", e)
    if skipped_no_date:
        logging.warning(
            "Skipped %d book(s) with no read date set on Goodreads; "
            "set a finish date on each to include them: %s",
            len(skipped_no_date),
            ", ".join(repr(t) for t in skipped_no_date),
        )
    return books, first_name


def _parse_first_name(channel_title: str) -> Optional[str]:
    """Goodreads channel titles look like 'Michael's bookshelf: read'.
    Pull everything before \"'s bookshelf\" and take the first whitespace-
    separated token as the first name. Returns None if the pattern doesn't
    match."""
    if not channel_title:
        return None
    idx = channel_title.find("'s bookshelf")
    if idx < 0:
        # Some locales use a unicode right-single-quote; try that too.
        idx = channel_title.find("’s bookshelf")
    if idx < 0:
        return None
    name_blob = channel_title[:idx].strip()
    if not name_blob:
        return None
    return name_blob.split()[0]


def _text(elem) -> str:
    return elem.text.strip() if elem and elem.text else ""


class _NoReadDate(Exception):
    """Raised when an RSS item has no user_read_at value.

    Surfaces the title so the caller can log which books were skipped — that's
    the actionable information for the user (set a read date on Goodreads)."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.title = title


def _parse_item(item) -> Book:
    title = _text(item.find("title"))
    author = _text(item.find("author_name"))

    isbn = _text(item.find("isbn")) or _text(item.find("isbn13")) or None

    num_pages_text = _text(item.find("num_pages"))
    num_pages = int(num_pages_text) if num_pages_text.isdigit() else None

    rating_text = _text(item.find("user_rating"))
    user_rating = int(rating_text) if rating_text.isdigit() and int(rating_text) > 0 else None

    # Use ONLY user_read_at — the date the user marked the book as finished.
    # Do NOT fall back to user_date_added: that's the date the entry was last
    # touched on the shelf (re-shelving, migrations, edits), which can attribute
    # a book read decades ago to a recent month and corrupt the windowed view.
    # Books without a read date are skipped so the user can set one on Goodreads.
    read_at_text = _text(item.find("user_read_at"))
    if not read_at_text:
        raise _NoReadDate(title or "<unknown title>")
    user_read_at = datetime.strptime(read_at_text, "%a, %d %b %Y %H:%M:%S %z").astimezone()

    # Pull the Goodreads book ID for the optional page-scrape genre fallback.
    book_id = _text(item.find("book_id")) or None
    if not book_id:
        book_elem = item.find("book")
        if book_elem is not None:
            attr_id = book_elem.get("id")
            if attr_id and attr_id.strip():
                book_id = attr_id.strip()

    return Book(
        title=title,
        author=author,
        isbn=isbn,
        num_pages=num_pages,
        user_read_at=user_read_at,
        user_rating=user_rating,
        goodreads_book_id=book_id,
    )


# -------- aggregate --------

def compute_highlights(stats: "Stats") -> dict:
    """Pull a few editorial 'numbers worth knowing' from the year's data.
    Each entry is (label, primary, detail). Missing data is omitted rather
    than shown as a placeholder. The three-part shape supports a label /
    big-value / small-caption render in each highlight column."""
    h: dict = {}

    # Peak reading month
    if stats.books_per_month:
        peak_label, peak_count = max(stats.books_per_month, key=lambda x: x[1])
        if peak_count > 0:
            month_name = peak_label.split()[0]
            plural = "books" if peak_count != 1 else "book"
            h["peak_month"] = ("Peak month", month_name, f"{peak_count} {plural}")

    # Most-read author
    by_author: dict = {}
    for b in stats.books:
        if b.author:
            by_author[b.author] = by_author.get(b.author, 0) + 1
    if by_author:
        top_author, count = max(by_author.items(), key=lambda kv: (kv[1], -len(kv[0])))
        if count >= 2:
            h["top_author"] = (
                "Most-read author",
                _truncate_label(top_author, 18),
                f"{count} books",
            )

    # Longest book + average pages
    with_pages = [b for b in stats.books if b.num_pages]
    if with_pages:
        longest = max(with_pages, key=lambda b: b.num_pages)
        h["longest"] = (
            "Longest book",
            _truncate_label(longest.title, 18),
            f"{longest.num_pages:,} pages",
        )
        avg = stats.total_pages // max(1, len(with_pages))
        h["avg_pages"] = ("Average length", f"{avg:,}", "pages per book")

    return h


def aggregate_last_12_months(
    books: list,
    today: Optional[datetime] = None,
    first_name: Optional[str] = None,
) -> Stats:
    if today is None:
        today = datetime.now().astimezone()
    window_start = today - timedelta(days=365)
    return aggregate_range(books, window_start, today, first_name=first_name)


def aggregate_range(
    books: list,
    window_start: datetime,
    window_end: datetime,
    first_name: Optional[str] = None,
) -> Stats:
    """Like aggregate_last_12_months, but for an arbitrary [window_start,
    window_end] range instead of always the trailing 365 days. Both bounds
    are inclusive."""
    in_window = [b for b in books if window_start <= b.user_read_at <= window_end]

    months = _month_buckets_range(window_start, window_end)
    bpm = {label: 0 for label in months}
    ppm = {label: 0 for label in months}

    total_pages = 0
    books_missing_pages = 0
    for b in in_window:
        label = b.user_read_at.strftime("%b %Y")
        if label not in bpm:
            continue
        bpm[label] += 1
        if b.num_pages:
            ppm[label] += b.num_pages
            total_pages += b.num_pages
        else:
            books_missing_pages += 1

    titles = sorted(
        [(b.user_read_at, b.title, b.author) for b in in_window],
        key=lambda x: x[0],
        reverse=True,
    )

    return Stats(
        today=window_end,
        window_start=window_start,
        window_end=window_end,
        total_books=len(in_window),
        total_pages=total_pages,
        books_missing_pages=books_missing_pages,
        books_per_month=[(label, bpm[label]) for label in months],
        pages_per_month=[(label, ppm[label]) for label in months],
        book_titles=titles,
        books=in_window,
        first_name=first_name,
    )


def _month_buckets_range(start: datetime, end: datetime) -> list:
    """Month labels ('%b %Y') from start's month through end's month,
    inclusive, oldest first."""
    labels = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        labels.append(datetime(year, month, 1).strftime("%b %Y"))
        if month == 12:
            month = 1
            year += 1
        else:
            month += 1
    return labels


# -------- genres --------

def lookup_genres(books: list, cache_path: Path, timeout: int = 10) -> dict:
    """Look up genre/category data for each book. Strategy:

    1. If the book has an ISBN, query Google Books by ISBN.
    2. If that returned nothing or only generic top-levels, try Google
       Books with a title+author query.
    3. If still nothing useful and we have a Goodreads book ID from the
       RSS, scrape the Goodreads book page for crowd-sourced genres.

    A cached entry that's empty or only-generic is treated as stale so a
    re-run benefits from the new fallback chain even if older Google-only
    cache data is on disk.
    """
    cache = _load_cache(cache_path)
    seen = set()
    for b in books:
        key = _book_key(b)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in cache and cache[key] and not _is_only_generic(cache[key]):
            continue
        try:
            cats: list = []
            if b.isbn:
                cats = _query_google_books_isbn(b.isbn, timeout=timeout)
            if (not cats) or _is_only_generic(cats):
                ta_cats = _query_google_books_title_author(
                    b.title, b.author, timeout=timeout
                )
                if ta_cats and not _is_only_generic(ta_cats):
                    cats = ta_cats
                elif not cats:
                    cats = ta_cats
            if (not cats or _is_only_generic(cats)) and b.goodreads_book_id:
                gr_cats = _query_goodreads_genres(b.goodreads_book_id, timeout=timeout)
                if gr_cats:
                    cats = gr_cats
            cache[key] = cats
        except Exception as e:
            logging.warning("Genre lookup failed for %s: %s", key, e)
            cache[key] = []
    _save_cache(cache_path, cache)
    return cache


def _is_only_generic(cats: list) -> bool:
    """True when every category in the list is a generic top-level like
    "Fiction" (no sub-tag, no informative non-fiction top). Empty list
    returns False so callers handle the "no data" case separately."""
    if not cats:
        return False
    for cat in cats:
        parts = [p.strip() for p in cat.split(" / ") if p.strip()]
        if len(parts) >= 2:
            return False
        if parts and parts[0] not in GENERIC_TOP_LEVELS:
            return False
    return True


def _book_key(book) -> str:
    if book.isbn:
        return book.isbn
    title = (book.title or "").strip().lower()
    author = (book.author or "").strip().lower()
    if not (title or author):
        return ""
    return f"ta:{title}|{author}"


def _query_google_books_isbn(isbn: str, timeout: int = 10) -> list:
    response = requests.get(
        GOOGLE_BOOKS_URL,
        params={"q": f"isbn:{isbn}"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    return _extract_categories_from_response(response)


def _query_google_books_title_author(title: str, author: str, timeout: int = 10) -> list:
    parts = []
    if title and title.strip():
        parts.append(f'intitle:"{title.strip()}"')
    if author and author.strip():
        parts.append(f'inauthor:"{author.strip()}"')
    if not parts:
        return []
    response = requests.get(
        GOOGLE_BOOKS_URL,
        params={"q": "+".join(parts), "maxResults": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    return _extract_categories_from_response(response)


def _extract_categories_from_response(response) -> list:
    if response.status_code != 200:
        return []
    data = response.json()
    if not data.get("items"):
        return []
    info = data["items"][0].get("volumeInfo", {})
    return list(info.get("categories") or [])


# Module-level rate gate so we don't hammer Goodreads with parallel requests
# from a fast-running render. Single-threaded today, but this keeps us
# polite if anything ever calls into here concurrently.
_GR_REQUEST_DELAY_SEC = 0.4
_GR_LAST_REQUEST_AT = 0.0


def _query_goodreads_genres(book_id: str, timeout: int = 15) -> list:
    """Scrape the Goodreads book page for crowd-sourced genre tags. Returns
    a list of genre name strings (e.g., ["Fantasy", "Epic Fantasy"]) or an
    empty list if the page can't be parsed."""
    import time as _time

    global _GR_LAST_REQUEST_AT
    delta = _time.time() - _GR_LAST_REQUEST_AT
    if delta < _GR_REQUEST_DELAY_SEC:
        _time.sleep(_GR_REQUEST_DELAY_SEC - delta)
    _GR_LAST_REQUEST_AT = _time.time()

    url = f"https://www.goodreads.com/book/show/{book_id}"
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        allow_redirects=True,
    )
    if response.status_code != 200:
        return []
    return _extract_goodreads_genres(response.text)


def _extract_goodreads_genres(html: str) -> list:
    """Two-pattern extractor for Goodreads' genre tags. Tries the classic
    HTML pattern first, then walks the React __NEXT_DATA__ JSON blob for
    Genre objects. Returns deduped names in insertion order."""
    import re as _re

    soup = BeautifulSoup(html, "html.parser")

    found: list = []
    seen: set = set()

    def add(name: str) -> None:
        n = (name or "").strip()
        if not n or n.lower() in {"...more", "more"} or n in seen:
            return
        seen.add(n)
        found.append(n)

    # Pattern 1: anchor links to /genres/* — works on classic and many
    # current Goodreads pages.
    for a in soup.find_all("a", href=_re.compile(r"^/genres/")):
        text = a.get_text(strip=True)
        if text and len(text) <= 40:
            add(text)

    if found:
        return found[:15]

    # Pattern 2: React __NEXT_DATA__ JSON blob. Genre objects look like
    # {"__typename": "Genre", "name": "Fantasy", ...} nested in the apollo
    # cache. Walk the whole blob and harvest names.
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script and next_script.string:
        try:
            blob = json.loads(next_script.string)
        except json.JSONDecodeError:
            blob = None
        if blob is not None:
            def visit(obj):
                if isinstance(obj, dict):
                    if obj.get("__typename") == "Genre" and obj.get("name"):
                        add(obj["name"])
                    for v in obj.values():
                        visit(v)
                elif isinstance(obj, list):
                    for v in obj:
                        visit(v)
            visit(blob)

    return found[:15]


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def aggregate_genres(books: list, genres_by_key: dict) -> tuple:
    """Return (top_items, uncategorized_count) for the genre chart.

    Bucketing rules:
    - Per book, walk its category list in order. Take the first 3 distinct
      genre tags (after filtering format/age/shelf-management stop-words
      and generic top-levels like "Fiction" alone). This stops Goodreads'
      noisier 10-15-tag results from inflating an "Other" bar.
    - Each kept tag contributes 1 to that bucket's count.
    - Show the top 10 buckets, sorted by count (ties alphabetical). No
      "Other" rollup — readers care about what they read, not the long
      tail.
    - If the strict pass returns nothing, retry once allowing generic
      top-levels so a book tagged only "Fiction" still appears.
    """
    counter, uncategorized = _bucket_genres(books, genres_by_key, allow_generic=False)
    if not counter:
        counter, uncategorized = _bucket_genres(books, genres_by_key, allow_generic=True)

    if not counter:
        return ([], uncategorized)

    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return (items[:8], uncategorized)


def _bucket_genres(
    books: list,
    genres_by_key: dict,
    *,
    allow_generic: bool,
    max_per_book: int = 3,
) -> tuple:
    counter: dict = {}
    uncategorized = 0
    for b in books:
        cats = genres_by_key.get(_book_key(b), [])
        book_buckets: list = []
        for cat in cats:
            parts = [p.strip() for p in cat.split(" / ") if p.strip()]
            if len(parts) >= 2:
                bucket = parts[1]
            elif parts:
                if allow_generic or parts[0] not in GENERIC_TOP_LEVELS:
                    bucket = parts[0]
                else:
                    continue
            else:
                continue
            if bucket.lower() in GENRE_STOPWORDS:
                continue
            if bucket in book_buckets:
                continue
            book_buckets.append(bucket)
            if len(book_buckets) >= max_per_book:
                break
        if not book_buckets:
            uncategorized += 1
            continue
        for bucket in book_buckets:
            counter[bucket] = counter.get(bucket, 0) + 1
    return counter, uncategorized


# -------- HTML report --------

def _split_title_series(title: str) -> tuple:
    """If a title ends with ' (...)' that looks like series metadata
    (contains a # number, comma, or words like 'series'/'cycle'/'trilogy'),
    split it into (main_title, series_text). Otherwise return (title, None)."""
    import re as _re
    m = _re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", title)
    if not m:
        return (title, None)
    inner = m.group(2)
    indicators = ("#", ",")
    words = ("series", "cycle", "trilogy", "volume", "book ", "saga", "duology")
    if any(c in inner for c in indicators) or any(w in inner.lower() for w in words):
        return (m.group(1).rstrip(), inner)
    return (title, None)


def _gridline_levels(max_books: int) -> list:
    """Return a list of (value, percent_from_bottom, label) tuples for the
    chart's dotted gridlines. Positions match the actual bar-stack geometry
    in the CSS: each spine is 16px tall with a 2px gap, in a 200px-tall chart
    container. So a V-spine bar's top sits at (V*16 + (V-1)*2) px from the
    chart bottom. The peak value is always labeled so the scale is unambiguous.
    """
    if max_books <= 0:
        return []
    chart_h = 200  # px; must match .chart height in the template
    spine_h = 16
    gap = 2

    def position_pct(v: int) -> float:
        bar_top_px = v * spine_h + max(0, v - 1) * gap
        return round(100.0 * bar_top_px / chart_h, 2)

    values = []
    # One intermediate gridline at roughly the midpoint when there's room.
    if max_books >= 4:
        mid = max_books // 2
        if mid > 0 and mid < max_books:
            values.append(mid)
    values.append(max_books)

    levels = []
    for v in values:
        levels.append({
            "value": v,
            "percent": position_pct(v),
            "label": str(v),
        })
    return levels


def render_html_report(stats: Stats, genre_data, output_path: Path) -> Path:
    """Render the full year-in-books as standalone HTML/CSS using Jinja2.
    Templated against year_in_books_report.html — editorial typographic
    design with Fraunces serif + Inter sans, warm cream palette, terracotta
    accent. Independent of the matplotlib pipeline."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates_dir = Path(__file__).resolve().parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("year_in_books_report.html")

    # ----- Stats strip (four highlights) -----
    stats_strip: list = []
    # Peak month
    peak_count = 0
    peak_month_short = ""
    if stats.books_per_month:
        peak_label, peak_count = max(stats.books_per_month, key=lambda x: x[1])
        if peak_count > 0:
            parts = peak_label.split()
            month_name = parts[0]
            year_short = parts[1][-2:] if len(parts) == 2 else ""
            peak_month_short = f"{month_name} '{year_short}" if year_short else month_name
            stats_strip.append({
                "label": "Peak Month",
                "primary": month_name,
                "primary_small": f"&rsquo;{year_short}" if year_short else None,
                "detail": f"{peak_count} books — the year&rsquo;s most prolific stretch",
                "numeric": True,
            })

    # Books read
    if stats.total_books:
        stats_strip.append({
            "label": "Books Read",
            "primary": f"{stats.total_books:,}",
            "primary_small": None,
            "detail": "finished cover-to-cover this year",
            "numeric": True,
        })

    # Longest read
    with_pages = [b for b in stats.books if b.num_pages]
    if with_pages:
        longest = max(with_pages, key=lambda b: b.num_pages)
        long_title, _ = _split_title_series(longest.title)
        stats_strip.append({
            "label": "Longest Read",
            "primary": long_title,
            "primary_small": None,
            "detail": f"{longest.num_pages:,} pages &middot; {longest.author}",
            "numeric": False,
        })
        # Average length
        avg = stats.total_pages // max(1, len(with_pages))
        stats_strip.append({
            "label": "Average Length",
            "primary": f"{avg:,}",
            "primary_small": "pp.",
            "detail": f"per book, across {len(with_pages)} finished",
            "numeric": True,
        })

    # ----- Chart data -----
    max_books = max((c for _, c in stats.books_per_month), default=0)
    months_data = []
    for label, count in stats.books_per_month:
        parts = label.split()
        short_name = parts[0] if parts else label
        year_short = parts[1][-2:] if len(parts) == 2 else ""
        months_data.append({
            "label": label,
            "short_name": short_name,
            "year_short": year_short,
            "count": count,
            "peak": count > 0 and count == max_books,
        })

    gridlines = _gridline_levels(max_books)

    # ----- Genres -----
    genre_items = genre_data[0] if genre_data else []
    genre_max = max((v for _, v in genre_items), default=1)
    # Editorial aside: derive a short headline from the data
    genre_aside = None
    if genre_items:
        top_label, top_count = genre_items[0]
        if top_count >= stats.total_books * 0.4:
            genre_aside = f"a {top_label.lower()}-heavy year"
        elif len(genre_items) >= 5:
            genre_aside = "a wide-ranging year"

    # ----- Book list groups -----
    groups: list = []
    cur_label = None
    cur_list: list = []
    num = stats.total_books
    for date, title, author in stats.book_titles:
        label = date.strftime("%B %Y")
        if label != cur_label:
            if cur_list:
                groups.append((cur_label, cur_list))
            cur_label = label
            cur_list = []
        main_title, series = _split_title_series(title)
        cur_list.append((num, main_title, series, author))
        num -= 1
    if cur_list:
        groups.append((cur_label, cur_list))

    # ----- Render -----
    today = datetime.now().astimezone()
    html = template.render(
        stats=stats,
        first_name=stats.first_name,
        window_start_str=stats.window_start.strftime("%B %Y"),
        window_end_str=stats.window_end.strftime("%B %Y"),
        stats_strip=stats_strip,
        peak_count=peak_count,
        peak_month_short=peak_month_short,
        months=months_data,
        gridlines=gridlines,
        genre_items=genre_items,
        genre_max=genre_max,
        genre_aside=genre_aside,
        groups=groups,
        generated_str=today.strftime("%d %B %Y"),
    )
    output_path = Path(output_path)
    output_path.write_text(html, encoding="utf-8")
    return output_path


# -------- end-to-end --------

def generate(
    user_id: str,
    output_dir: Path,
    today: Optional[datetime] = None,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> dict:
    """Generate the report. By default (no window_start/window_end) this is
    the rolling last-12-months view and always writes the fixed
    `year_in_books.*` filenames, overwriting any previous run — that's the
    "current report" entry point used by the CLI and the main web UI button.

    Passing an explicit window_start/window_end instead renders that custom
    date range and writes filenames suffixed with the range (e.g.
    `year_in_books_20260101-20260821.pdf`) so different custom ranges don't
    clobber each other on disk.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "genres_cache.json"

    books, first_name = fetch_read_shelf(user_id)

    if window_start is not None and window_end is not None:
        stats = aggregate_range(books, window_start, window_end, first_name=first_name)
        basename = f"year_in_books_{window_start:%Y%m%d}-{window_end:%Y%m%d}"
    else:
        stats = aggregate_last_12_months(books, today=today, first_name=first_name)
        basename = "year_in_books"

    if stats.total_books == 0:
        genre_data = ([], 0)
    else:
        genres_by_isbn = lookup_genres(stats.books, cache_path)
        genre_data = aggregate_genres(stats.books, genres_by_isbn)

    # HTML is the single source of truth for design. PDF and PNGs are
    # rendered FROM the HTML via headless Chromium (Playwright).
    html_path = output_dir / f"{basename}.html"
    render_html_report(stats, genre_data, html_path)
    paths = {"html": html_path}
    try:
        paths.update(render_html_outputs(html_path, output_dir, basename=basename))
    except Exception as e:
        logging.exception("Playwright render failed: %s", e)

    return {
        "total_books": stats.total_books,
        "total_pages": stats.total_pages,
        "books_missing_pages": stats.books_missing_pages,
        "window_start": stats.window_start.strftime("%Y-%m-%d"),
        "window_end": stats.window_end.strftime("%Y-%m-%d"),
        "outputs": {name: str(p) for name, p in paths.items()},
    }


def render_html_outputs(html_path: Path, output_dir: Path, basename: str = "year_in_books") -> dict:
    """Render the HTML report to PDF, web PNG, and 9:16 social card PNG
    via headless Chromium (Playwright). Single browser launch shared
    across the renders for speed. Returns a dict of {format: path} with
    keys: pdf, web, social."""
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path).resolve()
    html_url = html_path.as_uri()
    out: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # ---- PDF (Letter portrait) ----
            page = browser.new_page()
            page.goto(html_url, wait_until="networkidle")
            pdf_path = output_dir / f"{basename}.pdf"
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                margin={"top": "0.5in", "bottom": "0.5in",
                        "left": "0.5in", "right": "0.5in"},
            )
            out["pdf"] = pdf_path
            page.close()

            # ---- Web PNG (desktop viewport sized to match typical embed
            # widths (~720-800px logical). DPR=2 keeps it crisp on retina;
            # rendering at this viewport instead of 1200 prevents the heavy
            # downscale that made the previous 2400px-wide PNG hard to read. ----
            ctx = browser.new_context(viewport={"width": 800, "height": 1200},
                                      device_scale_factor=2)
            page = ctx.new_page()
            page.goto(html_url, wait_until="networkidle")
            web_path = output_dir / f"{basename}_web.png"
            page.screenshot(path=str(web_path), full_page=True)
            out["web"] = web_path
            ctx.close()

            # ---- Social card (9:16, 1080×1920) ----
            # Viewport 540 × DPR 2 = 1080-wide output. The @media (max-width:
            # 720px) rules in the template trigger at this viewport, applying
            # the larger mobile type scale. Adding `card-mode` to <body>
            # activates the dense 9:16 layout (masthead + stats + chart +
            # 2-col book list) defined in the template's .card-mode CSS,
            # which is then clipped to 540×960 CSS px (= 1080×1920 PNG at
            # DPR=2). This format fits feed-based platforms (Bluesky,
            # Mastodon, Instagram Stories) without the cropping/blurring
            # that very tall images get on those services.
            ctx = browser.new_context(viewport={"width": 540, "height": 960},
                                      device_scale_factor=2)
            page = ctx.new_page()
            page.goto(html_url, wait_until="networkidle")
            page.evaluate("document.body.classList.add('card-mode')")
            social_path = output_dir / f"{basename}_social.png"
            page.screenshot(
                path=str(social_path),
                clip={"x": 0, "y": 0, "width": 540, "height": 960},
            )
            out["social"] = social_path
            ctx.close()
        finally:
            browser.close()

    return out


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 'year in books' visualization from a Goodreads read shelf."
    )
    parser.add_argument("--user-id", help="Goodreads user ID (else read from config.json).")
    parser.add_argument("--output-dir", default="output", help="Output directory (default: output).")
    parser.add_argument("--config", default="config.json", help="Path to config.json.")
    parser.add_argument(
        "--start-date", metavar="YYYY-MM-DD",
        help="Custom range start (inclusive). Requires --end-date. "
             "Default: rolling last 12 months ending today.",
    )
    parser.add_argument(
        "--end-date", metavar="YYYY-MM-DD",
        help="Custom range end (inclusive). Requires --start-date.",
    )
    args = parser.parse_args(argv)

    if bool(args.start_date) != bool(args.end_date):
        print("--start-date and --end-date must be given together", file=sys.stderr)
        return 2

    window_start = window_end = None
    if args.start_date and args.end_date:
        try:
            window_start = datetime.strptime(args.start_date, "%Y-%m-%d").astimezone()
            window_end = datetime.strptime(args.end_date, "%Y-%m-%d").astimezone().replace(
                hour=23, minute=59, second=59
            )
        except ValueError as e:
            print(f"invalid date: {e}", file=sys.stderr)
            return 2
        if window_start > window_end:
            print("--start-date must not be after --end-date", file=sys.stderr)
            return 2

    user_id = args.user_id
    if not user_id:
        config_path = Path(args.config)
        config = load_config(config_path)
        if not config:
            print(f"config not found at {config_path} and no --user-id supplied", file=sys.stderr)
            return 2
        user_id = config.get("goodreads_user_id")
        if not user_id:
            print("goodreads_user_id missing from config.json / config.local.json", file=sys.stderr)
            return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = generate(user_id, Path(args.output_dir), window_start=window_start, window_end=window_end)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
