import unittest

from swarm_scraper.dispatch import process
from swarm_scraper.handlers import site
from tests.helpers import FakeFetcher, context, html_page, read_doc, record

SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>"""
INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://docs.x.org/sm1.xml</loc></sitemap></sitemapindex>"""


def urlset(urls):
    return SITEMAP.format("".join(f"<url><loc>{u}</loc></url>" for u in urls))


class HelperTests(unittest.TestCase):
    def test_scope_prefix(self):
        self.assertEqual(site.scope_prefix("https://docs.px4.io/main/en/"), "https://docs.px4.io/main/en/")
        self.assertEqual(site.scope_prefix("https://nvlabs.github.io/sionna/rt"), "https://nvlabs.github.io/sionna/rt/")
        self.assertEqual(site.scope_prefix("https://a.io/docs/index.html#x"), "https://a.io/docs/")
        self.assertEqual(site.scope_prefix("https://a.io"), "https://a.io/")

    def test_page_filename(self):
        p = "https://a.io/docs/"
        self.assertEqual(site.page_filename("https://a.io/docs/", p), "index.md")
        self.assertEqual(site.page_filename("https://a.io/docs/Guide/Setup.html", p), "guide/setup.md")


    def test_query_strings_kept_for_page_selection(self):
        self.assertEqual(site.normalize("https://w.org/pmwiki.php?n=Main.Home#top"), "https://w.org/pmwiki.php?n=Main.Home")
        self.assertEqual(site.normalize("https://a.io/p?utm_source=x&b=2&a=1"), "https://a.io/p?a=1&b=2")
        self.assertEqual(site.page_filename("https://w.org/wiki/pmwiki.php?n=Main.Home", "https://w.org/wiki/"),
                         "pmwiki--nmainhome.md")
        self.assertNotEqual(site.page_filename("https://w.org/p.php?n=A", "https://w.org/"),
                            site.page_filename("https://w.org/p.php?n=B", "https://w.org/"))

    def test_scope_excludes_site_furniture(self):
        p = "https://www.argos-sim.info/"
        for url in ["https://www.argos-sim.info/forum/viewtopic.php?t=5", "https://www.argos-sim.info/cdn-cgi/l/email-protection",
                    "https://w.org/pmwiki.php?n=Main.Home&action=edit", "https://w.org/index.php?title=X&oldid=12",
                    "https://www.argos-sim.info/login"]:
            self.assertFalse(site.in_scope(url, "https://w.org/" if "w.org" in url else p), url)
        self.assertTrue(site.in_scope("https://w.org/pmwiki.php?n=Main.Home&action=view", "https://w.org/"))
        self.assertTrue(site.in_scope("https://www.argos-sim.info/user_manual.php", p))


class CrawlStrategyTests(unittest.TestCase):
    def test_llms_full_wins(self):
        f = FakeFetcher()
        f.add("https://docs.x.org/llms-full.txt", "# All docs\n" + "content " * 300, ctype="text/plain")
        ctx, _ = context(f)
        res = site.crawl(record("https://docs.x.org/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "ok")
        self.assertIn("llms-full", res.message)
        self.assertEqual(len(res.files), 1)

    def test_llms_index_fetches_linked_pages_same_host_only(self):
        f = FakeFetcher()
        f.add("https://docs.x.org/llms.txt",
              "# Docs\n- [SDK](https://docs.x.org/sdk.md)\n- [API](https://docs.x.org/api/)\n"
              "- [Other](https://elsewhere.com/a)\n", ctype="text/plain")
        f.add("https://docs.x.org/sdk.md", "# SDK\nThe SDK is built on ROS 2 and does many things.", ctype="text/markdown")
        f.add("https://docs.x.org/api/", html_page("API"))
        ctx, _ = context(f)
        res = site.crawl(record("https://docs.x.org/", "Documentation-site crawl (llms.txt first)"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(len(res.files), 2)
        self.assertNotIn("https://elsewhere.com/a", f.requested)

    def test_sitemap_index_and_scope(self):
        f = FakeFetcher()
        f.add("https://docs.x.org/sitemap.xml", INDEX, ctype="application/xml")
        f.add("https://docs.x.org/sm1.xml", urlset([
            "https://docs.x.org/main/en/a.html", "https://docs.x.org/main/en/b.html",
            "https://docs.x.org/main/en/c/", "https://docs.x.org/v1.14/en/a.html"]), ctype="application/xml")
        for u in ["a.html", "b.html", "c/"]:
            f.add(f"https://docs.x.org/main/en/{u}", html_page(u))
        ctx, _ = context(f)
        res = site.crawl(record("https://docs.x.org/main/en/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertIn("sitemap", res.message)
        self.assertEqual(len(res.files), 3)
        self.assertNotIn("https://docs.x.org/v1.14/en/a.html", f.requested)

    def test_bfs_stays_in_scope_and_respects_cap(self):
        f = FakeFetcher()
        f.add("https://a.io/docs/", html_page("Home", links=["guide.html", "/docs/api.html", "/blog/x.html",
                                                               "https://other.com/", "img.png", "guide.html#s"]))
        f.add("https://a.io/docs/guide.html", html_page("Guide", links=["deep.html"]))
        f.add("https://a.io/docs/api.html", html_page("API"))
        f.add("https://a.io/docs/deep.html", html_page("Deep"))
        ctx, _ = context(f, max_pages=3)
        res = site.crawl(record("https://a.io/docs/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "partial")  # hit the 3-page cap
        self.assertEqual(len(res.files), 3)
        self.assertNotIn("https://a.io/blog/x.html", f.requested)
        self.assertNotIn("https://other.com/", f.requested)
        meta, body = read_doc(res.files[0])
        self.assertEqual(meta["page_title"], "Home")
        self.assertNotIn("Site navigation", body)

    def test_empty_shells_and_duplicates_skipped(self):
        # PX4's sitemap lists old URLs that redirect client-side: <main> is empty, only nav remains
        shell = ("<html><body><nav>" + "Flight modes Sensors Peripherals " * 20 + "</nav>"
                 '<main class="main"></main></body></html>')
        f = FakeFetcher()
        f.add("https://docs.x.org/sitemap.xml", urlset(
            [f"https://docs.x.org/en/{p}" for p in ("a.html", "b.html", "old1.html", "old2.html", "a/index.html")]),
            ctype="application/xml")
        f.add("https://docs.x.org/en/a.html", html_page("A"))
        f.add("https://docs.x.org/en/a/index.html", html_page("A"))  # same page under an alias
        f.add("https://docs.x.org/en/b.html", html_page("B"))
        f.add("https://docs.x.org/en/old1.html", shell)
        f.add("https://docs.x.org/en/old2.html", shell)
        ctx, _ = context(f)
        res = site.crawl(record("https://docs.x.org/en/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in res.files), ["a.md", "b.md"])
        self.assertIn("3 empty or duplicate skipped", res.message)

    def test_sphinx_generated_pages_not_crawled(self):
        f = FakeFetcher()
        f.add("https://a.io/", html_page("Home", links=["_modules/index.html", "_sources/x.rst.txt", "genindex.html",
                                                        "search.html", "py-modindex.html", "guide.html"]))
        f.add("https://a.io/guide.html", html_page("Guide"))
        ctx, _ = context(f)
        res = site.crawl(record("https://a.io/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(len(res.files), 2)
        self.assertFalse([u for u in f.requested if any(k in u for k in ("_modules", "_sources", "index.html",
                                                                         "search", "modindex"))])

    def test_cap_detected_even_when_pages_skipped(self):
        f = FakeFetcher()
        f.add("https://docs.x.org/sitemap.xml", urlset([f"https://docs.x.org/p{i}.html" for i in range(5)]),
              ctype="application/xml")
        for i in range(5):
            f.add(f"https://docs.x.org/p{i}.html", html_page("Same"))  # all identical: 1 written
        ctx, _ = context(f, max_pages=3)
        res = site.crawl(record("https://docs.x.org/", "Documentation-site crawl"), ctx)
        self.assertEqual((res.status, len(res.files)), ("partial", 1))
        self.assertIn("capped at 3", res.message)

    def test_follows_redirect_to_new_host_and_meta_refresh(self):
        # autowarefoundation.github.io/autoware-documentation/ -> docs.autoware.org/ -> (meta refresh) main/
        f = FakeFetcher()
        f.redirect("https://org.github.io/proj-docs/", "https://docs.proj.org/")
        f.add("https://docs.proj.org/", '<html><head><meta http-equiv="refresh" content="0; url=main/"></head>'
                                        '<body>Redirecting to <a href="main/">main/</a>...</body></html>')
        f.add("https://docs.proj.org/main/", html_page("Home", links=["guide/", "/other/x.html"]))
        f.add("https://docs.proj.org/main/guide/", html_page("Guide"))
        ctx, _ = context(f)
        res = site.crawl(record("https://org.github.io/proj-docs/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(len(res.files), 2)
        self.assertIn("redirected", res.message)
        meta, body = read_doc(res.files[0])
        self.assertEqual(meta["crawl_root"], "https://docs.proj.org/main/")
        self.assertNotIn("Redirecting", body)
        self.assertNotIn("https://docs.proj.org/other/x.html", f.requested)

    def test_redirect_stub_not_saved(self):
        f = FakeFetcher()
        f.add("https://a.io/docs/", html_page("Home", links=["old.html", "new.html"]))
        f.add("https://a.io/docs/old.html", '<html><head><meta http-equiv="Refresh" content="0;URL=\'new.html\'">'
                                            "</head><body>" + "This page has moved to a new location. " * 3
                                            + "</body></html>")
        f.add("https://a.io/docs/new.html", html_page("New"))
        ctx, _ = context(f)
        res = site.crawl(record("https://a.io/docs/", "Documentation-site crawl"), ctx)
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in res.files), ["index.md", "new.md"])

    def test_start_page_problems(self):
        spa = ('<html><head><script src="main.js"></script></head><body><app-root></app-root>'
               "<script>boot()</script></body></html>")
        cases = {  # Robotarium (Angular), Raspberry Pi (403), Skydio (robots), Clearpath (moved)
            "https://spa.io/": ("manual", "JavaScript-rendered"),
            "https://forbidden.io/docs/": ("manual", "403"),
            "https://blocked.io/docs/": ("manual", "robots.txt"),
            "https://moved.io/docs/": ("error", "start page returned HTTP 404"),
        }
        f = FakeFetcher(disallow=("https://blocked.io",))
        f.add("https://spa.io/", spa)
        f.add("https://forbidden.io/docs/", "Just a moment...", status=403)
        for i, (url, (status, text)) in enumerate(cases.items()):
            ctx, root = context(f)
            res = process(record(url, "Documentation-site crawl", doc_id=f"D{i}"), ctx)
            self.assertEqual(res.status, status, (url, res.message))
            self.assertIn(text, res.message)
            self.assertEqual((root / "manual_queue.csv").exists(), status == "manual")

    def test_wiki_pages_by_query_crawled_separately(self):
        base = "https://oceanai.mit.edu/moos-ivp/pmwiki/pmwiki.php"
        f = FakeFetcher()
        f.add(base + "?n=Main.HomePage", html_page("Home", links=["pmwiki.php?n=Helm.Intro", "pmwiki.php?n=IvP.Behaviors",
                                                                 "pmwiki.php?n=Helm.Intro&action=edit"]))
        f.add(base + "?n=Helm.Intro", html_page("Helm intro"))
        f.add(base + "?n=IvP.Behaviors", html_page("Behaviors"))
        ctx, _ = context(f)
        res = site.crawl(record(base + "?n=Main.HomePage", "Documentation-site crawl"), ctx)
        self.assertEqual((res.status, len(res.files)), ("ok", 3), res.message)
        self.assertFalse([u for u in f.requested if "action=edit" in u])

    def test_nothing_found_is_error(self):
        ctx, _ = context(FakeFetcher())
        res = site.crawl(record("https://none.io/docs/", "Documentation-site crawl"), ctx)
        self.assertEqual(res.status, "error")


if __name__ == "__main__":
    unittest.main()
