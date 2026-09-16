import os

from dotenv import load_dotenv
from flask import Flask, render_template, request

from lib.letterboxd import LetterboxdError, extract_username, fetch_diary
from lib.recommender import recommend
from lib.tmdb import TMDBClient, TMDBError

load_dotenv()

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", default_key=os.environ.get("TMDB_API_KEY", ""))


@app.route("/recommend", methods=["POST"])
def recommend_route():
    username_input = request.form.get("username", "").strip()
    api_key = request.form.get("api_key", "").strip() or os.environ.get("TMDB_API_KEY", "")

    if not username_input:
        return render_template("index.html", error="Enter a Letterboxd username or profile URL.", default_key=api_key)
    if not api_key:
        return render_template("index.html", error="A TMDB API key is required.", default_key=api_key, username=username_input)

    username = extract_username(username_input)
    client = TMDBClient(api_key)

    try:
        if not client.verify_key():
            raise TMDBError("That TMDB API key doesn't seem to be valid.")
        films = fetch_diary(username)
        recs = recommend(client, films)
    except (LetterboxdError, TMDBError, ValueError) as e:
        return render_template("index.html", error=str(e), default_key=api_key, username=username_input)

    rated_count = sum(1 for f in films if f.rating is not None)
    return render_template(
        "results.html",
        username=username,
        total_logged=len(films),
        rated_count=rated_count,
        recs=recs,
        api_key=api_key,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5057)
