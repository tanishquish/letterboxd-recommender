# TODO

## Known bugs / gaps

- [ ] TV series logged as "films" on Letterboxd (confirmed on this account: `WandaVision`,
      `Pit Babe`, `KinnPorsche: The Series`) can't resolve to a TMDB movie id via
      `search_movie`, so they're silently dropped from the taste profile. They're still
      protected from being re-recommended (title+year fallback in `WatchedFilter`), but
      they contribute zero signal. Could add a `/search/tv` fallback and treat those
      separately, or at least surface a "couldn't match N titles" note on the results page.
- [ ] No visible progress indicator during the request. `recommend()` and the scraper both
      take a `progress` callback, but nothing in the Flask route wires it to the browser --
      the request just blocks silently. Worth adding (SSE or simple polling) since a cold
      run can still take several seconds.

## Ideas / improvements

- [ ] Cache a scraped diary snapshot per username (e.g. on disk with a short TTL) so
      re-running for the same profile doesn't always re-scrape all 8+ diary pages and
      re-resolve every TMDB id from scratch.
- [x] ~~Tag recommendations that are already on the user's Letterboxd watchlist~~ -- done:
      `scrape_profile()` now scrapes the watchlist alongside the diary, and matches surface
      in a separate "From Your Watchlist" column on the results page.
- [ ] Let the 30-diary-page scrape cap be configurable instead of a hardcoded constant in
      `letterboxd_scraper.py`, for accounts with a truly enormous logged history.
- [ ] Consider whether cast weighting should favor top-billed actors more heavily than it
      does now (currently top 3 cast + director, weighted 0.8x/1.2x respectively).

## Housekeeping

- [ ] No automated tests yet -- the recommender/scraper/tmdb logic has all been verified
      manually against a real profile throughout development, but there's no pytest suite
      to catch regressions.
- [ ] `app.run(debug=True)` is fine for local personal use but shouldn't be used as-is if
      this is ever exposed beyond localhost.
