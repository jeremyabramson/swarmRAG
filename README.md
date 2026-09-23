# swarm-doc-scraper

Collects the documents listed in `inventory/swarm-technology-document-inventory.xlsx`
(the **Documents** tab) and saves them as Markdown files with a metadata header, ready to
split into chunks for a retrieval system.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[better,dev]"      # 'better' adds trafilatura and PyMuPDF for cleaner text
export GITHUB_TOKEN=...             # optional; raises the GitHub API limit from 60 to 5,000 requests per hour
export SCRAPER_CONTACT=you@example.com   # put in the User-Agent so site operators can reach you
```

## Run

```bash
python -m swarm_scraper --top3 --dry-run            # list the first-pass documents without fetching
python -m swarm_scraper --top3                       # fetch every technology's top 3 documents
python -m swarm_scraper --tech "PX4" --tech Zenoh    # one or more technologies
python -m swarm_scraper --ids D0001 D0042            # specific documents
python -m swarm_scraper --max-priority 2 --confirmed-only
```

Runs are resumable: documents already saved successfully are skipped unless you pass `--force`.

Documents are fetched in parallel (`--workers 4` by default, `--workers 1` for a sequential run).
The per-host delay is shared by all workers, so running in parallel speeds things up across sites
without sending more requests to any one site.

## Output

```
corpus/
  <technology>/<doc_id>-<title>.md         one document
  <technology>/<doc_id>-<title>/...md      multi-page documents (sites, docs folders, wikis)
  <technology>/<doc_id>-<title>.pdf        original PDF alongside its extracted text
  manifest.jsonl                           one line per attempt: status, files, message, time
  manual_queue.csv                         paywalled, gated, internal, or robots-blocked items
```

Each Markdown file starts with a YAML header: technology, doc ID, title, document type,
source and fetched URLs, scrape method, priority, top-3 rank, access, URL-verification
status, retrieval time, and a SHA-256 hash of the body. arXiv papers also get the title,
authors, publication date, and abstract.

## How each scrape method works

| Inventory value | What happens |
|---|---|
| GitHub README | GitHub API README endpoint (raw file for `blob` links); falls back to raw.githubusercontent.com |
| Git clone (docs folder) | GitHub tree API, then every .md/.mdx/.rst/.txt/.adoc file under the path (prefers `docs/` at repository root) |
| GitHub releases API | All release notes combined into one file |
| GitHub organization listing, then READMEs | Index table of repositories plus READMEs of the top non-archived ones by stars |
| GitHub wiki clone | `git clone <repo>.wiki.git` |
| Documentation-site crawl | `llms-full.txt`, then `llms.txt` links, then `sitemap.xml` within the URL's folder, then a link crawl bounded to that folder |
| arXiv paper | arXiv API metadata; HTML version when available, otherwise the PDF |
| Direct PDF | Download, keep the original, extract text (saves the page instead if the link now serves HTML) |
| Web page to markdown | Main content converted to Markdown; plain text kept as-is; GitHub `blob` links fetched raw |
| Manual collection | Added to `manual_queue.csv` |

Rows whose Access is Gated, Internal, or Subscription go to the manual queue whatever the method.

## Politeness

One request per second per host by default (three seconds for arXiv, per its guidance;
a quarter second for the raw.githubusercontent.com CDN), shared across parallel workers,
retries with backoff on 429 and 5xx responses, and robots.txt checks (skip with
`--ignore-robots` only where you have permission). Check each site's terms of use before
bulk collection, especially vendor documentation.

## Tests

```bash
python -m unittest discover -s tests -t .      # or: pytest
```

The tests run offline against a fake HTTP layer (`tests/helpers.py: FakeFetcher`) and the
real inventory workbook.
