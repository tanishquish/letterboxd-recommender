import os

from dotenv import load_dotenv
from flask import Flask, render_template, request

from lib.letterboxd import LetterboxdError, extract_username, fetch_diary
from lib.letterboxd_scraper import resolve_tmdb_ids, scrape_profile
from lib.recommender import MAX_PREFERRED_GENRES, recommend
from lib.tmdb import GENRES, TMDBClient, TMDBError

load_dotenv()

app = Flask(__name__)


def get_logged_films(username: str, client: TMDBClient):
    """Scrapes the full diary and watchlist (a real headless browser, since
    Letterboxd's HTML pages are behind Cloudflare bot protection plain HTTP
    can't pass). Falls back to the ~100-item RSS feed (with no watchlist
    data) if scraping fails for any reason -- a blocked/crashed browser, a
    Playwright install issue, etc. -- so a scraper hiccup doesn't take down
    the whole app."""
    try:
        films, watchlist_keys = scrape_profile(username)
        resolve_tmdb_ids(films, client)
        return films, watchlist_keys, "scraped"
    except Exception:
        # Scraping can fail for reasons unrelated to whether the profile is
        # actually public -- a mid-scrape Cloudflare block, a Playwright/
        # browser issue, etc. -- so always give the RSS feed a shot before
        # giving up; its own error message is used if that fails too.
        return fetch_diary(username), set(), "rss"


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html", default_key=os.environ.get("TMDB_API_KEY", ""), genres=GENRES
    )


@app.route("/recommend", methods=["POST"])
def recommend_route():
    username_input = request.form.get("username", "").strip()
    api_key = request.form.get("api_key", "").strip() or os.environ.get("TMDB_API_KEY", "")
    genre_ids_raw = request.form.get("genre_ids", "").strip()
    selected_genre_ids = [int(g) for g in genre_ids_raw.split(",") if g][:MAX_PREFERRED_GENRES]

    if not username_input:
        return render_template("index.html", error="Enter a Letterboxd username or profile URL.", default_key=api_key, genres=GENRES)
    if not api_key:
        return render_template("index.html", error="A TMDB API key is required.", default_key=api_key, username=username_input, genres=GENRES)

    username = extract_username(username_input)
    client = TMDBClient(api_key)

    try:
        if not client.verify_key():
            raise TMDBError("That TMDB API key doesn't seem to be valid.")
        films, watchlist_keys, source = get_logged_films(username, client)
        recs = recommend(
            client, films, preferred_genre_ids=selected_genre_ids, watchlist_keys=watchlist_keys
        )
    except (LetterboxdError, TMDBError, ValueError) as e:
        return render_template(
            "index.html",
            error=str(e),
            default_key=api_key,
            username=username_input,
            genres=GENRES,
            selected_genre_ids=selected_genre_ids,
        )

    genre_names = dict(GENRES)
    selected_genre_names = [genre_names[g] for g in selected_genre_ids if g in genre_names]
    watchlist_recs = [r for r in recs if r.on_watchlist]
    general_recs = [r for r in recs if not r.on_watchlist]

    return render_template(
        "results.html",
        username=username,
        total_logged=len(films),
        general_recs=general_recs,
        watchlist_recs=watchlist_recs,
        api_key=api_key,
        selected_genre_names=selected_genre_names,
        source=source,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5057)
