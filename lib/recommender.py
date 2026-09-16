"""Builds a taste profile from a user's rated films and scores candidates.

Approach:
1. Every rated film contributes a signed weight (rating - 2.5) to a profile
   of genres / keywords / people (director + top cast). Films rated above
   ~3 stars pull the profile toward their traits; films rated low push away.
2. Candidates come from TMDB's own "recommendations" and "similar" endpoints,
   seeded from the user's favorite films (rating >= 4), since that's a much
   richer signal than genre-matching alone (it captures tone, era, style).
3. Each candidate is scored by blending TMDB's own ranking (it returned the
   candidate near the top of a favorite film's list) with how well the
   candidate's genres/keywords/people match the user's overall profile, then
   lightly floored by TMDB's average rating so obscure poorly-rated titles
   don't dominate just because they matched a keyword.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from .letterboxd import LoggedFilm
from .tmdb import GENRES, TMDBClient

FAVORITE_THRESHOLD = 4.0
LOW_RATING_THRESHOLD = 2.0
MAX_SEEDS = 20
MAX_SEEDS_PER_GENRE = 6
MAX_RESULTS_PER_GENRE = 5
CANDIDATES_PER_SEED = 12
MAX_PREFERRED_GENRES = 3
GENRE_PREFERENCE_BOOST = 10.0
GENRE_NAMES = dict(GENRES)


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


def build_profile(rated_details: list[tuple[LoggedFilm, dict]]) -> Profile:
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

    for logged, details in rated_details:
        if not details or logged.rating is None:
            continue
        weight = logged.rating - 2.5
        if logged.liked:
            weight += 0.5

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
    seeds can share a genre so a handful of tied-rating titles in one genre
    (e.g. several 5-star animated films) can't crowd out other favorites
    (e.g. 4.5-star titles) that never get picked because the cap fills up
    on rating alone."""
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


def recommend(
    client: TMDBClient,
    logged_films: list[LoggedFilm],
    top_n: int = 20,
    preferred_genre_ids: list[int] | None = None,
    progress=None,
) -> list[Recommendation]:
    def report(msg: str):
        if progress:
            progress(msg)

    preferred_genre_ids = list(dict.fromkeys(preferred_genre_ids or []))[:MAX_PREFERRED_GENRES]
    preferred_set = set(preferred_genre_ids)

    with_ids = [f for f in logged_films if f.tmdb_id]
    watched_ids = {f.tmdb_id for f in with_ids}

    rated = [f for f in with_ids if f.rating is not None]
    report(f"Fetching details for {len(rated)} rated films...")
    rated_details = [(f, client.movie_details(f.tmdb_id)) for f in rated]
    details_by_id = {f.tmdb_id: d for f, d in rated_details}

    profile = build_profile(rated_details)
    if not profile.genre_weight:
        raise ValueError(
            "Couldn't build a taste profile -- no rated films with usable TMDB data were found."
        )

    favorite_pool = sorted(
        [f for f in rated if f.rating >= FAVORITE_THRESHOLD],
        key=lambda f: (f.rating, f.liked),
        reverse=True,
    )
    if not favorite_pool:
        # Fall back to the highest-rated films even if none clear the bar.
        favorite_pool = sorted(rated, key=lambda f: f.rating or 0, reverse=True)
    seeds = _select_diverse_seeds(favorite_pool, details_by_id, MAX_SEEDS, MAX_SEEDS_PER_GENRE)

    seed_titles = {f.tmdb_id: f"you liked {f.title} ({f.year})" for f in seeds}

    report(f"Gathering candidates from {len(seeds)} favorite films...")
    candidate_sources: dict[int, dict] = {}
    candidate_seed_score: dict[int, float] = defaultdict(float)
    candidate_because: dict[int, set[str]] = defaultdict(set)

    for seed in seeds:
        seed_weight = (seed.rating - 2.5) + (0.5 if seed.liked else 0)
        for kind in ("recommendations", "similar"):
            related = client.related(seed.tmdb_id, kind)[:CANDIDATES_PER_SEED]
            for rank, item in enumerate(related):
                cid = item["id"]
                if cid in watched_ids:
                    continue
                candidate_sources[cid] = item
                position_decay = 1.0 / (1 + rank * 0.15)
                candidate_seed_score[cid] += seed_weight * position_decay
                candidate_because[cid].add(seed_titles[seed.tmdb_id])

    if preferred_genre_ids:
        report(f"Pulling extra picks for {len(preferred_genre_ids)} chosen genre(s)...")
        for gid in preferred_genre_ids:
            genre_label = f"you picked {GENRE_NAMES.get(gid, 'that genre')}"
            for item in client.discover_by_genre(gid):
                cid = item["id"]
                if cid in watched_ids:
                    continue
                candidate_sources.setdefault(cid, item)
                candidate_because[cid].add(genre_label)

    report(f"Scoring {len(candidate_sources)} candidates...")
    results: list[Recommendation] = []
    for cid, basic in candidate_sources.items():
        details = client.movie_details(cid)
        if not details:
            continue
        content_score, matched_genres = _content_score(details, profile)
        quality_floor = (details["vote_average"] - 5.5) * 0.15
        preferred_hits = len(preferred_set.intersection(details["genres"]))
        genre_pref_boost = preferred_hits * GENRE_PREFERENCE_BOOST
        total_score = candidate_seed_score[cid] * 2 + content_score + quality_floor + genre_pref_boost

        results.append(
            Recommendation(
                tmdb_id=cid,
                title=details["title"] or basic.get("title", "Unknown"),
                year=details["year"] or "",
                overview=details["overview"],
                poster_url=client.poster_url(details["poster_path"]),
                vote_average=details["vote_average"],
                score=total_score,
                because_of=sorted(candidate_because[cid])[:3],
                matched_genres=matched_genres[:3],
                genre_ids=details["genres"],
            )
        )

    return _diversify(results, top_n, MAX_RESULTS_PER_GENRE, exempt_genres=preferred_set)
