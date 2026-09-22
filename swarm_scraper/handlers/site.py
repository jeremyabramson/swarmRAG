"""Documentation-site crawl: llms-full.txt, then llms.txt, then sitemap, then link crawl."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import PurePosixPath
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from ..convert import html_title, html_to_markdown
from ..http import FetchError
from ..output import DocResult, now_iso, slugify
from . import Context

SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".zip", ".gz",
            ".tar", ".mp4", ".webm", ".woff", ".woff2", ".ttf", ".json", ".xml", ".pdf")
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")


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
    url = urldefrag(url)[0].split("?")[0]
    return url


def in_scope(url: str, prefix: str) -> bool:
    return normalize(url).startswith(prefix) and not url.lower().endswith(SKIP_EXT)


def page_filename(url: str, prefix: str) -> str:
    rel = normalize(url)[len(prefix):].strip("/")
    rel = re.sub(r"\.(html?|php|aspx)$", "", rel) or "index"
    return "/".join(slugify(seg, 80) for seg in rel.split("/")) + ".md"


class SiteCrawler:
    def __init__(self, record, ctx: Context):
        self.record, self.ctx = record, ctx
        self.start = record.url
        self.prefix = scope_prefix(record.url)
        p = urlparse(record.url)
        self.origin = f"{p.scheme}://{p.netloc}"
        self.out_dir = ctx.store.base_path(record)
        self.written: list[str] = []
        self.failures = 0

    def _meta(self, **extra):
        m = self.record.metadata()
        m["retrieved_at"] = now_iso()
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
            body = html_to_markdown(resp.text, url=url)
            meta = self._meta(fetched_url=url, page_title=html_title(resp.text), source_format="html")
        else:
            body = resp.text
            meta = self._meta(fetched_url=url, source_format=resp.content_type or "text")
        if len(body.strip()) < 50:
            return
        self.written.append(str(self.ctx.store.write_markdown(self.out_dir / name, body, meta)))

    def fetch_list(self, urls: list[str], strategy: str) -> None:
        for url in urls[: self.ctx.max_pages]:
            resp = self._try_get(url)
            if resp is None:
                self.failures += 1
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
                self.failures += 1
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
        self.strategy = "link crawl"

    def run(self) -> DocResult:
        self.strategy = ""
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
            return DocResult(self.record.doc_id, "error", message=f"No pages saved ({self.strategy})")
        capped = self.strategy != "llms-full.txt" and len(self.written) >= self.ctx.max_pages
        status = "partial" if (self.failures or capped) else "ok"
        msg = f"{len(self.written)} pages via {self.strategy}"
        if capped:
            msg += f" (capped at {self.ctx.max_pages})"
        if self.failures:
            msg += f", {self.failures} failed"
        return DocResult(self.record.doc_id, status, self.written, msg)


def crawl(record, ctx: Context) -> DocResult:
    return SiteCrawler(record, ctx).run()
