# Brief for Claude Code

This scraper was written and unit-tested offline (no network). The next job is live testing.

## Known state
- 53 offline tests pass (`python -m unittest discover -s tests -t .`).
- Every inventory row was run through the dispatcher offline with all URLs returning 404; all
  failures were clean error messages. Live behaviour is untested.

## Suggested live-test order
1. `python -m swarm_scraper --top3 --tech PX4 --tech MCAP --tech Aerostack2` — covers README,
   site crawl, docs folder, arXiv, web page.
2. Inspect `corpus/manifest.jsonl` and a few output files for extraction quality.
3. Then `--top3` for everything; then `--max-priority 2`; then the rest.

## Things likely to need tuning once live
- Documentation sites that render with JavaScript (thin pages are marked `partial`).
- Sitemap scoping for versioned docs (PX4, Isaac Sim, ROS 2): the crawl is bounded to the
  folder of the inventory URL, so check the URL points at one version.
- `--max-pages` (default 200) versus very large sites (ROS 2 docs, Unreal Engine, ns-3).
- GitHub API limits: set GITHUB_TOKEN before large runs.
- ~95 inventory URLs are unconfirmed (see the Verified column); expect some 404s there.
- Concurrency across hosts is not implemented yet; runs are sequential.
