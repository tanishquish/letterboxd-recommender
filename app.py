import os

from dotenv import load_dotenv
from flask import Flask, render_template, request

from lib.letterboxd import LetterboxdError, extract_username, fetch_diary
from lib.recommender import MAX_PREFERRED_GENRES, recommend
from lib.tmdb import GENRES, TMDBClient, TMDBError

load_dotenv()

app = Flask(__name__)


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
        films = fetch_diary(username)
        recs = recommend(client, films, preferred_genre_ids=selected_genre_ids)
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

    rated_count = sum(1 for f in films if f.rating is not None)
    return render_template(
        "results.html",
        username=username,
        total_logged=len(films),
        rated_count=rated_count,
        recs=recs,
        api_key=api_key,
        selected_genre_names=selected_genre_names,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5057)
