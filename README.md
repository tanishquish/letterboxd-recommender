# Letterboxd Recommender

Suggests films you haven't seen, based on a public Letterboxd profile's rated films.

## How it works

1. Reads `letterboxd.com/<username>/rss/` — the public activity feed every
   Letterboxd profile publishes. It includes each film's title, year, your
   star rating, like status, and its TMDB id. (Letterboxd has no public API;
   this feed is the only structured, public data it exposes. It caps out at
   the ~100 most recent activity entries — no pagination — so the profile is
   built from recent activity, not full history.)
2. For each rated film, fetches genres/keywords/cast/director from TMDB and
   builds a weighted taste profile (higher ratings pull the profile toward
   those traits, low ratings push away from them).
3. Pulls candidate films from TMDB's `recommendations` and `similar`
   endpoints, seeded by your favorite (4★+) logged films.
4. Scores every candidate by blending TMDB's own ranking with how well it
   matches your taste profile, filters out anything already logged, and
   returns the top matches with a poster, TMDB rating, matched genres, and
   "because you liked ___".

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Get a free TMDB API key at https://www.themoviedb.org/settings/api (sign up,
request an API key under "Developer" use — approval is instant).

Either export it so you don't have to paste it each time:

```bash
export TMDB_API_KEY=your_key_here
```

or just paste it into the form on the page.

## Run

```bash
python3 app.py
```

Open http://127.0.0.1:5057, enter a public Letterboxd username (or profile
URL) and your TMDB key, and get recommendations.

## Notes / limitations

- The profile must be public (default for Letterboxd).
- Only films logged *with a star rating* feed the taste profile; unrated
  logs still count toward "already watched" exclusions.
- Everything runs locally — no data is stored or sent anywhere besides
  Letterboxd's RSS endpoint and the TMDB API.
