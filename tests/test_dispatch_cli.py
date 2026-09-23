import csv
import json
import unittest
from pathlib import Path

from swarm_scraper import cli
from swarm_scraper.dispatch import process
from swarm_scraper.output import OutputStore
from tests.helpers import INVENTORY, FakeFetcher, context, html_page, record


class DispatchTests(unittest.TestCase):
    def test_manual_rows_queued(self):
        ctx, root = context(FakeFetcher())
        for r in [record("(internal)", "Technical documentation", doc_id="D1"),
                  record("https://ieeexplore.ieee.org/x.pdf", "Manual collection", doc_id="D2"),
                  record("https://dl.acm.org/x", "arXiv paper", doc_id="D3", access="Subscription (ACM)")]:
            self.assertEqual(process(r, ctx).status, "manual")
        with open(root / "manual_queue.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["doc_id"] for r in rows], ["D1", "D2", "D3"])

    def test_manual_queue_not_duplicated_on_rerun(self):
        ctx, root = context(FakeFetcher())
        r = record("https://x.org/p.pdf", "Manual collection", doc_id="D7")
        for _ in range(3):
            process(r, ctx)
        with open(root / "manual_queue.csv", encoding="utf-8") as fh:
            self.assertEqual([row["doc_id"] for row in csv.DictReader(fh)], ["D7"])

    def test_robots_disallowed_goes_to_manual(self):
        ctx, root = context(FakeFetcher(disallow=("https://blocked.org",)))
        res = process(record("https://blocked.org/page", "Web page to markdown"), ctx)
        self.assertEqual(res.status, "manual")

    def test_forbidden_goes_to_manual(self):
        f = FakeFetcher()
        f.add("https://apps.dtic.mil/sti/tr/pdf/X.pdf", "Access Denied", status=403, ctype="text/html")
        ctx, root = context(f)
        res = process(record("https://apps.dtic.mil/sti/tr/pdf/X.pdf", "Direct PDF", doc_id="D5"), ctx)
        self.assertEqual(res.status, "manual")
        self.assertIn("403", res.message)
        with open(root / "manual_queue.csv", encoding="utf-8") as fh:
            self.assertEqual([r["doc_id"] for r in csv.DictReader(fh)], ["D5"])

    def test_unknown_method_and_http_errors(self):
        ctx, _ = context(FakeFetcher())
        self.assertIn("Unknown", process(record("https://x.org", "Carrier pigeon"), ctx).message)
        res = process(record("https://x.org/missing", "Web page to markdown"), ctx)
        self.assertEqual(res.status, "error")
        self.assertIn("404", res.message)

    def test_unexpected_exception_contained(self):
        f = FakeFetcher()
        f.add("https://x.org/p", "raise", status="raise")
        ctx, _ = context(f)
        self.assertEqual(process(record("https://x.org/p", "Web page to markdown"), ctx).status, "error")


class CliTests(unittest.TestCase):
    def test_load_dotenv(self):
        import os
        import tempfile
        from unittest import mock
        path = Path(tempfile.mkdtemp()) / ".env"
        path.write_text("# comment\nSWARM_TEST_A=one\nSWARM_TEST_B='two'\nSWARM_TEST_C=from-file\n")
        with mock.patch.dict(os.environ, {"SWARM_TEST_C": "from-env"}):
            cli.load_dotenv(str(path))
            self.assertEqual((os.environ["SWARM_TEST_A"], os.environ["SWARM_TEST_B"]), ("one", "two"))
            self.assertEqual(os.environ["SWARM_TEST_C"], "from-env")  # real environment wins
        cli.load_dotenv(str(path.parent / "missing.env"))  # absent file is fine

    def test_dry_run_on_real_inventory(self):
        out = Path(context(FakeFetcher())[1])
        code = cli.main(["--inventory", str(INVENTORY), "--out", str(out), "--top3", "--tech", "PX4 Autopilot",
                         "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse((out / "manifest.jsonl").exists())

    def test_run_and_resume(self):
        f = FakeFetcher()
        # D-ids for MCAP product page and blog; serve both as HTML
        from swarm_scraper.inventory import filter_records, load_records
        recs = filter_records(load_records(INVENTORY), technologies=["MCAP"])
        pages = [r for r in recs if r.method == "Web page to markdown"]
        self.assertTrue(pages)
        for r in pages:
            f.add(r.url, html_page(r.title))
        out = Path(context(f)[1])
        ids = [r.doc_id for r in pages]
        args = ["--inventory", str(INVENTORY), "--out", str(out), "--ids", *ids, "--delay", "0"]
        self.assertEqual(cli.main(args, fetcher=f), 0)
        lines = [json.loads(l) for l in (out / "manifest.jsonl").read_text().splitlines()]
        self.assertEqual({l["status"] for l in lines}, {"ok"})
        self.assertEqual(OutputStore(out).completed_ids(), set(ids))
        # second run skips completed documents
        f.requested.clear()
        self.assertEqual(cli.main(args, fetcher=f), 0)
        self.assertEqual(f.requested, [])


if __name__ == "__main__":
    unittest.main()
