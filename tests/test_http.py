import unittest
from unittest import mock

import requests

from swarm_scraper.http import FetchError, Fetcher, RobotsDisallowed


class FakeResp:
    def __init__(self, status, text="", headers=None, url="https://x.org/"):
        self.status_code, self.content, self.headers, self.url = status, text.encode(), headers or {}, url


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.headers = {}
        self.calls = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, headers))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch("swarm_scraper.http.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_retries_then_succeeds(self):
        s = FakeSession([requests.ConnectionError("boom"), FakeResp(503), FakeResp(200, "ok")])
        f = Fetcher(delay=0, retries=3, respect_robots=False, session=s)
        self.assertEqual(f.get("https://x.org/a").text, "ok")
        self.assertEqual(len(s.calls), 3)

    def test_ssl_error_not_retried(self):
        s = FakeSession([requests.exceptions.SSLError("unable to get local issuer certificate")] * 4)
        f = Fetcher(delay=0, retries=3, respect_robots=False, session=s)
        with self.assertRaises(FetchError) as cm:
            f.get("https://moos-ivp.org/")
        self.assertIn("SSL certificate", str(cm.exception))
        self.assertEqual(len(s.calls), 1)

    def test_get_ok_error_carries_status(self):
        f = Fetcher(delay=0, retries=0, respect_robots=False, session=FakeSession([FakeResp(403)]))
        with self.assertRaises(FetchError) as cm:
            f.get_ok("https://x.org/a")
        self.assertEqual(cm.exception.status, 403)

    def test_unreachable_host_fails_fast_afterwards(self):
        s = FakeSession([requests.Timeout("read timed out")] * 6 + [FakeResp(200, "other host")])
        f = Fetcher(delay=0, retries=2, session=s)
        with self.assertRaises(FetchError):  # robots.txt: 2 tries, then the page itself: fails fast
            f.get("https://dead.example/a")
        self.assertEqual(len(s.calls), 2)
        with self.assertRaises(FetchError) as cm:
            f.get("https://dead.example/b")
        self.assertIn("unreachable", str(cm.exception))
        self.assertEqual(len(s.calls), 2)

    def test_http_errors_do_not_mark_host_dead(self):
        s = FakeSession([FakeResp(503), FakeResp(503), FakeResp(200, "back")])
        f = Fetcher(delay=0, retries=1, respect_robots=False, session=s)
        with self.assertRaises(FetchError):
            f.get("https://busy.example/a")
        self.assertEqual(f.get("https://busy.example/a").text, "back")

    def test_gives_up(self):
        s = FakeSession([FakeResp(500)] * 3)
        f = Fetcher(delay=0, retries=2, respect_robots=False, session=s)
        with self.assertRaises(FetchError):
            f.get("https://x.org/a")

    def test_404_returned_not_retried(self):
        s = FakeSession([FakeResp(404)])
        f = Fetcher(delay=0, retries=3, respect_robots=False, session=s)
        self.assertEqual(f.get("https://x.org/a").status, 404)
        with self.assertRaises(FetchError):
            Fetcher(delay=0, retries=0, respect_robots=False, session=FakeSession([FakeResp(404)])).get_ok("https://x.org/a")

    def test_robots_respected(self):
        s = FakeSession([FakeResp(200, "User-agent: *\nDisallow: /private/"), FakeResp(200, "fine")])
        f = Fetcher(delay=0, retries=0, session=s)
        with self.assertRaises(RobotsDisallowed):
            f.get("https://x.org/private/page")
        self.assertEqual(f.get("https://x.org/public").text, "fine")  # robots.txt cached

    def test_github_token_only_sent_to_api(self):
        s = FakeSession([FakeResp(200, "{}"), FakeResp(200, "x")])
        f = Fetcher(delay=0, retries=0, respect_robots=False, github_token="tok", session=s)
        f.get("https://api.github.com/repos/a/b")
        f.get("https://raw.githubusercontent.com/a/b/main/README.md")
        self.assertEqual(s.calls[0][1]["Authorization"], "Bearer tok")
        self.assertNotIn("Authorization", s.calls[1][1])

    def test_per_host_delay(self):
        s = FakeSession([FakeResp(200), FakeResp(200)])
        f = Fetcher(delay=0, retries=0, respect_robots=False, session=s)
        f.get("https://arxiv.org/abs/1")
        f.get("https://arxiv.org/abs/2")
        self.assertTrue(any(c.args[0] > 2.5 for c in self.sleep.call_args_list))


if __name__ == "__main__":
    unittest.main()
