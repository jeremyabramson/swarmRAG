import unittest
from pathlib import Path

from swarm_scraper.convert import html_to_markdown, pdf_to_text
from swarm_scraper.handlers import web
from tests.helpers import FakeFetcher, context, html_page, make_pdf, read_doc, record

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Aerostack2: A Software
 Framework</title><summary>An abstract.</summary><published>2023-03-31T00:00:00Z</published>
<author><name>A. Author</name></author><author><name>B. Author</name></author></entry></feed>"""


class ConvertTests(unittest.TestCase):
    def test_html_main_content(self):
        md = html_to_markdown(html_page("Title"))
        self.assertIn("# Title", md)
        self.assertNotIn("Footer text", md)

    def test_pdf_text(self):
        self.assertIn("Swarm coordination", pdf_to_text(make_pdf()))


class WebpageTests(unittest.TestCase):
    def test_html_page(self):
        f = FakeFetcher()
        f.add("https://shield.ai/edge-os/", html_page("EdgeOS"))
        ctx, _ = context(f)
        res = web.webpage(record("https://shield.ai/edge-os/", "Web page to markdown"), ctx)
        self.assertEqual(res.status, "ok")
        meta, body = read_doc(res.files[0])
        self.assertEqual(meta["page_title"], "EdgeOS")
        self.assertEqual(meta["source_format"], "html")

    def test_plain_text_kept(self):
        f = FakeFetcher()
        f.add("https://www.rfc-editor.org/rfc/rfc3626", "OLSR spec text " * 50, ctype="text/plain")
        ctx, _ = context(f)
        res = web.webpage(record("https://www.rfc-editor.org/rfc/rfc3626", "Web page to markdown"), ctx)
        self.assertEqual(read_doc(res.files[0])[0]["source_format"], "text/plain")

    def test_github_blob_fetched_raw(self):
        f = FakeFetcher()
        raw = "https://raw.githubusercontent.com/gazebosim/docs/master/harmonic/comparison.md"
        f.add(raw, "# Comparison\n" + "row " * 100, ctype="text/plain")
        ctx, _ = context(f)
        res = web.webpage(record("https://github.com/gazebosim/docs/blob/master/harmonic/comparison.md",
                                 "Web page to markdown"), ctx)
        self.assertEqual(res.status, "ok")
        self.assertEqual(f.requested, [raw])

    def test_pdf_served_as_webpage(self):
        f = FakeFetcher()
        f.add("https://x.org/paper", make_pdf(), ctype="application/octet-stream")
        ctx, _ = context(f)
        res = web.webpage(record("https://x.org/paper", "Web page to markdown"), ctx)
        self.assertTrue(res.files[0].endswith(".md"))
        self.assertTrue(any(p.endswith(".pdf") for p in res.files))

    def test_thin_page_is_partial(self):
        f = FakeFetcher()
        f.add("https://spa.io/", "<html><body><div id=root></div></body></html>")
        ctx, _ = context(f)
        self.assertEqual(web.webpage(record("https://spa.io/", "Web page to markdown"), ctx).status, "partial")


class PdfTests(unittest.TestCase):
    def test_direct_pdf_keeps_original(self):
        f = FakeFetcher()
        f.add("https://p.com/wp.pdf", make_pdf(), ctype="application/pdf")
        ctx, _ = context(f)
        res = web.direct_pdf(record("https://p.com/wp.pdf", "Direct PDF"), ctx)
        self.assertEqual(res.status, "ok")
        self.assertEqual({Path(p).suffix for p in res.files}, {".md", ".pdf"})

    def test_no_pdf_option(self):
        f = FakeFetcher()
        f.add("https://p.com/wp.pdf", make_pdf(), ctype="application/pdf")
        ctx, _ = context(f, keep_pdf=False)
        res = web.direct_pdf(record("https://p.com/wp.pdf", "Direct PDF"), ctx)
        self.assertEqual(len(res.files), 1)

    def test_landing_page_instead_of_pdf(self):
        f = FakeFetcher()
        f.add("https://p.com/wp.pdf", html_page("Landing"))
        ctx, _ = context(f)
        res = web.direct_pdf(record("https://p.com/wp.pdf", "Direct PDF"), ctx)
        self.assertIn("Expected a PDF", res.message)


class ArxivTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(web.arxiv_id("https://arxiv.org/abs/2303.18237"), "2303.18237")
        self.assertEqual(web.arxiv_id("https://arxiv.org/pdf/2212.03106"), "2212.03106")
        self.assertEqual(web.arxiv_id("https://arxiv.org/abs/2603.28032v1"), "2603.28032v1")
        with self.assertRaises(ValueError):
            web.arxiv_id("https://arxiv.org/list/cs.RO")

    def test_html_preferred(self):
        f = FakeFetcher()
        f.add("https://export.arxiv.org/api/query?id_list=2303.18237", ATOM, ctype="application/atom+xml")
        f.add("https://arxiv.org/html/2303.18237", html_page("Aerostack2", paragraphs=60))
        ctx, _ = context(f)
        res = web.arxiv(record("https://arxiv.org/abs/2303.18237", "arXiv paper"), ctx)
        self.assertEqual(res.status, "ok")
        meta, _ = read_doc(res.files[0])
        self.assertEqual(meta["paper_title"], "Aerostack2: A Software Framework")
        self.assertEqual(meta["authors"], ["A. Author", "B. Author"])
        self.assertEqual(meta["source_format"], "arxiv-html")

    def test_pdf_fallback(self):
        f = FakeFetcher()
        f.add("https://arxiv.org/pdf/2109.07735", make_pdf(), ctype="application/pdf")
        ctx, _ = context(f)
        res = web.arxiv(record("https://arxiv.org/abs/2109.07735", "arXiv paper"), ctx)
        self.assertEqual(res.status, "ok")
        self.assertIn("arXiv PDF", res.message)
        self.assertEqual(read_doc(res.files[0])[0]["arxiv_id"], "2109.07735")


if __name__ == "__main__":
    unittest.main()
