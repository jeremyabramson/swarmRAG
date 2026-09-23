"""Parallel runs: per-host politeness across threads, and CLI workers."""
import json
import threading
import time
import unittest
from pathlib import Path

from swarm_scraper import cli
from swarm_scraper.http import Fetcher, Response
from tests.helpers import INVENTORY, FakeFetcher, context, html_page, record


class TimedSession:
    """requests.Session stand-in that records (host, time) for every GET."""

    def __init__(self, latency=0.0):
        self.headers = {}
        self.latency = latency
        self.hits: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        with self._lock:
            self.hits.append((url, time.monotonic()))
        time.sleep(self.latency)

        class R:
            status_code, content, headers = 200, b"ok", {"Content-Type": "text/plain"}
        R.url = url
        return R


class FetcherThreadingTests(unittest.TestCase):
    def _run_threads(self, fetcher, urls):
        threads = [threading.Thread(target=fetcher.get, args=(u,)) for u in urls]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def test_same_host_spaced_across_threads(self):
        s = TimedSession()
        f = Fetcher(delay=0.1, retries=0, respect_robots=False, session=s)
        self._run_threads(f, [f"https://a.org/{i}" for i in range(5)])
        times = sorted(t for _, t in s.hits)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertEqual(len(times), 5)
        self.assertTrue(all(g >= 0.09 for g in gaps), gaps)

    def test_other_hosts_not_blocked(self):
        s = TimedSession()
        f = Fetcher(delay=0.3, retries=0, respect_robots=False, session=s)
        t0 = time.monotonic()
        self._run_threads(f, [f"https://h{i}.org/" for i in range(6)])
        self.assertLess(time.monotonic() - t0, 0.25)  # six hosts, no waiting on each other

    def test_robots_fetched_once_per_origin(self):
        s = TimedSession(latency=0.05)
        f = Fetcher(delay=0, retries=0, session=s)
        self._run_threads(f, [f"https://a.org/p{i}" for i in range(6)])
        robots = [u for u, _ in s.hits if u.endswith("/robots.txt")]
        self.assertEqual(robots, ["https://a.org/robots.txt"])

    def test_raw_github_faster_than_default(self):
        f = Fetcher(delay=1.0, respect_robots=False, session=TimedSession())
        self.assertLess(f.host_delays["raw.githubusercontent.com"], 1.0)
        self.assertEqual(Fetcher(delay=0, session=TimedSession()).host_delays["raw.githubusercontent.com"], 0)


class SlowFakeFetcher(FakeFetcher):
    """FakeFetcher with network-like latency and the real per-host spacing."""

    def __init__(self, latency, delay, **kw):
        super().__init__(**kw)
        self.latency, self.delay = latency, delay
        self._lock = threading.Lock()

    def _raw_get(self, url, headers=None, check_robots=True):
        from urllib.parse import urlparse
        self._wait(urlparse(url).netloc)
        time.sleep(self.latency)
        with self._lock:
            return super()._raw_get(url, headers, check_robots)


class CliWorkersTests(unittest.TestCase):
    def test_interleave_by_host(self):
        rs = [record(u, "Web page to markdown", doc_id=str(i)) for i, u in enumerate(
            ["https://a.org/1", "https://a.org/2", "https://a.org/3", "https://b.org/1", "https://c.org/1"])]
        order = [urlparse_host(r.url) for r in cli.interleave_by_host(rs)]
        self.assertEqual(order, ["a.org", "b.org", "c.org", "a.org", "a.org"])
        self.assertEqual(sorted(r.doc_id for r in cli.interleave_by_host(rs)), ["0", "1", "2", "3", "4"])

    def _pages_fetcher(self, latency=0.0, delay=0.0):
        from swarm_scraper.inventory import load_records
        recs = [r for r in load_records(INVENTORY) if r.method == "Web page to markdown"
                and r.access == "Public" and r.has_url and "github.com" not in r.url][:12]
        f = SlowFakeFetcher(latency, delay)
        for r in recs:
            f.add(r.url, html_page(r.title))
        return f, [r.doc_id for r in recs], recs

    def test_parallel_matches_sequential(self):
        results = {}
        for workers in (1, 4):
            f, ids, _ = self._pages_fetcher()
            out = Path(context(f)[1])
            code = cli.main(["--inventory", str(INVENTORY), "--out", str(out), "--ids", *ids,
                             "--workers", str(workers)], fetcher=f)
            self.assertEqual(code, 0)
            lines = [json.loads(l) for l in (out / "manifest.jsonl").read_text().splitlines()]
            results[workers] = {l["doc_id"]: (l["status"], [Path(p).name for p in l["files"]]) for l in lines}
        self.assertEqual(results[1], results[4])
        self.assertEqual(set(results[4]), set(ids))

    def test_parallel_is_faster_across_hosts(self):
        f, ids, recs = self._pages_fetcher(latency=0.05, delay=0.05)
        hosts = {urlparse_host(r.url) for r in recs}
        self.assertGreater(len(hosts), 4)
        timings = {}
        for workers in (1, 6):
            f, ids, _ = self._pages_fetcher(latency=0.05, delay=0.05)
            out = Path(context(f)[1])
            t0 = time.monotonic()
            cli.main(["--inventory", str(INVENTORY), "--out", str(out), "--ids", *ids,
                      "--workers", str(workers)], fetcher=f)
            timings[workers] = time.monotonic() - t0
        self.assertLess(timings[6], timings[1] * 0.6, timings)


def urlparse_host(url):
    from urllib.parse import urlparse
    return urlparse(url).netloc


if __name__ == "__main__":
    unittest.main()
