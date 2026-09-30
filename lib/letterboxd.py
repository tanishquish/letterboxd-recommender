"""Reads a public Letterboxd profile via its RSS feed.

Letterboxd doesn't offer a public API, but every profile publishes an RSS feed
at /<username>/rss/ that includes each logged film's title, year, member
rating, like status, and (usefully) its TMDB id via the tmdb: namespace. The
feed only carries the ~100 most recent activity entries -- there's no
supported pagination -- so the taste profile is built from recent activity
rather than a user's entire history.
"""
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass

import requests

NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class LetterboxdError(Exception):
    pass


_TITLE_NOISE_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    """Lowercases and strips punctuation/whitespace noise so titles can be
    compared across sources (Letterboxd scrape vs. TMDB) that format them
    slightly differently."""
    return _TITLE_NOISE_RE.sub(" ", title.lower()).strip()


@dataclass
class LoggedFilm:
    title: str
    year: int | None
    tmdb_id: int | None
    rating: float | None  # 0.5-5.0 stars, None if logged without a rating
    liked: bool  # true if liked on ANY of its log entries
    times_logged: int  # diary entries for this film in the fetched window (rewatches included)
    times_reviewed: int  # of those, how many carried a written review
    watched_date: str | None  # most recent


def fetch_diary(username: str) -> list[LoggedFilm]:
    """Fetch and parse a user's public activity RSS feed."""
    url = f"https://letterboxd.com/{username.strip().lstrip('@')}/rss/"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as e:
        raise LetterboxdError(f"Couldn't reach Letterboxd: {e}") from e

    if resp.status_code == 404:
        raise LetterboxdError(f"No Letterboxd user found at '{username}'.")
    if resp.status_code != 200:
        raise LetterboxdError(
            f"Letterboxd returned an unexpected status ({resp.status_code}). "
            "The profile may be private, or Letterboxd is temporarily blocking requests."
        )

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise LetterboxdError(f"Couldn't parse Letterboxd's RSS feed: {e}") from e

    items = root.findall(".//item")
    if not items:
        raise LetterboxdError(
            f"'{username}' has no public activity, or the profile is private."
        )

    # Each diary/watch entry is its own RSS item, so a rewatch or a film
    # reviewed more than once shows up as multiple items for the same film.
    # The feed lists newest first, so entries are grouped in that order and
    # the group's rating/tmdb id/watched date come from the most recent one.
    entries_by_key: dict[tuple[str, int | None], list[dict]] = defaultdict(list)
    for item in items:
        # Only "watched a film" entries carry a filmTitle; list activity
        # entries don't and should be skipped.
        title_el = item.find("letterboxd:filmTitle", NS)
        if title_el is None or not title_el.text:
            continue

        year_el = item.find("letterboxd:filmYear", NS)
        tmdb_el = item.find("tmdb:movieId", NS)
        rating_el = item.find("letterboxd:memberRating", NS)
        liked_el = item.find("letterboxd:memberLike", NS)
        watched_el = item.find("letterboxd:watchedDate", NS)
        guid_el = item.find("guid")

        year = int(year_el.text) if year_el is not None and year_el.text else None
        # letterboxd-review-<id> guids carry a written review; letterboxd-watch-<id>
        # guids are a plain log with no review text.
        is_review = bool(guid_el is not None and guid_el.text and "letterboxd-review-" in guid_el.text)

        key = (title_el.text.lower(), year)
        entries_by_key[key].append(
            {
                "title": title_el.text,
                "year": year,
                "tmdb_id": int(tmdb_el.text) if tmdb_el is not None and tmdb_el.text else None,
                "rating": float(rating_el.text) if rating_el is not None and rating_el.text else None,
                "liked": (liked_el is not None and liked_el.text == "Yes"),
                "is_review": is_review,
                "watched_date": watched_el.text if watched_el is not None else None,
            }
        )

    films: list[LoggedFilm] = []
    for entries in entries_by_key.values():
        rating = next((e["rating"] for e in entries if e["rating"] is not None), None)
        tmdb_id = next((e["tmdb_id"] for e in entries if e["tmdb_id"] is not None), None)
        films.append(
            LoggedFilm(
                title=entries[0]["title"],
                year=entries[0]["year"],
                tmdb_id=tmdb_id,
                rating=rating,
                liked=any(e["liked"] for e in entries),
                times_logged=len(entries),
                times_reviewed=sum(1 for e in entries if e["is_review"]),
                watched_date=entries[0]["watched_date"],
            )
        )
    return films


def extract_username(profile_input: str) -> str:
    """Accepts a bare username or a full profile URL."""
    profile_input = profile_input.strip()
    m = re.search(r"letterboxd\.com/([^/]+)/?", profile_input)
    if m:
        return m.group(1)
    return profile_input.lstrip("@")
