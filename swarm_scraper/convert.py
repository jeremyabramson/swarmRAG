"""Turn fetched HTML and PDF content into Markdown / plain text."""
from __future__ import annotations

import io
import re
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:  # better main-content extraction when available
    import trafilatura  # type: ignore
except ImportError:  # pragma: no cover
    trafilatura = None

try:
    from markdownify import markdownify as _markdownify
except ImportError:  # pragma: no cover
    _markdownify = None

_STRIP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"]
_MAIN_SELECTORS = ["main", "article", "[role=main]", ".markdown-body", ".rst-content", ".md-content",
                   ".theme-doc-markdown", "#content", ".content", ".document"]


# Heading permalinks ("¶", "#", zero-width space, or nothing) that add noise to every heading
_PERMALINK_CLASSES = {"headerlink", "header-anchor", "anchor", "anchorjs-link", "hash-link"}
_EMPTY_LINK = re.compile(r"\[[\s\u200b¶#§🔗]*\]\([^)]*\)")


def _tidy(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _clean_links(soup: BeautifulSoup, url: str | None) -> None:
    """Drop heading permalinks and make every href absolute against the page URL.

    trafilatura resolves relative links against the host root rather than the page,
    which breaks links on versioned docs (e.g. /main/en/...), so resolve them first.
    """
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).replace("\u200b", "")
        classes = set(a.get("class") or [])
        if (classes & _PERMALINK_CLASSES or a["href"].startswith("#")) and text in ("", "¶", "#", "§"):
            a.decompose()
        elif url:
            a["href"] = urljoin(url, a["href"])


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def html_title(html: str) -> str:
    m = _TITLE.search(html[:200_000])  # avoid a second full parse of large pages
    if m and m.group(1).strip():
        return re.sub(r"\s+", " ", unescape(m.group(1))).strip()
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def is_empty_shell(html: str) -> bool:
    """True when the page has a main-content element but it holds no text.

    Seen on JavaScript-rendered sites and client-side redirect stubs (old URLs still in
    the sitemap), where only the navigation would otherwise be extracted.
    """
    soup = BeautifulSoup(html, "lxml")
    regions = soup.select("main, article, [role=main]")
    return bool(regions) and sum(len(r.get_text(strip=True)) for r in regions) < 50


def html_to_markdown(html: str, url: str | None = None) -> str:
    """Extract the main content of a page as Markdown."""
    soup = BeautifulSoup(html, "lxml")
    _clean_links(soup, url)
    if trafilatura is not None:
        out = trafilatura.extract(str(soup), url=url, output_format="markdown", include_links=True,
                                  include_tables=True, favor_recall=True)
        if out and len(out) > 200:
            return _tidy(_EMPTY_LINK.sub("", out))
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    main = None
    for sel in _MAIN_SELECTORS:
        main = soup.select_one(sel)
        if main and len(main.get_text(strip=True)) > 100:
            break
        main = None
    node = main or soup.body or soup
    if _markdownify is not None:
        md = _markdownify(str(node), heading_style="ATX", strip=["img"])
    else:  # pragma: no cover
        md = node.get_text("\n")
    return _tidy(_EMPTY_LINK.sub("", md))


def pdf_to_text(data: bytes) -> str:
    """Extract text from a PDF. Prefers PyMuPDF, falls back to pypdf."""
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore  # PyMuPDF < 1.24
        except ImportError:
            pymupdf = None
    if pymupdf is not None:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return _tidy("\n\n".join(f"<!-- page {i} -->\n" + page.get_text() for i, page in enumerate(doc, 1)))
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            pages.append(f"<!-- page {i} -->\n" + (page.extract_text() or ""))
        except Exception as exc:  # malformed page
            pages.append(f"<!-- page {i}: extraction failed ({exc}) -->")
    return _tidy("\n\n".join(pages))


def rst_to_text(rst: str) -> str:
    """Keep reStructuredText as-is (readable and chunkable); convert with pandoc later if wanted."""
    return _tidy(rst)
