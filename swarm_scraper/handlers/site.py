"""Documentation-site crawl: llms-full.txt, then llms.txt, then sitemap, then link crawl."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from ..convert import html_title, html_to_markdown, is_empty_shell, meta_refresh_url
from ..http import FetchError, RobotsDisallowed
from ..output import DocResult, now_iso, slugify
from . import Context

SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".zip", ".gz",
            ".tar", ".mp4", ".webm", ".woff", ".woff2", ".ttf", ".json", ".xml", ".pdf")
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
# Generated pages with no prose: Sphinx source viewers, indexes, search, and asset folders
# and site furniture (forums, logins, Cloudflare endpoints)
SKIP_PATH = re.compile(r"/(_modules|_sources|_static|_images|_downloads)/|/(genindex|py-modindex|search)(\.html)?$"
                       r"|/(forums?|cdn-cgi|login|signin|signup|register|wp-admin|wp-json|feed)(/|$)", re.I)
# Wiki and CMS views of a page that are not the page itself (edit, history, diff, print)
SKIP_QUERY = re.compile(r"(^|&)(action=(?!view)|oldid=|diff=|printable=|do=|redirect=no|share=|replytocom=)", re.I)
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src")


def scope_prefix(url: str) -> str:
    """Directory-level prefix that bounds the crawl.

    https://docs.x.org/main/en/        -> https://docs.x.org/main/en/
    https://docs.x.org/main/en/a.html  -> https://docs.x.org/main/en/
    https://x.github.io/proj            -> https://x.github.io/proj/
    """
    url = urldefrag(url)[0].split("?")[0]
    p = urlparse(url)
    path = p.path or "/"
    if not path.endswith("/"):
        last = PurePosixPath(path).name
        path = path.rsplit("/", 1)[0] + "/" if "." in last else path + "/"
    return f"{p.scheme}://{p.netloc}{path}"


def normalize(url: str) -> str:
    """Drop the fragment and tracking parameters; keep (sorted) query strings that select a page."""
    url = urldefrag(url)[0]
    p = urlparse(url)
    if not p.query:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith(TRACKING_PARAMS)]
    return p._replace(query=urlencode(sorted(q))).geturl()


def in_scope(url: str, prefix: str) -> bool:
    url = normalize(url)
    p = urlparse(url)
    return (url.startswith(prefix) and not p.path.lower().endswith(SKIP_EXT)
            and not SKIP_PATH.search(p.path) and not SKIP_QUERY.search(p.query))


def page_filename(url: str, prefix: str) -> str:
    rel, _, query = normalize(url)[len(prefix):].partition("?")
    rel = re.sub(r"\.(html?|php|aspx)$", "", rel.strip("/")) or "index"
    segs = [slugify(seg, 80) for seg in rel.split("/")]
    if query:  # pmwiki.php?n=Main.Page and friends: the query names the page
        segs[-1] += "--" + slugify(query, 80)
    return "/".join(segs) + ".md"


def looks_js_rendered(html: str) -> bool:
    """A page whose body has no text and no links: the content is built by JavaScript."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.body
    if body is None:
        return True
    for tag in body(["script", "style", "noscript"]):
        tag.decompose()
    return len(body.get_text(strip=True)) < 50 and not body.find("a", href=True)


class SiteCrawler:
    def __init__(self, record, ctx: Context):
        self.record, self.ctx = record, ctx
        self._set_start(record.url)
        self.out_dir = ctx.store.base_path(record)
        self.written: list[str] = []
        self.failed_urls: list[str] = []
        self.skipped = 0            # empty shells and duplicate bodies
        self.capped = False         # stopped at max_pages with pages left
        self._hashes: set[str] = set()
        self.redirected_from = ""
        self.start_problem = ""     # why the start page itself gave nothing, for the error message

    def _set_start(self, url: str) -> None:
        self.start = url
        self.prefix = scope_prefix(url)
        p = urlparse(url)
        self.origin = f"{p.scheme}://{p.netloc}"

    def resolve_start(self) -> None:
        """Follow HTTP and meta-refresh redirects from the inventory URL and re-scope the crawl.

        Documentation often moves (github.io -> custom domain, / -> /main/); crawling the old
        scope then finds nothing but a "Redirecting..." stub.
        """
        url = self.start
        for _ in range(3):
            try:
                resp = self.ctx.fetcher.get(url)  # RobotsDisallowed propagates: dispatch queues it
            except RobotsDisallowed:
                raise
            except FetchError as exc:
                self.start_problem = str(exc)
                return
            if resp.status in (401, 403):  # bot protection: dispatch queues it for manual collection
                raise FetchError(f"HTTP {resp.status} for {url}", status=resp.status)
            if not resp.ok:
                self.start_problem = f"start page returned HTTP {resp.status}"
                return
            final = resp.url
            if "html" in resp.content_type:
                refresh = meta_refresh_url(resp.text, final)
                if refresh:
                    final = refresh
                elif looks_js_rendered(resp.text):
                    self.start_problem = "start page has no text or links (JavaScript-rendered site)"
            if normalize(final) == normalize(url):
                break
            url = final
        if normalize(url) != normalize(self.start):
            self.redirected_from = self.start
            self._set_start(url)

    def _meta(self, **extra):
        m = self.record.metadata()
        m["retrieved_at"] = now_iso()
        if self.redirected_from:
            m["crawl_root"] = self.start
        m.update(extra)
        return m

    def _try_get(self, url):
        try:
            resp = self.ctx.fetcher.get(url)
            return resp if resp.ok else None
        except FetchError:
            return None

    # -- strategies -------------------------------------------------------
    def llms_full(self) -> bool:
        for cand in dict.fromkeys([self.prefix + "llms-full.txt", self.origin + "/llms-full.txt"]):
            resp = self._try_get(cand)
            if resp and "html" not in resp.content_type and len(resp.content) > 1000:
                path = self.ctx.store.write_markdown(self.out_dir / "llms-full.md", resp.text,
                                                     self._meta(fetched_url=cand, strategy="llms-full.txt"))
                self.written.append(str(path))
                return True
        return False

    def llms_index(self) -> list[str]:
        cands = [self.start] if self.start.endswith("llms.txt") else []
        cands += [self.prefix + "llms.txt", self.origin + "/llms.txt"]
        for cand in dict.fromkeys(cands):
            resp = self._try_get(cand)
            if resp and "html" not in resp.content_type:
                links = [normalize(u) for u in MD_LINK.findall(resp.text)]
                host = urlparse(self.origin).netloc
                links = [u for u in links if urlparse(u).netloc == host]
                if cand == self.origin + "/llms.txt" and self.prefix != self.origin + "/":
                    # a site-wide index (dev.epicgames.com/llms.txt) mostly points outside the docs folder
                    links = [u for u in links if in_scope(u, self.prefix)]
                if links:
                    self.ctx.store.write_markdown(self.out_dir / "_llms-index.md", resp.text,
                                                  self._meta(fetched_url=cand, strategy="llms.txt"))
                    return list(dict.fromkeys(links))
        return []

    def sitemap(self) -> list[str]:
        urls, queue, seen = [], deque([self.origin + "/sitemap.xml"]), set()
        while queue and len(seen) < 20:
            sm = queue.popleft()
            if sm in seen:
                continue
            seen.add(sm)
            resp = self._try_get(sm)
            if not resp:
                continue
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError:
                continue
            if root.tag.endswith("sitemapindex"):
                queue.extend(e.text.strip() for e in root.iter(f"{SITEMAP_NS}loc") if e.text)
            else:
                urls.extend(e.text.strip() for e in root.iter(f"{SITEMAP_NS}loc") if e.text)
        return list(dict.fromkeys(u for u in map(normalize, urls) if in_scope(u, self.prefix)))

    # -- page handling ----------------------------------------------------
    def save_page(self, url: str, resp) -> None:
        name = page_filename(url, self.prefix)
        if "html" in resp.content_type:
            if is_empty_shell(resp.text) or (len(resp.content) < 5000 and meta_refresh_url(resp.text, url)):
                self.skipped += 1
                return
            body = html_to_markdown(resp.text, url=url)
            meta = self._meta(fetched_url=url, page_title=html_title(resp.text), source_format="html")
        else:
            body = resp.text
            meta = self._meta(fetched_url=url, source_format=resp.content_type or "text")
        if len(body.strip()) < 50:
            return
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest in self._hashes:  # same page under another URL (aliases, trailing index.html)
            self.skipped += 1
            return
        self._hashes.add(digest)
        self.written.append(str(self.ctx.store.write_markdown(self.out_dir / name, body, meta)))

    def fetch_list(self, urls: list[str], strategy: str) -> None:
        self.capped = len(urls) > self.ctx.max_pages
        for url in urls[: self.ctx.max_pages]:
            resp = self._try_get(url)
            if resp is None:
                self.failed_urls.append(url)
                continue
            self.save_page(url, resp)
        self.strategy = strategy

    def bfs(self) -> None:
        queue, seen = deque([normalize(self.start)]), {normalize(self.start)}
        fetched = 0
        while queue and fetched < self.ctx.max_pages:
            url = queue.popleft()
            resp = self._try_get(url)
            fetched += 1
            if resp is None:
                self.failed_urls.append(url)
                continue
            if "html" not in resp.content_type:
                self.save_page(url, resp)
                continue
            self.save_page(url, resp)
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                nxt = normalize(urljoin(resp.url, a["href"]))
                if nxt not in seen and in_scope(nxt, self.prefix):
                    seen.add(nxt)
                    queue.append(nxt)
        self.capped = bool(queue)
        self.strategy = "link crawl"

    def run(self) -> DocResult:
        self.strategy = ""
        self.resolve_start()
        if self.llms_full():
            self.strategy = "llms-full.txt"
        else:
            links = self.llms_index()
            if links:
                self.fetch_list(links, "llms.txt index")
            else:
                pages = self.sitemap()
                if len(pages) >= 3:
                    self.fetch_list(pages, "sitemap")
                else:
                    self.bfs()
        if not self.written:
            detail = f"; {self.start_problem}" if self.start_problem else ""
            if "JavaScript" in self.start_problem:
                reason = f"No pages saved: {self.start_problem}; needs a browser or manual collection"
                self.ctx.store.add_manual(self.record, reason)
                return DocResult(self.record.doc_id, "manual", message=reason)
            return DocResult(self.record.doc_id, "error", message=f"No pages saved ({self.strategy}{detail})")
        status = "partial" if (self.failed_urls or self.capped) else "ok"
        msg = f"{len(self.written)} pages via {self.strategy}"
        if self.redirected_from:
            msg += f" from {self.start} (redirected)"
        if self.capped:
            msg += f" (capped at {self.ctx.max_pages})"
        if self.failed_urls:
            msg += f", {len(self.failed_urls)} failed (e.g. {', '.join(self.failed_urls[:3])})"
        if self.skipped:
            msg += f", {self.skipped} empty or duplicate skipped"
        return DocResult(self.record.doc_id, status, self.written, msg)


def crawl(record, ctx: Context) -> DocResult:
    return SiteCrawler(record, ctx).run()
