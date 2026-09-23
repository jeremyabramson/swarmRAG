"""Route each inventory row to its handler and record the outcome."""
from __future__ import annotations

import traceback

from .handlers import Context, github, site, web
from .http import FetchError, RobotsDisallowed
from .output import DocResult

HANDLERS = {
    "GitHub README": github.readme,
    "Git clone (docs folder)": github.docs_folder,
    "GitHub releases API": github.releases,
    "GitHub organization listing, then READMEs": github.org_listing,
    "GitHub wiki clone": github.wiki,
    "Documentation-site crawl": site.crawl,
    "Documentation-site crawl (llms.txt first)": site.crawl,
    "arXiv paper": web.arxiv,
    "Direct PDF": web.direct_pdf,
    "Web page to markdown": web.webpage,
}
MANUAL = "Manual collection"
GATED_ACCESS = ("Gated", "Internal", "Subscription")


def process(record, ctx: Context) -> DocResult:
    if not record.has_url:
        ctx.store.add_manual(record, "No public URL")
        return DocResult(record.doc_id, "manual", message="No public URL")
    if record.method == MANUAL or record.access.startswith(GATED_ACCESS):
        reason = record.access if record.access.startswith(GATED_ACCESS) else "Marked for manual collection"
        ctx.store.add_manual(record, reason)
        return DocResult(record.doc_id, "manual", message=reason)
    handler = HANDLERS.get(record.method)
    if handler is None:
        return DocResult(record.doc_id, "error", message=f"Unknown scrape method: {record.method!r}")
    try:
        return handler(record, ctx)
    except RobotsDisallowed as exc:
        ctx.store.add_manual(record, f"robots.txt disallows: {exc}")
        return DocResult(record.doc_id, "manual", message=str(exc))
    except FetchError as exc:
        if exc.status in (401, 403):  # site refuses automated clients; a person can usually still get it
            reason = f"Site refused automated access: {exc}"
            ctx.store.add_manual(record, reason)
            return DocResult(record.doc_id, "manual", message=reason)
        return DocResult(record.doc_id, "error", message=str(exc))
    except ValueError as exc:
        return DocResult(record.doc_id, "error", message=str(exc))
    except Exception as exc:  # keep the batch going; details go in the manifest
        return DocResult(record.doc_id, "error",
                         message=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
