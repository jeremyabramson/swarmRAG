"""Single web pages, direct PDFs, and arXiv papers."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..convert import html_title, html_to_markdown, pdf_to_text
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
    if "html" in ctype or resp.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        body = html_to_markdown(resp.text, url=resp.url)
        meta = _meta(record, fetched_url=resp.url, page_title=html_title(resp.text), source_format="html")
    else:  # plain text, Markdown, reStructuredText, RFC text, XML
        body = resp.text
        meta = _meta(record, fetched_url=resp.url, source_format=ctype or "text")
    path = ctx.store.write_markdown(base.with_suffix(".md"), body, meta)
    status = "ok" if len(body.strip()) > 200 else "partial"
    return DocResult(record.doc_id, status, [str(path)],
                     "" if status == "ok" else "Very little text extracted (JavaScript-rendered page?)")


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


def arxiv_metadata(ctx: Context, aid: str) -> dict:
    try:
        resp = ctx.fetcher.get_ok(f"https://export.arxiv.org/api/query?id_list={aid}")
        entry = ET.fromstring(resp.content).find("a:entry", ATOM)
        if entry is None:
            return {}
        text = lambda tag: (entry.findtext(f"a:{tag}", default="", namespaces=ATOM) or "").strip()
        return {
            "arxiv_id": aid,
            "paper_title": re.sub(r"\s+", " ", text("title")),
            "authors": [a.findtext("a:name", namespaces=ATOM) for a in entry.findall("a:author", ATOM)],
            "published": text("published")[:10],
            "abstract": re.sub(r"\s+", " ", text("summary")),
        }
    except (FetchError, ET.ParseError):
        return {"arxiv_id": aid}


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
                return DocResult(record.doc_id, "ok", [str(path)], "arXiv HTML")
    except FetchError:
        pass
    resp = ctx.fetcher.get_ok(f"https://arxiv.org/pdf/{aid}")
    result = _save_pdf(record, ctx, resp, extra=meta)
    result.message = ("arXiv PDF. " + result.message).strip()
    return result
