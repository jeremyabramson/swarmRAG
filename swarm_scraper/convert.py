"""Turn fetched HTML and PDF content into Markdown / plain text."""
from __future__ import annotations

import io
import re

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


def _tidy(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def html_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def html_to_markdown(html: str, url: str | None = None) -> str:
    """Extract the main content of a page as Markdown."""
    if trafilatura is not None:
        out = trafilatura.extract(html, url=url, output_format="markdown", include_links=True,
                                  include_tables=True, favor_recall=True)
        if out and len(out) > 200:
            return _tidy(out)
    soup = BeautifulSoup(html, "lxml")
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
    return _tidy(md)


def pdf_to_text(data: bytes) -> str:
    """Extract text from a PDF. Prefers PyMuPDF, falls back to pypdf."""
    try:
        import fitz  # type: ignore  # PyMuPDF
        with fitz.open(stream=data, filetype="pdf") as doc:
            return _tidy("\n\n".join(page.get_text() for page in doc))
    except ImportError:
        pass
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
