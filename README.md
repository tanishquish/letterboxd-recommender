# Letterboxd Recommender

Suggests films you haven't seen, based on a public Letterboxd profile's full logged history.

## How it works

1. Scrapes `letterboxd.com/<username>/diary/films/` with a real (headless)
   browser via Playwright, paging through your entire diary. Letterboxd's
   HTML pages sit behind Cloudflare bot protection that blocks plain HTTP
   requests, so a real browser is required to read anything beyond the RSS
   feed. Each diary row gives title, year, star rating, like status, rewatch
   status, and whether that entry had a written review. Falls back to the
   `letterboxd.com/<username>/rss/` feed (capped at your ~100 most recent
   activity items) if scraping fails for any reason -- Playwright not
   installed, Cloudflare escalating its challenge, etc.
2. Films found by scraping don't carry a TMDB id the way the RSS feed's do,
   so each one is matched to TMDB via a title+year search.
3. Every logged film -- not just rated ones -- feeds a weighted taste profile
   of genres/keywords/cast/director. The weight blends star rating, a flat
   bonus for the like feature (regardless of rating), and bonuses for films
   logged and/or reviewed more than once (rewatches and re-reviews are a
   stronger signal than a single viewing).
4. Pulls candidate films from TMDB's `recommendations` and `similar`
   endpoints, seeded by your highest-weighted favorite films. If you pick up
   to 3 genres to focus on, TMDB's `discover` endpoint also pulls in
   well-regarded films from those genres directly, and the final list is
   filtered down to only films in those genres.
5. Scores every candidate by blending TMDB's own ranking with how well it
   matches your taste profile, filters out anything already logged, and
   returns the top matches with a poster, TMDB rating, matched genres, and
   "because you liked ___" / "because you picked ___".

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
```

The last step downloads a headless Chromium build (~100-300MB) that
Playwright drives to scrape Letterboxd. It's a one-time download.

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
URL) and your TMDB key, optionally pick up to 3 genres to focus on, and get
recommendations. A full scrape of an active diary can take 30-90 seconds
(paging through the diary, then matching every film to TMDB) — this is
normal, not a hang.

## Notes / limitations

- The profile must be public (default for Letterboxd).
- The scraper is capped at 30 diary pages (~1500 entries) as a safety limit
  for extremely prolific loggers.
- A handful of titles logged as "films" on Letterboxd that are actually TV
  series (e.g. mislabeled anthology/limited series) won't resolve to a TMDB
  movie id and are silently skipped.
- Scraping is inherently a bit fragile: it depends on Letterboxd's current
  page structure and on Cloudflare continuing to let a headless browser
  through. If it breaks, the app automatically falls back to the RSS feed
  rather than failing outright — the results page tells you which source
  was used.
- Everything runs locally — no data is stored or sent anywhere besides
  Letterboxd and the TMDB API.
