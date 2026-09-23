import unittest
from pathlib import Path
from unittest import mock

from swarm_scraper.convert import html_to_markdown, is_empty_shell, meta_refresh_url, pdf_to_text
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

    def test_pdf_text_whole_page(self):
        text = pdf_to_text(make_pdf("word " * 150))
        self.assertGreater(text.count("word"), 140)  # nothing clipped at the page edge

    def test_links_resolved_against_page_not_host(self):
        # Versioned docs (PX4 /main/en/) link with page-relative and root-relative paths
        html = html_page("Altitude", links=["../flight_modes/altitude.html", "/main/en/sim/gazebo.html"])
        md = html_to_markdown(html, url="https://docs.px4.io/main/en/config/page.html")
        self.assertIn("https://docs.px4.io/main/en/flight_modes/altitude", md)
        self.assertIn("https://docs.px4.io/main/en/sim/gazebo", md)
        self.assertNotIn("](https://docs.px4.io/flight_modes", md)

    def test_meta_refresh_url(self):
        self.assertEqual(meta_refresh_url('<meta http-equiv="refresh" content="0; url=main/">', "https://d.org/"),
                         "https://d.org/main/")
        self.assertEqual(meta_refresh_url("<meta http-equiv='REFRESH' content=\"5;URL='/x/y.html'\">", "https://d.org/a/"),
                         "https://d.org/x/y.html")
        self.assertIsNone(meta_refresh_url(html_page("No refresh"), "https://d.org/"))

    def test_empty_shell_detection(self):
        self.assertTrue(is_empty_shell("<html><body><nav>" + "menu " * 50 + "</nav><main> </main></body></html>"))
        self.assertFalse(is_empty_shell(html_page("Real")))
        self.assertFalse(is_empty_shell("<html><body><p>" + "no main element " * 20 + "</p></body></html>"))

    def test_heading_permalinks_dropped(self):
        html = ("<html><body><main>"
                '<h1>Setup <a class="header-anchor" href="#setup" aria-label="Permalink">\u200b</a></h1>'
                '<h2>Build<a class="headerlink" href="#build">\u00b6</a></h2>'
                + "<p>Instructions for building the multi-vehicle swarm stack from source code.</p>" * 6
                + '<p>See <a href="#build">the build section</a>.</p></main></body></html>')
        md = html_to_markdown(html, url="https://a.io/docs/setup.html")
        self.assertNotRegex(md, r"\[[\s\u200b\u00b6]*\]\(")
        self.assertIn("the build section", md)


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

    def test_empty_shell_page_is_partial(self):
        f = FakeFetcher()
        f.add("https://app.io/docs", "<html><body><nav>" + "Menu item " * 60 + '</nav><div id="root"><main></main></div>'
              "</body></html>")
        ctx, _ = context(f)
        res = web.webpage(record("https://app.io/docs", "Web page to markdown"), ctx)
        self.assertEqual(res.status, "partial")
        self.assertIn("navigation", res.message)

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


ABS_PAGE = """<html><head>
<meta name="citation_title" content="Aerostack2: A Software Framework" />
<meta name="citation_author" content="Fernandez-Cortizas, Miguel" />
<meta name="citation_author" content="Campoy, Pascual" />
<meta name="citation_date" content="2023/03/31" />
<meta name="citation_abstract" content="The development of   autonomous aerial systems." />
</head><body></body></html>"""


class ArxivTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch("swarm_scraper.handlers.web.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

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
        self.assertIn("no arXiv metadata", res.message)

    def test_api_throttle_retried(self):
        api = "https://export.arxiv.org/api/query?id_list=2303.18237"
        f = FakeFetcher()
        f.add(api, "Not Acceptable", status=406, ctype="text/plain")
        f.add("https://arxiv.org/html/2303.18237", html_page("Aerostack2", paragraphs=60))
        original = f._raw_get

        def flaky(url, headers=None, check_robots=True):  # 406 once, then the feed
            resp = original(url, headers, check_robots)
            if url == api:
                f.add(api, ATOM, ctype="application/atom+xml")
            return resp
        f._raw_get = flaky
        ctx, _ = context(f)
        res = web.arxiv(record("https://arxiv.org/abs/2303.18237", "arXiv paper"), ctx)
        self.assertEqual(f.requested.count(api), 2)
        self.assertEqual(read_doc(res.files[0])[0]["authors"], ["A. Author", "B. Author"])
        self.assertEqual(res.message, "arXiv HTML")

    def test_abs_page_fallback_when_api_down(self):
        f = FakeFetcher()
        f.add("https://export.arxiv.org/api/query?id_list=2303.18237", "busy", status=503, ctype="text/plain")
        f.add("https://arxiv.org/abs/2303.18237", ABS_PAGE)
        f.add("https://arxiv.org/html/2303.18237", html_page("Aerostack2", paragraphs=60))
        ctx, _ = context(f)
        res = web.arxiv(record("https://arxiv.org/abs/2303.18237", "arXiv paper"), ctx)
        meta, _ = read_doc(res.files[0])
        self.assertEqual(meta["paper_title"], "Aerostack2: A Software Framework")
        self.assertEqual(meta["authors"], ["Fernandez-Cortizas, Miguel", "Campoy, Pascual"])
        self.assertEqual(meta["published"], "2023-03-31")
        self.assertEqual(meta["abstract"], "The development of autonomous aerial systems.")


if __name__ == "__main__":
    unittest.main()
