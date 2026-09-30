"""Scrapes a public Letterboxd profile's diary and watchlist with a real browser.

Letterboxd's HTML pages sit behind Cloudflare bot protection that blocks
plain HTTP requests -- confirmed both with curl and with `requests` (both get
a 403 "Just a moment" challenge page). A real browser, even headless, passes
through cleanly, so this drives one with Playwright instead.

The diary page (unlike the RSS feed) paginates through a user's ENTIRE
logged history rather than just the ~100 most recent activity items, and
each row directly exposes rating, like, rewatch, and "has a written review"
as separate signals. It doesn't expose a TMDB id though, so films scraped
here need a follow-up TMDB search to resolve one (see resolve_tmdb_ids).

The watchlist page uses the same paginated poster-grid markup, just without
any of the diary's per-entry signals -- it's only used to flag which
recommendations the user already meant to watch (see scrape_watchlist).
"""
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright

from .letterboxd import LetterboxdError, LoggedFilm, normalize_title
from .tmdb import MAX_WORKERS, TMDBClient

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAX_PAGES = 30  # safety cap (~1500 entries) against runaway scrapes for very prolific loggers

DIARY_URL_TEMPLATE = "https://letterboxd.com/{username}/diary/films/page/{page}/"
WATCHLIST_URL_TEMPLATE = "https://letterboxd.com/{username}/watchlist/page/{page}/"

_EXTRACT_NAMES_JS = """
() => Array.from(document.querySelectorAll('[data-item-full-display-name]'))
    .map(el => el.getAttribute('data-item-full-display-name'))
"""

_EXTRACT_ROWS_JS = """
() => Array.from(document.querySelectorAll('tr.diary-entry-row')).map(row => {
    const nameEl = row.querySelector('[data-item-full-display-name]');
    const dateLink = row.querySelector('a.daydate');
    const ratingSpan = row.querySelector('.col-rating .rating');
    const ratingClass = ratingSpan
        ? Array.from(ratingSpan.classList).find(c => c.startsWith('rated-'))
        : null;
    const rewatchTd = row.querySelector('.col-rewatch');
    return {
        fullName: nameEl ? nameEl.getAttribute('data-item-full-display-name') : null,
        dateHref: dateLink ? dateLink.getAttribute('href') : null,
        ratingClass: ratingClass,
        liked: !!row.querySelector('.col-like .icon-liked'),
        isRewatchEntry: rewatchTd ? !rewatchTd.className.includes('icon-status-off') : false,
        isReview: row.getAttribute('data-object-name') === 'review',
    };
})
"""

_LAST_PAGE_JS = """
() => {
    const nums = Array.from(document.querySelectorAll('.paginate-page a, .pagination a'))
        .map(a => parseInt(a.textContent.trim(), 10))
        .filter(n => !isNaN(n));
    return nums.length ? Math.max(...nums) : 1;
}
"""


def _parse_title_year(full_name: str) -> tuple[str, int | None]:
    """'Top Gun: Maverick (2022)' -> ('Top Gun: Maverick', 2022)."""
    m = re.match(r"^(.*)\s\((\d{4})\)$", full_name)
    if not m:
        return full_name, None
    return m.group(1), int(m.group(2))


def _parse_date(date_href: str | None) -> str | None:
    if not date_href:
        return None
    m = re.search(r"/for/(\d{4})/(\d{2})/(\d{2})/", date_href)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _paginate(page, url_template: str, extract_js: str, username: str, progress, max_pages: int, label: str) -> list:
    """Walks numbered pages of a listing (diary, watchlist, ...) with an
    already-open Playwright page, extracting each page's rows via the given
    JS and stopping at the last page (or max_pages, or an empty page)."""

    def report(msg: str):
        if progress:
            progress(msg)

    rows: list = []
    total_pages = None
    page_num = 1
    while page_num <= max_pages:
        url = url_template.format(username=username, page=page_num)
        report(f"Scraping {label} page {page_num}{f' of {total_pages}' if total_pages else ''}...")
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp is not None and resp.status == 404:
            if page_num == 1:
                raise LetterboxdError(f"No Letterboxd user found at '{username}'.")
            break

        page_rows = page.evaluate(extract_js)
        if not page_rows:
            break
        rows.extend(page_rows)

        if total_pages is None:
            total_pages = page.evaluate(_LAST_PAGE_JS)
        if page_num >= total_pages:
            break
        page_num += 1
    return rows


def _aggregate_diary_rows(raw_rows: list[dict]) -> list[LoggedFilm]:
    entries_by_key: dict[tuple[str, int | None], list[dict]] = defaultdict(list)
    for row in raw_rows:
        if not row["fullName"]:
            continue
        title, year = _parse_title_year(row["fullName"])
        rating = int(row["ratingClass"].split("-")[1]) / 2 if row["ratingClass"] else None
        entries_by_key[(title.lower(), year)].append(
            {
                "title": title,
                "year": year,
                "rating": rating,
                "liked": row["liked"],
                "is_review": row["isReview"],
                "watched_date": _parse_date(row["dateHref"]),
            }
        )

    films = []
    for entries in entries_by_key.values():
        rating = next((e["rating"] for e in entries if e["rating"] is not None), None)
        films.append(
            LoggedFilm(
                title=entries[0]["title"],
                year=entries[0]["year"],
                tmdb_id=None,
                rating=rating,
                liked=any(e["liked"] for e in entries),
                times_logged=len(entries),
                times_reviewed=sum(1 for e in entries if e["is_review"]),
                watched_date=entries[0]["watched_date"],
            )
        )
    return films


def scrape_diary(username: str, progress=None, max_pages: int = MAX_PAGES) -> list[LoggedFilm]:
    """Scrapes every diary page for a public profile and returns one
    LoggedFilm per unique film, aggregating rewatches/re-reviews the same
    way fetch_diary() does. tmdb_id is left unset -- call resolve_tmdb_ids()
    afterward."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            raw_rows = _paginate(
                page, DIARY_URL_TEMPLATE, _EXTRACT_ROWS_JS, username, progress, max_pages, "diary"
            )
        finally:
            browser.close()

    if not raw_rows:
        raise LetterboxdError(f"'{username}' has no public diary entries, or the profile is private.")
    return _aggregate_diary_rows(raw_rows)


def scrape_watchlist(username: str, progress=None, max_pages: int = MAX_PAGES) -> set[tuple[str, int | None]]:
    """Scrapes every watchlist page for a public profile and returns a set
    of (normalized title, year) keys -- the watchlist page doesn't carry a
    TMDB id, and matching by title+year against a full candidate list is
    enough without a follow-up search per entry. Returns an empty set
    (rather than raising) if the watchlist is empty or hidden, since it's a
    supplementary feature, not something the app depends on to function."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            try:
                names = _paginate(
                    page, WATCHLIST_URL_TEMPLATE, _EXTRACT_NAMES_JS, username, progress, max_pages, "watchlist"
                )
            except LetterboxdError:
                names = []
        finally:
            browser.close()

    keys = set()
    for full_name in names:
        if not full_name:
            continue
        title, year = _parse_title_year(full_name)
        keys.add((normalize_title(title), year))
    return keys


def scrape_profile(
    username: str,
    progress=None,
    max_diary_pages: int = MAX_PAGES,
    max_watchlist_pages: int = MAX_PAGES,
) -> tuple[list[LoggedFilm], set[tuple[str, int | None]]]:
    """Scrapes both the diary and the watchlist in one browser session
    (rather than launching Chromium twice) and returns (films, watchlist_keys)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            raw_rows = _paginate(
                page, DIARY_URL_TEMPLATE, _EXTRACT_ROWS_JS, username, progress, max_diary_pages, "diary"
            )
            if not raw_rows:
                raise LetterboxdError(f"'{username}' has no public diary entries, or the profile is private.")

            try:
                watchlist_names = _paginate(
                    page, WATCHLIST_URL_TEMPLATE, _EXTRACT_NAMES_JS, username, progress,
                    max_watchlist_pages, "watchlist",
                )
            except LetterboxdError:
                watchlist_names = []  # the diary succeeded, so the profile exists -- a hidden/empty watchlist is fine
        finally:
            browser.close()

    films = _aggregate_diary_rows(raw_rows)
    watchlist_keys = {
        (normalize_title(t), y) for t, y in (_parse_title_year(n) for n in watchlist_names if n)
    }
    return films, watchlist_keys


def resolve_tmdb_ids(films: list[LoggedFilm], client: TMDBClient, progress=None) -> None:
    """Fills in tmdb_id for scraped films via TMDB search, in place. The
    diary page doesn't carry a TMDB id the way the RSS feed does. Each
    search is an independent request, so they run concurrently rather than
    one at a time -- this is the single biggest chunk of the app's runtime
    for an active profile (one search per unique logged film)."""
    to_resolve = [f for f in films if f.tmdb_id is None]
    if not to_resolve:
        return

    def _resolve(film: LoggedFilm) -> None:
        match = client.search_movie(film.title, film.year)
        if match:
            film.tmdb_id = match["id"]

    total = len(to_resolve)
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_resolve, f) for f in to_resolve]
        for future in as_completed(futures):
            future.result()  # surface any exception instead of swallowing it
            done += 1
            if progress and (done % 25 == 0 or done == total):
                progress(f"Matching scraped films to TMDB ({done}/{total})...")
