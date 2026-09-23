# Brief for Claude Code

This scraper was written offline, then live-tested against the real inventory.

## Known state
- Offline tests pass (`python -m unittest discover -s tests -t .`); install with `pip install -e ".[better,dev]"`
  so the PyMuPDF and trafilatura paths are exercised too.
- Live runs done: `--top3 --tech PX4 --tech MCAP --tech Aerostack2` (full depth), and all `--top3`
  documents with `--max-pages 40 --workers 8`. Results and fixes are in the git log; the
  "Live results" section below summarises what is left.
- Documents are fetched in parallel (`--workers`, default 4). The per-host delay is shared across
  workers, so parallelism only helps across hosts. GitHub API calls are serialized at 1/s.

## Live results (22 September 2026)
- First test (12 documents, sequential, before fixes): 6 min 38 s; PX4 docs capped at 200 pages.
- All 299 top-3 documents, `--workers 8 --max-pages 40`: about 13 min of active time;
  237 ok, 35 partial, 10 manual, 17 error. Re-running the 28 problem documents after the fixes
  and URL corrections: 1 error (investor.palladyneai.com times out), 7 manual, 9 ok, 11 partial.
- Remaining partials are the 40-page test cap, broken links on the sites themselves, and two
  JavaScript-rendered pages (anduril.com/fury, buf.build).
- Manual queue: DTIC, Medium, Raspberry Pi and Bitcraze docs (403), Skydio API docs (robots.txt),
  Robotarium and DJI Payload SDK (JavaScript-only), plus the rows marked Manual/Gated/Subscription.
- Seven stale inventory URLs were corrected in the workbook (Verified = "Checked live ..."; old
  URL kept in the notes): D0029, D0040, D0127, D0266, D0274, D0287, D0289.

## Suggested next runs
1. `python -m swarm_scraper --top3` at full depth (default `--max-pages 200`).
2. `--max-priority 2`, then the rest.
3. Re-run with `--force --ids ...` after fixing inventory URLs listed as errors in the manifest.

## Behaviour worth knowing
- Crawls resolve HTTP and meta-refresh redirects on the start URL and re-scope to where the docs
  now live (the message says "redirected"; files get `crawl_root`).
- Empty-shell pages (client-side redirects, JS apps) and duplicate bodies are skipped in crawls;
  a single empty-shell page is marked `partial`.
- 401/403 responses, robots.txt blocks, and JavaScript-only sites go to `manual_queue.csv`.
- A host that times out through every retry is skipped for the rest of the run.
- SSL verification errors are not retried (moos-ivp.org has an incomplete certificate chain).

## Things likely to need tuning
- `--max-pages` (default 200) versus very large sites (PX4 alone has 1000+ pages; ROS 2, Unreal, ns-3).
  Sitemap order decides which pages fall inside the cap.
- Versioned docs: the crawl is bounded to the folder of the (possibly redirected) start URL.
- JavaScript-rendered sites would need a headless browser; they are queued for manual collection.
- GitHub API limits: set GITHUB_TOKEN before large runs.
