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
from .tmdb import TMDBClient

FAVORITE_THRESHOLD = 4.0
LOW_RATING_THRESHOLD = 2.0
MAX_SEEDS = 12
CANDIDATES_PER_SEED = 12


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


def build_profile(rated_details: list[tuple[LoggedFilm, dict]]) -> Profile:
    profile = Profile()
    for logged, details in rated_details:
        if not details or logged.rating is None:
            continue
        weight = logged.rating - 2.5
        if logged.liked:
            weight += 0.5

        for gid in details["genres"]:
            profile.genre_weight[gid] += weight
            profile.genre_names[gid] = details["genre_names"][gid]
        for kid in details["keywords"]:
            profile.keyword_weight[kid] += weight * 0.6
            profile.keyword_names[kid] = details["keyword_names"][kid]
        people = details["directors"] + details["cast"][:3]
        for p in people:
            person_w = weight * (1.2 if p in details["directors"] else 0.8)
            profile.person_weight[p["id"]] += person_w
            profile.person_names[p["id"]] = p["name"]
    return profile


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
    progress=None,
) -> list[Recommendation]:
    def report(msg: str):
        if progress:
            progress(msg)

    with_ids = [f for f in logged_films if f.tmdb_id]
    watched_ids = {f.tmdb_id for f in with_ids}

    rated = [f for f in with_ids if f.rating is not None]
    report(f"Fetching details for {len(rated)} rated films...")
    rated_details = [(f, client.movie_details(f.tmdb_id)) for f in rated]

    profile = build_profile(rated_details)
    if not profile.genre_weight:
        raise ValueError(
            "Couldn't build a taste profile -- no rated films with usable TMDB data were found."
        )

    seeds = sorted(
        [f for f in rated if f.rating >= FAVORITE_THRESHOLD],
        key=lambda f: (f.rating, f.liked),
        reverse=True,
    )[:MAX_SEEDS]
    if not seeds:
        # Fall back to the highest-rated films even if none clear the bar.
        seeds = sorted(rated, key=lambda f: f.rating or 0, reverse=True)[:MAX_SEEDS]

    seed_titles = {f.tmdb_id: f"{f.title} ({f.year})" for f in seeds}

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

    report(f"Scoring {len(candidate_sources)} candidates...")
    results: list[Recommendation] = []
    for cid, basic in candidate_sources.items():
        details = client.movie_details(cid)
        if not details:
            continue
        content_score, matched_genres = _content_score(details, profile)
        quality_floor = (details["vote_average"] - 5.5) * 0.15
        total_score = candidate_seed_score[cid] * 2 + content_score + quality_floor

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
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:top_n]
