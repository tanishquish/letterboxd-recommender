"""Scrapes a public Letterboxd profile's full diary with a real browser.

Letterboxd's HTML pages sit behind Cloudflare bot protection that blocks
plain HTTP requests -- confirmed both with curl and with `requests` (both get
a 403 "Just a moment" challenge page). A real browser, even headless, passes
through cleanly, so this drives one with Playwright instead.

The diary page (unlike the RSS feed) paginates through a user's ENTIRE
logged history rather than just the ~100 most recent activity items, and
each row directly exposes rating, like, rewatch, and "has a written review"
as separate signals. It doesn't expose a TMDB id though, so films scraped
here need a follow-up TMDB search to resolve one (see resolve_tmdb_ids).
"""
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright

from .letterboxd import LetterboxdError, LoggedFilm
from .tmdb import MAX_WORKERS, TMDBClient

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAX_PAGES = 30  # safety cap (~1500 entries) against runaway scrapes for very prolific loggers

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


def scrape_diary(username: str, progress=None, max_pages: int = MAX_PAGES) -> list[LoggedFilm]:
    """Scrapes every diary page for a public profile and returns one
    LoggedFilm per unique film, aggregating rewatches/re-reviews the same
    way fetch_diary() does. tmdb_id is left unset -- call resolve_tmdb_ids()
    afterward."""

    def report(msg: str):
        if progress:
            progress(msg)

    raw_rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            total_pages = None
            page_num = 1
            while page_num <= max_pages:
                url = f"https://letterboxd.com/{username}/diary/films/page/{page_num}/"
                report(f"Scraping diary page {page_num}{f' of {total_pages}' if total_pages else ''}...")
                resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                if resp is not None and resp.status == 404:
                    if page_num == 1:
                        raise LetterboxdError(f"No Letterboxd user found at '{username}'.")
                    break

                rows = page.evaluate(_EXTRACT_ROWS_JS)
                if not rows:
                    break
                raw_rows.extend(rows)

                if total_pages is None:
                    total_pages = page.evaluate(_LAST_PAGE_JS)
                if page_num >= total_pages:
                    break
                page_num += 1
        finally:
            browser.close()

    if not raw_rows:
        raise LetterboxdError(f"'{username}' has no public diary entries, or the profile is private.")

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
