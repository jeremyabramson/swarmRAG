"""HTTP fetching with per-host rate limiting, retries, and robots.txt checks.

Everything that touches the network goes through a Fetcher so tests can
substitute a fake one.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

DEFAULT_USER_AGENT = "swarm-doc-scraper/0.1 (research document collection; contact: set SCRAPER_CONTACT)"


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RobotsDisallowed(FetchError):
    pass


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        for k, v in self.headers.items():
            if k.lower() == "content-type":
                return v.split(";")[0].strip().lower()
        return ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        import json
        return json.loads(self.text)


class Fetcher:
    """Polite HTTP client.

    - waits at least `delay` seconds between requests to the same host, across threads
    - retries on connection errors, 429, and 5xx with exponential backoff
    - gives up on a host for the rest of the run once it times out or refuses connections
      through every retry, so one dead site costs one timeout cycle rather than one per page
    - honours robots.txt unless `respect_robots` is False
    - sends a GitHub token (GITHUB_TOKEN) to api.github.com if present
    """

    def __init__(self, delay: float = 1.0, retries: int = 3, timeout: float | tuple = (10.0, 30.0),
                 respect_robots: bool = True, user_agent: str | None = None,
                 github_token: str | None = None, session: requests.Session | None = None):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.respect_robots = respect_robots
        contact = os.environ.get("SCRAPER_CONTACT")
        ua = user_agent or DEFAULT_USER_AGENT
        if contact:
            ua = ua.replace("set SCRAPER_CONTACT", contact)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = ua
        self.github_token = github_token if github_token is not None else os.environ.get("GITHUB_TOKEN")
        self._next_slot: dict[str, float] = {}
        # hosts that publish stricter crawl delays (arXiv), or CDNs built for bulk reads
        self.host_delays = {"arxiv.org": 3.0, "export.arxiv.org": 3.0,
                            "raw.githubusercontent.com": min(delay, 0.25)}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._slot_lock = threading.Lock()
        self._robots_locks: dict[str, threading.Lock] = {}
        self._dead_hosts: dict[str, str] = {}

    # -- robots ---------------------------------------------------------
    def _robots_for(self, url: str):
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._slot_lock:
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:  # one robots.txt fetch per origin even with several threads
            if origin not in self._robots:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    resp = self._raw_get(origin + "/robots.txt", check_robots=False, retries=min(1, self.retries))
                    if resp.status == 200:
                        rp.parse(resp.text.splitlines())
                    else:
                        rp = None  # no robots file: allowed
                except FetchError:
                    rp = None
                self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = urlparse(url).netloc
        if host in ("api.github.com", "raw.githubusercontent.com", "export.arxiv.org"):
            return True  # APIs with their own usage policies
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(self.session.headers["User-Agent"], url)

    # -- fetching -------------------------------------------------------
    def _wait(self, host: str):
        """Reserve the next request slot for `host`, then sleep until it arrives.

        Slots are handed out under a lock, so concurrent threads hitting one host
        queue up `delay` seconds apart while threads on other hosts are not blocked.
        """
        wanted = self.host_delays.get(host, self.delay)
        with self._slot_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + wanted
        if slot > now:
            time.sleep(slot - now)

    def _raw_get(self, url: str, headers: dict | None = None, check_robots: bool = True,
                 retries: int | None = None) -> Response:
        host = urlparse(url).netloc
        if host in self._dead_hosts:
            raise FetchError(f"Failed to fetch {url}: {host} unreachable earlier in this run ({self._dead_hosts[host]})")
        if check_robots and not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        if host in self._dead_hosts:  # robots.txt fetch just found it dead
            raise FetchError(f"Failed to fetch {url}: {host} unreachable ({self._dead_hosts[host]})")
        retries = self.retries if retries is None else retries
        hdrs = dict(headers or {})
        if host == "api.github.com" and self.github_token:
            hdrs.setdefault("Authorization", f"Bearer {self.github_token}")
        last_exc = None
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                r = self.session.get(url, headers=hdrs, timeout=self.timeout, allow_redirects=True)
            except requests.exceptions.SSLError as exc:  # a broken certificate chain will not fix itself
                raise FetchError(f"SSL certificate verification failed for {url}: {exc}") from exc
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last_exc = FetchError(f"HTTP {r.status_code} for {url}")
                    retry_after = r.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        time.sleep(min(int(retry_after), 120))
                else:
                    return Response(url=r.url, status=r.status_code, content=r.content, headers=dict(r.headers))
            if attempt < retries:
                time.sleep(2 ** attempt)
        if isinstance(last_exc, (requests.Timeout, requests.ConnectionError)):
            with self._slot_lock:
                self._dead_hosts[host] = type(last_exc).__name__
        raise FetchError(f"Failed to fetch {url}: {last_exc}")

    def get(self, url: str, headers: dict | None = None) -> Response:
        return self._raw_get(url, headers=headers)

    def get_ok(self, url: str, headers: dict | None = None) -> Response:
        resp = self.get(url, headers=headers)
        if not resp.ok:
            raise FetchError(f"HTTP {resp.status} for {url}", status=resp.status)
        return resp
