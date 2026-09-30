"""Builds a taste profile from everything a user has logged and scores candidates.

Approach:
1. Every logged film contributes a weight to a profile of genres / keywords
   / people (director + top cast) -- not just rated films. The weight blends
   several signals: star rating (if any), a flat bonus for the like feature
   (independent of rating -- a liked-but-unrated film still counts), and
   bonuses for being logged and/or reviewed more than once, since a rewatch
   or a fresh review on a rewatch is a stronger signal than a single viewing.
2. Candidates come from TMDB's own "recommendations" and "similar" endpoints,
   seeded from the user's highest-weighted films, since that's a much richer
   signal than genre-matching alone (it captures tone, era, style). If the
   user picked genres to focus on, TMDB's "discover" endpoint also pulls in
   well-regarded films from those genres directly, since a genre absent from
   someone's own ratings would never surface via seed-based recommendations.
3. Each candidate is scored by blending TMDB's own ranking (it returned the
   candidate near the top of a favorite film's list) with how well the
   candidate's genres/keywords/people match the user's overall profile, then
   lightly floored by TMDB's average rating so obscure poorly-rated titles
   don't dominate just because they matched a keyword. If genres were picked,
   the final list is filtered down to only films in those genres.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from .letterboxd import LoggedFilm, normalize_title
from .tmdb import GENRES, TMDBClient

FAVORITE_WEIGHT_THRESHOLD = 1.5  # roughly equivalent to a plain 4-star rating
MAX_SEEDS = 20
MAX_SEEDS_PER_GENRE = 6
MAX_RESULTS_PER_GENRE = 5
CANDIDATES_PER_SEED = 12
MAX_PREFERRED_GENRES = 3
GENRE_PREFERENCE_BOOST = 10.0
GENRE_NAMES = dict(GENRES)

LIKE_BONUS = 0.75
REWATCH_BONUS_PER_EXTRA = 0.5
REREVIEW_BONUS_PER_EXTRA = 0.3
MAX_BONUS_OCCURRENCES = 3  # cap how many extra logs/reviews keep adding weight


@dataclass
class Profile:
    genre_weight: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    keyword_weight: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    person_weight: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    genre_names: dict[int, str] = field(default_factory=dict)
    keyword_names: dict[int, str] = field(default_factory=dict)
    person_names: dict[int, str] = field(default_factory=dict)


@dataclass
class Recommendation:
    tmdb_id: int
    title: str
    year: str
    overview: str
    poster_url: str | None
    vote_average: float
    score: float
    because_of: list[str]
    matched_genres: list[str]
    genre_ids: list[int]
    on_watchlist: bool = False


def film_weight(logged: LoggedFilm) -> float:
    """How strongly a logged film should pull the taste profile toward its
    traits. Rating contributes the bulk of it, but the like feature, rewatches,
    and re-reviews all add weight on their own, regardless of rating -- a
    liked-but-unrated film, or one rewatched three times, is a real signal
    even without stars attached."""
    weight = (logged.rating - 2.5) if logged.rating is not None else 0.0
    if logged.liked:
        weight += LIKE_BONUS
    if logged.times_logged > 1:
        weight += min(logged.times_logged - 1, MAX_BONUS_OCCURRENCES) * REWATCH_BONUS_PER_EXTRA
    if logged.times_reviewed > 1:
        weight += min(logged.times_reviewed - 1, MAX_BONUS_OCCURRENCES) * REREVIEW_BONUS_PER_EXTRA
    return weight


def build_profile(logged_details: list[tuple[LoggedFilm, dict]]) -> Profile:
    """Averages each trait's weight across the films that carry it, rather
    than summing. A genre you've merely watched a lot of (in volume) would
    otherwise outscore a genre you've rated consistently higher but seen in
    fewer films -- summing rewards volume, averaging rewards actual taste."""
    profile = Profile()
    genre_sum: dict[int, float] = defaultdict(float)
    genre_count: dict[int, int] = defaultdict(int)
    keyword_sum: dict[int, float] = defaultdict(float)
    keyword_count: dict[int, int] = defaultdict(int)
    person_sum: dict[int, float] = defaultdict(float)
    person_count: dict[int, int] = defaultdict(int)

    for logged, details in logged_details:
        if not details:
            continue
        weight = film_weight(logged)

        for gid in details["genres"]:
            genre_sum[gid] += weight
            genre_count[gid] += 1
            profile.genre_names[gid] = details["genre_names"][gid]
        for kid in details["keywords"]:
            keyword_sum[kid] += weight * 0.6
            keyword_count[kid] += 1
            profile.keyword_names[kid] = details["keyword_names"][kid]
        people = details["directors"] + details["cast"][:3]
        for p in people:
            person_w = weight * (1.2 if p in details["directors"] else 0.8)
            person_sum[p["id"]] += person_w
            person_count[p["id"]] += 1
            profile.person_names[p["id"]] = p["name"]

    profile.genre_weight = {g: genre_sum[g] / genre_count[g] for g in genre_sum}
    profile.keyword_weight = {k: keyword_sum[k] / keyword_count[k] for k in keyword_sum}
    profile.person_weight = {p: person_sum[p] / person_count[p] for p in person_sum}
    return profile


def _select_diverse_seeds(
    candidates: list[LoggedFilm],
    details_by_id: dict[int, dict],
    max_seeds: int,
    max_per_genre: int,
) -> list[LoggedFilm]:
    """Picks favorite films to seed candidate generation, capping how many
    seeds can share a genre so a handful of tied-weight titles in one genre
    (e.g. several 5-star animated films) can't crowd out other favorites
    that never get picked because the cap fills up on weight alone."""
    seeds: list[LoggedFilm] = []
    overflow: list[LoggedFilm] = []
    genre_counts: dict[int, int] = defaultdict(int)

    for f in candidates:
        genres = details_by_id.get(f.tmdb_id, {}).get("genres", [])
        if genres and any(genre_counts[g] >= max_per_genre for g in genres):
            overflow.append(f)
            continue
        seeds.append(f)
        for g in genres:
            genre_counts[g] += 1
        if len(seeds) >= max_seeds:
            return seeds

    for f in overflow:
        if len(seeds) >= max_seeds:
            break
        seeds.append(f)
    return seeds


def _diversify(
    results: list[Recommendation],
    top_n: int,
    max_per_genre: int,
    exempt_genres: set[int] = frozenset(),
) -> list[Recommendation]:
    """Caps how many of the final results can share a genre, so a tightly
    interconnected cluster on TMDB's graph (e.g. animated family films
    recommending each other) can't fill every slot at the expense of other
    genres the profile weighs just as highly. Genres the user explicitly
    asked to focus on are exempt from the cap -- that's the point of asking."""
    ranked = sorted(results, key=lambda r: r.score, reverse=True)
    chosen: list[Recommendation] = []
    overflow: list[Recommendation] = []
    genre_counts: dict[int, int] = defaultdict(int)

    for r in ranked:
        capped_genres = [g for g in r.genre_ids if g not in exempt_genres]
        if capped_genres and any(genre_counts[g] >= max_per_genre for g in capped_genres):
            overflow.append(r)
            continue
        chosen.append(r)
        for g in capped_genres:
            genre_counts[g] += 1
        if len(chosen) >= top_n:
            return chosen

    for r in overflow:
        if len(chosen) >= top_n:
            break
        chosen.append(r)
    return chosen


def _content_score(details: dict, profile: Profile) -> tuple[float, list[str]]:
    score = 0.0
    matched_genres = []
    for gid in details.get("genres", []):
        w = profile.genre_weight.get(gid, 0)
        if w:
            score += w
        if w > 0:
            matched_genres.append(profile.genre_names.get(gid, ""))
    for kid in details.get("keywords", []):
        score += profile.keyword_weight.get(kid, 0)
    for p in details.get("directors", []) + details.get("cast", []):
        score += profile.person_weight.get(p["id"], 0)
    return score, [g for g in matched_genres if g]


class WatchedFilter:
    """Excludes already-logged films from candidates by TMDB id primarily,
    but also by normalized title+year as a backup. A film scraped from the
    diary that TMDB search couldn't resolve to an id (or resolved wrongly)
    would otherwise have zero exclusion protection and could slip back in
    as a "recommendation" for something the user has already watched."""

    def __init__(self, logged_films: list[LoggedFilm]):
        self.ids = {f.tmdb_id for f in logged_films if f.tmdb_id}
        self.title_year_keys = {
            (normalize_title(f.title), f.year) for f in logged_films if f.title
        }

    def is_watched(self, item: dict) -> bool:
        if item.get("id") in self.ids:
            return True
        title = item.get("title") or item.get("name")
        if not title:
            return False
        year_str = (item.get("release_date") or "")[:4]
        year = int(year_str) if year_str.isdigit() else None
        return (normalize_title(title), year) in self.title_year_keys


def recommend(
    client: TMDBClient,
    logged_films: list[LoggedFilm],
    top_n: int = 20,
    preferred_genre_ids: list[int] | None = None,
    watchlist_keys: set[tuple[str, int | None]] | None = None,
    progress=None,
) -> list[Recommendation]:
    def report(msg: str):
        if progress:
            progress(msg)

    preferred_genre_ids = list(dict.fromkeys(preferred_genre_ids or []))[:MAX_PREFERRED_GENRES]
    preferred_set = set(preferred_genre_ids)
    watchlist_keys = watchlist_keys or set()

    with_ids = [f for f in logged_films if f.tmdb_id]
    watched = WatchedFilter(logged_films)

    report(f"Fetching details for {len(with_ids)} logged films...")
    client.prefetch_movie_details([f.tmdb_id for f in with_ids])
    logged_details = [(f, client.movie_details(f.tmdb_id)) for f in with_ids]
    details_by_id = {f.tmdb_id: d for f, d in logged_details}

    profile = build_profile(logged_details)
    if not profile.genre_weight:
        raise ValueError(
            "Couldn't build a taste profile -- no logged films with usable TMDB data were found."
        )

    favorite_pool = sorted(
        [f for f in with_ids if film_weight(f) >= FAVORITE_WEIGHT_THRESHOLD],
        key=film_weight,
        reverse=True,
    )
    if not favorite_pool:
        # Fall back to the highest-weighted films even if none clear the bar.
        favorite_pool = sorted(with_ids, key=film_weight, reverse=True)
    seeds = _select_diverse_seeds(favorite_pool, details_by_id, MAX_SEEDS, MAX_SEEDS_PER_GENRE)

    seed_titles = {f.tmdb_id: f"you liked {f.title} ({f.year})" for f in seeds}

    report(f"Gathering candidates from {len(seeds)} favorite films...")
    client.prefetch_related([s.tmdb_id for s in seeds])
    candidate_sources: dict[int, dict] = {}
    candidate_seed_score: dict[int, float] = defaultdict(float)
    candidate_because: dict[int, set[str]] = defaultdict(set)

    for seed in seeds:
        seed_weight = film_weight(seed)
        for kind in ("recommendations", "similar"):
            related = client.related(seed.tmdb_id, kind)[:CANDIDATES_PER_SEED]
            for rank, item in enumerate(related):
                cid = item["id"]
                if watched.is_watched(item):
                    continue
                candidate_sources[cid] = item
                position_decay = 1.0 / (1 + rank * 0.15)
                candidate_seed_score[cid] += seed_weight * position_decay
                candidate_because[cid].add(seed_titles[seed.tmdb_id])

    if preferred_genre_ids:
        report(f"Pulling extra picks for {len(preferred_genre_ids)} chosen genre(s)...")
        client.prefetch_discover(preferred_genre_ids)
        for gid in preferred_genre_ids:
            genre_label = f"you picked {GENRE_NAMES.get(gid, 'that genre')}"
            for item in client.discover_by_genre(gid):
                cid = item["id"]
                if watched.is_watched(item):
                    continue
                candidate_sources.setdefault(cid, item)
                candidate_because[cid].add(genre_label)

    report(f"Scoring {len(candidate_sources)} candidates...")
    client.prefetch_movie_details(list(candidate_sources.keys()))
    results: list[Recommendation] = []
    for cid, basic in candidate_sources.items():
        details = client.movie_details(cid)
        if not details:
            continue
        if watched.is_watched({"id": cid, "title": details["title"], "release_date": details["year"]}):
            continue  # belt-and-suspenders: full details can reveal a title/year match the summary missed
        if preferred_set and not preferred_set.intersection(details["genres"]):
            continue  # a genre focus is a hard filter, not just a nudge

        content_score, matched_genres = _content_score(details, profile)
        quality_floor = (details["vote_average"] - 5.5) * 0.15
        preferred_hits = len(preferred_set.intersection(details["genres"]))
        genre_pref_boost = preferred_hits * GENRE_PREFERENCE_BOOST
        total_score = candidate_seed_score[cid] * 2 + content_score + quality_floor + genre_pref_boost

        title = details["title"] or basic.get("title", "Unknown")
        year_str = details["year"] or ""
        year_int = int(year_str) if year_str.isdigit() else None
        on_watchlist = (normalize_title(title), year_int) in watchlist_keys

        results.append(
            Recommendation(
                tmdb_id=cid,
                title=title,
                year=year_str,
                overview=details["overview"],
                poster_url=client.poster_url(details["poster_path"]),
                vote_average=details["vote_average"],
                score=total_score,
                because_of=sorted(candidate_because[cid])[:3],
                matched_genres=matched_genres[:3],
                genre_ids=details["genres"],
                on_watchlist=on_watchlist,
            )
        )

    return _diversify(results, top_n, MAX_RESULTS_PER_GENRE, exempt_genres=preferred_set)
