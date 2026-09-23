"""Shared test fixtures: a fake fetcher, a tiny PDF, and record builders."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from swarm_scraper.handlers import Context
from swarm_scraper.http import FetchError, Fetcher, Response
from swarm_scraper.inventory import DocRecord
from swarm_scraper.output import OutputStore

INVENTORY = Path(__file__).resolve().parent.parent / "inventory" / "swarm-technology-document-inventory.xlsx"


class FakeFetcher(Fetcher):
    """Serves canned responses; unknown URLs return 404. Never touches the network."""

    def __init__(self, routes: dict | None = None, disallow: tuple = ()):
        super().__init__(delay=0, retries=0, respect_robots=False, github_token="")
        self.routes = dict(routes or {})
        self.disallow = disallow
        self.requested: list[str] = []
        self.redirects: dict[str, str] = {}

    def redirect(self, src, dst):
        """Serve dst's response when src is requested, with the final URL set to dst (like requests)."""
        self.redirects[src] = dst

    def add(self, url, body, status=200, ctype="text/html"):
        if isinstance(body, (dict, list)):
            body, ctype = json.dumps(body), "application/json"
        if isinstance(body, str):
            body = body.encode()
        self.routes[url] = (status, body, {"Content-Type": ctype})

    def _raw_get(self, url, headers=None, check_robots=True):
        from swarm_scraper.http import RobotsDisallowed
        if any(url.startswith(d) for d in self.disallow):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        self.requested.append(url)
        url = self.redirects.get(url, url)
        if url not in self.routes:
            return Response(url=url, status=404, content=b"not found", headers={"Content-Type": "text/plain"})
        status, body, hdrs = self.routes[url]
        if status == "raise":
            raise FetchError(f"connection failed for {url}")
        return Response(url=url, status=status, content=body, headers=hdrs)


def make_pdf(text: str = "Swarm coordination architecture " * 20) -> bytes:
    """Build a minimal one-page PDF containing extractable text."""
    # One short line per Tj so the text stays on the page (PyMuPDF clips text outside the MediaBox).
    words, lines, line = text[:900].split(), [], ""
    for w in words:
        if len(line) + len(w) >= 90:
            lines.append(line)
            line = ""
        line += w + " "
    lines.append(line)
    content = ("BT /F1 10 Tf 12 TL 20 750 Td " + " ".join(f"({l.strip()}) Tj T*" for l in lines) + " ET").encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


LONG_HTML = """<html><head><title>{title}</title></head><body>
<nav>Site navigation that should be dropped</nav>
<main><h1>{title}</h1>{body}</main><footer>Footer text</footer></body></html>"""


def html_page(title="Doc page", paragraphs=6, links=()):
    body = "".join(f"<p>Paragraph {i} about multi-vehicle autonomy and swarm coordination details.</p>"
                   for i in range(paragraphs))
    body += "".join(f'<a href="{href}">link</a>' for href in links)
    return LONG_HTML.format(title=title, body=body)


def record(url, method, doc_id="D9999", technology="Test Tech", title="Test doc", **kw) -> DocRecord:
    return DocRecord(doc_id=doc_id, technology=technology, title=title, url=url, method=method,
                     access=kw.pop("access", "Public"), verified=kw.pop("verified", "Seen in search results"), **kw)


def context(fetcher, **kw) -> tuple[Context, Path]:
    root = Path(tempfile.mkdtemp(prefix="scraper-test-"))
    return Context(fetcher=fetcher, store=OutputStore(root), log=lambda m: None, **kw), root


def read_doc(path) -> tuple[dict, str]:
    import yaml
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, header, body = text.split("---\n", 2)
    return yaml.safe_load(header), body
