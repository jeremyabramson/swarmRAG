"""Single web pages, direct PDFs, and arXiv papers."""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..convert import html_title, html_to_markdown, is_empty_shell, pdf_to_text
from ..http import FetchError
from ..output import DocResult, now_iso
from . import Context


def _meta(record, **extra):
    m = record.metadata()
    m["retrieved_at"] = now_iso()
    m.update(extra)
    return m


def is_pdf(resp) -> bool:
    return resp.content[:5] == b"%PDF-" or resp.content_type == "application/pdf"


def _save_pdf(record, ctx: Context, resp, extra: dict | None = None) -> DocResult:
    base = ctx.store.base_path(record)
    files = []
    if ctx.keep_pdf:
        files.append(str(ctx.store.write_bytes(base.with_suffix(".pdf"), resp.content)))
    text = pdf_to_text(resp.content)
    meta = _meta(record, fetched_url=resp.url, source_format="pdf", **(extra or {}))
    files.insert(0, str(ctx.store.write_markdown(base.with_suffix(".md"), text, meta)))
    status = "ok" if len(text.strip()) > 200 else "partial"
    msg = "" if status == "ok" else "PDF yielded little text (scanned? try OCR)"
    return DocResult(record.doc_id, status, files, msg)


def _github_blob_to_raw(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", url)
    return f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/{m[3]}/{m[4]}" if m else url


def webpage(record, ctx: Context) -> DocResult:
    url = _github_blob_to_raw(record.url)
    resp = ctx.fetcher.get_ok(url)
    if is_pdf(resp):
        return _save_pdf(record, ctx, resp)
    base = ctx.store.base_path(record)
    ctype = resp.content_type
    shell = False
    if "html" in ctype or resp.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        shell = is_empty_shell(resp.text)
        body = html_to_markdown(resp.text, url=resp.url)
        meta = _meta(record, fetched_url=resp.url, page_title=html_title(resp.text), source_format="html")
    else:  # plain text, Markdown, reStructuredText, RFC text, XML
        body = resp.text
        meta = _meta(record, fetched_url=resp.url, source_format=ctype or "text")
    path = ctx.store.write_markdown(base.with_suffix(".md"), body, meta)
    status = "ok" if len(body.strip()) > 200 and not shell else "partial"
    msg = ""
    if shell:
        msg = "Main content area is empty; saved text is probably navigation (JavaScript-rendered page?)"
    elif status != "ok":
        msg = "Very little text extracted (JavaScript-rendered page?)"
    return DocResult(record.doc_id, status, [str(path)], msg)


def direct_pdf(record, ctx: Context) -> DocResult:
    resp = ctx.fetcher.get_ok(record.url)
    if not is_pdf(resp):  # link now serves a landing page
        result = webpage(record, ctx)
        result.message = ("Expected a PDF but got a web page; saved the page instead. " + result.message).strip()
        return result
    return _save_pdf(record, ctx, resp)


# --- arXiv -----------------------------------------------------------
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def arxiv_id(url: str) -> str:
    path = urlparse(url).path
    m = ARXIV_ID.search(path)
    if not m:
        raise ValueError(f"No arXiv identifier in {url}")
    return m.group(1) + (m.group(2) or "")


def _arxiv_api_metadata(ctx: Context, aid: str) -> dict:
    # The export API intermittently answers 406/503 while throttling; give it a few tries.
    for attempt in range(3):
        try:
            resp = ctx.fetcher.get(f"https://export.arxiv.org/api/query?id_list={aid}")
        except FetchError:
            resp = None
        if resp is not None and resp.ok:
            break
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    else:
        return {}
    try:
        entry = ET.fromstring(resp.content).find("a:entry", ATOM)
    except ET.ParseError:
        return {}
    if entry is None or not entry.findtext("a:title", default="", namespaces=ATOM).strip():
        return {}
    text = lambda tag: (entry.findtext(f"a:{tag}", default="", namespaces=ATOM) or "").strip()
    return {
        "paper_title": re.sub(r"\s+", " ", text("title")),
        "authors": [a.findtext("a:name", namespaces=ATOM) for a in entry.findall("a:author", ATOM)],
        "published": text("published")[:10],
        "abstract": re.sub(r"\s+", " ", text("summary")),
    }


def _arxiv_abs_metadata(ctx: Context, aid: str) -> dict:
    """Fallback: the citation_* meta tags on the abstract page."""
    try:
        resp = ctx.fetcher.get(f"https://arxiv.org/abs/{aid}")
    except FetchError:
        return {}
    if not resp.ok:
        return {}
    soup = BeautifulSoup(resp.text, "lxml")
    tag = lambda name: [m.get("content", "").strip() for m in soup.find_all("meta", attrs={"name": name})]
    title = tag("citation_title")
    if not title:
        return {}
    date = (tag("citation_date") or [""])[0].replace("/", "-")
    return {
        "paper_title": title[0],
        "authors": tag("citation_author"),
        "published": date,
        "abstract": re.sub(r"\s+", " ", (tag("citation_abstract") or [""])[0]),
    }


def arxiv_metadata(ctx: Context, aid: str) -> dict:
    meta = _arxiv_api_metadata(ctx, aid) or _arxiv_abs_metadata(ctx, aid)
    return {"arxiv_id": aid, **meta}


def _meta_note(meta: dict) -> str:
    return "" if "paper_title" in meta else " (no arXiv metadata)"


def arxiv(record, ctx: Context) -> DocResult:
    aid = arxiv_id(record.url)
    meta = arxiv_metadata(ctx, aid)
    base = ctx.store.base_path(record)
    # The HTML rendering extracts more cleanly than the PDF when it exists.
    try:
        resp = ctx.fetcher.get(f"https://arxiv.org/html/{aid}")
        if resp.ok and "html" in resp.content_type and "No HTML for" not in resp.text[:5000]:
            body = html_to_markdown(resp.text, url=resp.url)
            if len(body) > 2000:
                path = ctx.store.write_markdown(base.with_suffix(".md"), body,
                                                _meta(record, fetched_url=resp.url, source_format="arxiv-html", **meta))
                return DocResult(record.doc_id, "ok", [str(path)], "arXiv HTML" + _meta_note(meta))
    except FetchError:
        pass
    resp = ctx.fetcher.get_ok(f"https://arxiv.org/pdf/{aid}")
    result = _save_pdf(record, ctx, resp, extra=meta)
    result.message = ("arXiv PDF" + _meta_note(meta) + ". " + result.message).strip()
    return result
