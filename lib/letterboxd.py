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


@dataclass
class LoggedFilm:
    title: str
    year: int | None
    tmdb_id: int | None
    rating: float | None  # 0.5-5.0 stars, None if logged without a rating
    liked: bool
    rewatch: bool
    watched_date: str | None


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

    films: list[LoggedFilm] = []
    for item in items:
        # Only "watched a film" entries carry a filmTitle; list/review-only
        # activity entries don't and should be skipped.
        title_el = item.find("letterboxd:filmTitle", NS)
        if title_el is None or not title_el.text:
            continue

        year_el = item.find("letterboxd:filmYear", NS)
        tmdb_el = item.find("tmdb:movieId", NS)
        rating_el = item.find("letterboxd:memberRating", NS)
        liked_el = item.find("letterboxd:memberLike", NS)
        rewatch_el = item.find("letterboxd:rewatch", NS)
        watched_el = item.find("letterboxd:watchedDate", NS)

        films.append(
            LoggedFilm(
                title=title_el.text,
                year=int(year_el.text) if year_el is not None and year_el.text else None,
                tmdb_id=int(tmdb_el.text) if tmdb_el is not None and tmdb_el.text else None,
                rating=float(rating_el.text) if rating_el is not None and rating_el.text else None,
                liked=(liked_el is not None and liked_el.text == "Yes"),
                rewatch=(rewatch_el is not None and rewatch_el.text == "Yes"),
                watched_date=watched_el.text if watched_el is not None else None,
            )
        )

    # Collapse rewatches / duplicate log entries for the same film, keeping
    # the most informative (rated > liked > first-seen) entry.
    by_key: dict[tuple[str, int | None], LoggedFilm] = {}
    for f in films:
        key = (f.title.lower(), f.year)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = f
        elif existing.rating is None and f.rating is not None:
            by_key[key] = f
    return list(by_key.values())


def extract_username(profile_input: str) -> str:
    """Accepts a bare username or a full profile URL."""
    profile_input = profile_input.strip()
    m = re.search(r"letterboxd\.com/([^/]+)/?", profile_input)
    if m:
        return m.group(1)
    return profile_input.lstrip("@")
