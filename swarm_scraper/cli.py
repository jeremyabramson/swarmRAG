"""Command-line entry point: python -m swarm_scraper --help"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from .dispatch import process
from .handlers import Context
from .http import Fetcher
from .inventory import filter_records, load_records
from .output import OutputStore


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm_scraper", description=__doc__)
    p.add_argument("--inventory", default="inventory/swarm-technology-document-inventory.xlsx")
    p.add_argument("--out", default="corpus", help="output folder (default: corpus)")
    sel = p.add_argument_group("selection")
    sel.add_argument("--top3", action="store_true", help="only documents ranked in a technology's top 3")
    sel.add_argument("--max-priority", type=int, help="only documents with priority <= N (1 = core)")
    sel.add_argument("--tech", action="append", help="technology name contains this text (repeatable)")
    sel.add_argument("--ids", nargs="+", help="specific Doc IDs, e.g. D0001 D0042")
    sel.add_argument("--confirmed-only", action="store_true", help="skip URLs not seen in search results")
    sel.add_argument("--limit", type=int, help="stop after N documents")
    run = p.add_argument_group("run control")
    run.add_argument("--dry-run", action="store_true", help="list what would be fetched, fetch nothing")
    run.add_argument("--force", action="store_true", help="re-fetch documents already marked ok")
    run.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one host")
    run.add_argument("--max-pages", type=int, default=200, help="page cap per documentation-site crawl")
    run.add_argument("--max-repo-files", type=int, default=400, help="file cap per repository docs folder")
    run.add_argument("--no-pdf", action="store_true", help="do not keep original PDFs, only extracted text")
    run.add_argument("--ignore-robots", action="store_true", help="do not check robots.txt (not recommended)")
    return p


def main(argv: list[str] | None = None, fetcher: Fetcher | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = filter_records(load_records(args.inventory), top3_only=args.top3,
                             max_priority=args.max_priority, technologies=args.tech,
                             doc_ids=args.ids, confirmed_only=args.confirmed_only)
    store = OutputStore(args.out)
    if not args.force:
        done = store.completed_ids()
        records = [r for r in records if r.doc_id not in done]
    if args.limit:
        records = records[: args.limit]
    if args.dry_run:
        for r in records:
            print(f"{r.doc_id}\t{r.method}\t{r.technology}\t{r.url}")
        print(f"{len(records)} documents selected")
        return 0
    if not records:
        print("Nothing to do: no selected documents are pending (use --force to re-fetch).")
        return 0
    ctx = Context(fetcher=fetcher or Fetcher(delay=args.delay, respect_robots=not args.ignore_robots),
                  store=store, max_pages=args.max_pages, max_repo_files=args.max_repo_files,
                  keep_pdf=not args.no_pdf)
    tally = Counter()
    for i, r in enumerate(records, 1):
        print(f"[{i}/{len(records)}] {r.doc_id} {r.technology}: {r.title}", flush=True)
        result = process(r, ctx)
        store.record_result(result)
        tally[result.status] += 1
        if result.status != "ok":
            print(f"    -> {result.status}: {result.message.splitlines()[0] if result.message else ''}")
    print("Summary: " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))
    print(f"Manifest: {store.manifest_path}")
    if store.manual_path.exists():
        print(f"Manual collection queue: {store.manual_path}")
    return 1 if tally.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
