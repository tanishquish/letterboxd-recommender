"""Thin TMDB (themoviedb.org) v3 API client with an in-memory cache.

Needs a free TMDB API key: https://www.themoviedb.org/settings/api
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

BASE_URL = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"
MAX_WORKERS = 16

# TMDB's standard movie genre list -- the same set Letterboxd shows on its
# own /films/genre/ pages, since Letterboxd sources genre metadata from
# TMDB. Fixed and stable enough to hardcode rather than fetch on every load.
GENRES = [
    (28, "Action"),
    (12, "Adventure"),
    (16, "Animation"),
    (35, "Comedy"),
    (80, "Crime"),
    (99, "Documentary"),
    (18, "Drama"),
    (10751, "Family"),
    (14, "Fantasy"),
    (36, "History"),
    (27, "Horror"),
    (10402, "Music"),
    (9648, "Mystery"),
    (10749, "Romance"),
    (878, "Science Fiction"),
    (53, "Thriller"),
    (10752, "War"),
    (37, "Western"),
]


class TMDBError(Exception):
    pass


class TMDBClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        # The requests default pool (10 connections) would bottleneck the
        # concurrent fetches below; give it enough headroom for MAX_WORKERS.
        adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
        self.session.mount("https://", adapter)
        self._details_cache: dict[int, dict] = {}
        self._rec_cache: dict[tuple[str, int], list[dict]] = {}
        self._providers_cache: dict[int, list[str]] = {}

    def _get(self, path: str, **params) -> dict:
        params["api_key"] = self.api_key
        try:
            resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=15)
        except requests.RequestException as e:
            raise TMDBError(f"Couldn't reach TMDB: {e}") from e

        if resp.status_code == 401:
            raise TMDBError("TMDB rejected the API key. Check it's a valid v3 API key.")
        if resp.status_code == 404:
            return {}
        if resp.status_code == 429:
            time.sleep(1)
            return self._get(path, **{k: v for k, v in params.items() if k != "api_key"})
        if resp.status_code != 200:
            raise TMDBError(f"TMDB error {resp.status_code} on {path}")
        return resp.json()

    def verify_key(self) -> bool:
        try:
            self._get("/configuration")
            return True
        except TMDBError:
            return False

    def movie_details(self, movie_id: int) -> dict:
        """Full details + keywords + top credits for one movie, cached."""
        if movie_id in self._details_cache:
            return self._details_cache[movie_id]

        data = self._get(f"/movie/{movie_id}", append_to_response="keywords,credits")
        if not data:
            self._details_cache[movie_id] = {}
            return {}

        genres = [g["id"] for g in data.get("genres", [])]
        genre_names = {g["id"]: g["name"] for g in data.get("genres", [])}
        keywords = [k["id"] for k in data.get("keywords", {}).get("keywords", [])]
        keyword_names = {k["id"]: k["name"] for k in data.get("keywords", {}).get("keywords", [])}

        credits = data.get("credits", {})
        directors = [
            {"id": c["id"], "name": c["name"]}
            for c in credits.get("crew", [])
            if c.get("job") == "Director"
        ]
        cast = [
            {"id": c["id"], "name": c["name"]}
            for c in credits.get("cast", [])[:6]
        ]

        parsed = {
            "id": data["id"],
            "title": data.get("title"),
            "year": (data.get("release_date") or "")[:4],
            "overview": data.get("overview", ""),
            "poster_path": data.get("poster_path"),
            "vote_average": data.get("vote_average", 0),
            "popularity": data.get("popularity", 0),
            "genres": genres,
            "genre_names": genre_names,
            "keywords": keywords,
            "keyword_names": keyword_names,
            "directors": directors,
            "cast": cast,
        }
        self._details_cache[movie_id] = parsed
        return parsed

    def related(self, movie_id: int, kind: str = "recommendations") -> list[dict]:
        """kind is 'recommendations' or 'similar'."""
        cache_key = (kind, movie_id)
        if cache_key in self._rec_cache:
            return self._rec_cache[cache_key]
        data = self._get(f"/movie/{movie_id}/{kind}")
        results = data.get("results", []) if data else []
        self._rec_cache[cache_key] = results
        return results

    def discover_by_genre(self, genre_id: int, pages: int = 2) -> list[dict]:
        """Well-regarded movies in a genre, independent of any seed film.

        Used for genres a user picks explicitly but may barely appear in
        their own ratings, where recommendations/similar off their favorite
        films would never surface anything.
        """
        cache_key = ("discover", genre_id)
        if cache_key in self._rec_cache:
            return self._rec_cache[cache_key]

        results = []
        for page in range(1, pages + 1):
            data = self._get(
                "/discover/movie",
                with_genres=genre_id,
                sort_by="popularity.desc",
                **{"vote_count.gte": 200},
                page=page,
            )
            results.extend(data.get("results", []) if data else [])
        self._rec_cache[cache_key] = results
        return results

    def watch_providers(self, movie_id: int, region: str = "US") -> list[str]:
        """A combined, deduplicated list of streaming/rental/purchase
        provider names for a region -- the same JustWatch-sourced data
        Letterboxd's own "Where to watch" panel shows (verified against a
        real film page). Subscription services (flatrate/free/ads) are
        listed ahead of rent/buy, since "already included" matters more
        than "you'd have to pay for it." Empty if the film isn't available
        anywhere for that region (covers both "showtimes only, no home
        release yet" and "no data at all" -- TMDB doesn't distinguish
        those, and neither does the UI need to)."""
        if movie_id in self._providers_cache:
            return self._providers_cache[movie_id]

        data = self._get(f"/movie/{movie_id}/watch/providers")
        region_data = (data.get("results") or {}).get(region, {}) if data else {}
        seen: set[str] = set()
        ordered: list[str] = []
        for category in ("flatrate", "free", "ads", "rent", "buy"):
            providers = sorted(region_data.get(category, []), key=lambda p: p.get("display_priority", 999))
            for p in providers:
                name = p["provider_name"]
                if name not in seen:
                    seen.add(name)
                    ordered.append(name)
        self._providers_cache[movie_id] = ordered
        return ordered

    def prefetch_watch_providers(self, movie_ids: list[int], max_workers: int = MAX_WORKERS) -> None:
        uncached = [m for m in dict.fromkeys(movie_ids) if m not in self._providers_cache]
        if not uncached:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(self.watch_providers, uncached))

    def search_movie(self, title: str, year: int | None) -> dict | None:
        data = self._get("/search/movie", query=title, year=year)
        results = data.get("results") if data else None
        return results[0] if results else None

    def poster_url(self, poster_path: str | None) -> str | None:
        return f"{POSTER_BASE}{poster_path}" if poster_path else None

    def prefetch_movie_details(self, movie_ids: list[int], max_workers: int = MAX_WORKERS) -> None:
        """Warms the details cache for many ids concurrently -- these are
        independent GET requests, so there's no reason to make them one at
        a time. Callers keep using movie_details() as normal afterward;
        this just makes those calls near-instant cache hits."""
        uncached = list(dict.fromkeys(mid for mid in movie_ids if mid not in self._details_cache))
        if not uncached:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(self.movie_details, uncached))

    def prefetch_related(
        self, movie_ids: list[int], kinds: tuple[str, ...] = ("recommendations", "similar"),
        max_workers: int = MAX_WORKERS,
    ) -> None:
        pairs = [
            (mid, kind)
            for mid in dict.fromkeys(movie_ids)
            for kind in kinds
            if (kind, mid) not in self._rec_cache
        ]
        if not pairs:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(lambda pair: self.related(*pair), pairs))

    def prefetch_discover(self, genre_ids: list[int], max_workers: int = 6) -> None:
        uncached = [g for g in dict.fromkeys(genre_ids) if ("discover", g) not in self._rec_cache]
        if not uncached:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(self.discover_by_genre, uncached))
