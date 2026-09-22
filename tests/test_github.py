import unittest
from pathlib import Path

from swarm_scraper.handlers import github
from swarm_scraper.handlers.github import parse_github_url
from tests.helpers import FakeFetcher, context, read_doc, record

API, RAW = "https://api.github.com", "https://raw.githubusercontent.com"


class ParseTests(unittest.TestCase):
    def test_cases(self):
        c = parse_github_url
        self.assertEqual(c("https://github.com/PX4/PX4-Autopilot").kind, "repo")
        g = c("https://github.com/PX4/PX4-Autopilot/blob/main/README.md")
        self.assertEqual((g.kind, g.ref, g.path), ("blob", "main", "README.md"))
        g = c("https://github.com/foxglove/mcap/tree/main/docs/specification")
        self.assertEqual((g.kind, g.ref, g.path), ("tree", "main", "docs/specification"))
        self.assertEqual(c("https://github.com/adjacentlink/emane/wiki").kind, "wiki")
        self.assertEqual(c("https://github.com/adjacentlink/emane/wiki/Introduction").path, "Introduction")
        self.assertEqual(c("https://github.com/isaac-sim/IsaacSim/releases").kind, "releases")
        self.assertEqual(c("https://github.com/orgs/skybrush-io/repositories").kind, "org")
        self.assertEqual(c("https://github.com/aerostack2").kind, "org")
        self.assertEqual(c("https://www.github.com/foxglove/studio").repo, "studio")
        self.assertEqual(c("https://github.com/x/y.git").repo, "y")
        with self.assertRaises(ValueError):
            c("https://gitlab.com/x/y")


class ReadmeTests(unittest.TestCase):
    def test_repo_readme_via_api(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/PX4/PX4-Autopilot/readme", "# PX4\nFlight stack", ctype="text/plain")
        ctx, _ = context(f)
        res = github.readme(record("https://github.com/PX4/PX4-Autopilot", "GitHub README"), ctx)
        self.assertEqual(res.status, "ok")
        meta, body = read_doc(res.files[0])
        self.assertIn("Flight stack", body)
        self.assertEqual(meta["source_url"], "https://github.com/PX4/PX4-Autopilot")
        self.assertIn("sha256", meta)

    def test_blob_uses_raw(self):
        f = FakeFetcher()
        f.add(f"{RAW}/ros2/rmw_zenoh/rolling/docs/design.md", "# Design", ctype="text/plain")
        ctx, _ = context(f)
        res = github.readme(record("https://github.com/ros2/rmw_zenoh/blob/rolling/docs/design.md",
                                   "GitHub README"), ctx)
        self.assertEqual(res.status, "ok")
        self.assertEqual(f.requested, [f"{RAW}/ros2/rmw_zenoh/rolling/docs/design.md"])

    def test_falls_back_to_raw_when_api_rate_limited(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/a/b/readme", "rate limited", status=403)
        f.add(f"{RAW}/a/b/HEAD/README.md", "# Fallback README", ctype="text/plain")
        ctx, _ = context(f)
        res = github.readme(record("https://github.com/a/b", "GitHub README"), ctx)
        self.assertEqual(res.status, "ok")
        self.assertIn("Fallback", read_doc(res.files[0])[1])

    def test_tree_url_readme_in_subfolder(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/a/b/readme/pkg?ref=main", "# Sub readme", ctype="text/plain")
        ctx, _ = context(f)
        res = github.readme(record("https://github.com/a/b/tree/main/pkg", "GitHub README"), ctx)
        self.assertEqual(res.status, "ok")


class DocsFolderTests(unittest.TestCase):
    def _tree(self, paths, truncated=False):
        return {"tree": [{"path": p, "type": "blob"} for p in paths], "truncated": truncated}

    def test_tree_url_prefix(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/foxglove/mcap/git/trees/main?recursive=1",
              self._tree(["docs/specification/README.md", "docs/specification/profiles/ros2.md",
                          "docs/other.md", "go/main.go"]))
        f.add(f"{RAW}/foxglove/mcap/main/docs/specification/README.md", "# Spec", ctype="text/plain")
        f.add(f"{RAW}/foxglove/mcap/main/docs/specification/profiles/ros2.md", "# ROS 2 profile", ctype="text/plain")
        ctx, _ = context(f)
        res = github.docs_folder(record("https://github.com/foxglove/mcap/tree/main/docs/specification",
                                        "Git clone (docs folder)"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(len(res.files), 2)
        self.assertTrue(any(p.endswith("profiles/ros2.md") for p in res.files))

    def test_repo_root_prefers_docs_dir_and_skips_noise(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/a/b", {"default_branch": "master"})
        f.add(f"{API}/repos/a/b/git/trees/master?recursive=1",
              self._tree(["README.md", "docs/intro.rst", "docs/node_modules/x.md", "docs/guide.txt", "src/a.py"]))
        f.add(f"{RAW}/a/b/master/docs/intro.rst", "Intro\n=====", ctype="text/plain")
        f.add(f"{RAW}/a/b/master/docs/guide.txt", "Guide text", ctype="text/plain")
        ctx, _ = context(f)
        res = github.docs_folder(record("https://github.com/a/b", "Git clone (docs folder)"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(len(res.files), 2)
        meta, _ = read_doc([p for p in res.files if p.endswith("intro.md")][0])
        self.assertEqual(meta["source_format"], "rst")

    def test_cap_marks_partial(self):
        f = FakeFetcher()
        paths = [f"docs/p{i}.md" for i in range(5)]
        f.add(f"{API}/repos/a/b/git/trees/main?recursive=1", self._tree(paths))
        for p in paths:
            f.add(f"{RAW}/a/b/main/{p}", "text", ctype="text/plain")
        ctx, _ = context(f, max_repo_files=3)
        res = github.docs_folder(record("https://github.com/a/b/tree/main/docs", "Git clone (docs folder)"), ctx)
        self.assertEqual((res.status, len(res.files)), ("partial", 3))

    def test_no_docs_is_error(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/a/b/git/trees/main?recursive=1", self._tree(["src/a.py"]))
        ctx, _ = context(f)
        res = github.docs_folder(record("https://github.com/a/b/tree/main/docs", "Git clone (docs folder)"), ctx)
        self.assertEqual(res.status, "error")


class ReleasesOrgWikiTests(unittest.TestCase):
    def test_releases(self):
        f = FakeFetcher()
        f.add(f"{API}/repos/isaac-sim/IsaacSim/releases?per_page=100",
              [{"name": "6.0.1", "tag_name": "v6.0.1", "published_at": "2026-06-01T00:00:00Z", "body": "Fixes"},
               {"name": None, "tag_name": "v6.0.0", "published_at": None, "body": None}])
        ctx, _ = context(f)
        res = github.releases(record("https://github.com/isaac-sim/IsaacSim/releases", "GitHub releases API"), ctx)
        self.assertEqual(res.status, "ok")
        body = read_doc(res.files[0])[1]
        self.assertIn("## 6.0.1 (v6.0.1, 2026-06-01)", body)
        self.assertIn("## v6.0.0", body)

    def test_org_listing_falls_back_to_users_and_skips_forks(self):
        f = FakeFetcher()
        f.add(f"{API}/orgs/ROS2swarm/repos?per_page=100", "nope", status=404)
        f.add(f"{API}/users/ROS2swarm/repos?per_page=100", [
            {"name": "ROS2swarm", "html_url": "https://github.com/ROS2swarm/ROS2swarm", "stargazers_count": 9,
             "archived": False, "fork": False, "description": "main"},
            {"name": "forked", "html_url": "u", "fork": True},
            {"name": "old", "html_url": "https://github.com/ROS2swarm/old", "stargazers_count": 1,
             "archived": True, "fork": False, "description": None}])
        f.add(f"{API}/repos/ROS2swarm/ROS2swarm/readme", "# Readme", ctype="text/plain")
        ctx, _ = context(f)
        res = github.org_listing(record("https://github.com/ROS2swarm",
                                        "GitHub organization listing, then READMEs"), ctx)
        self.assertEqual(res.status, "ok", res.message)
        self.assertEqual(len(res.files), 2)  # index + one README (archived repo skipped)
        index = read_doc(res.files[0])[1]
        self.assertIn("ROS2swarm", index)
        self.assertNotIn("forked", index)

    def test_wiki_uses_git_runner(self):
        def fake_git(args, cwd=None):
            dest = Path(args[-1])
            (dest / "Home.md").write_text("# Home")
            (dest / "Radio-Models.md").write_text("# Radios")
            (dest / "image.png").write_bytes(b"x")
        ctx, _ = context(FakeFetcher(), git_runner=fake_git)
        res = github.wiki(record("https://github.com/adjacentlink/emane/wiki", "GitHub wiki clone"), ctx)
        self.assertEqual((res.status, len(res.files)), ("ok", 2))
        meta, _ = read_doc(res.files[0])
        self.assertTrue(meta["fetched_url"].startswith("https://github.com/adjacentlink/emane/wiki/"))


class WikiFailureTests(unittest.TestCase):
    def test_git_failure_is_clean_error(self):
        import subprocess
        from swarm_scraper.dispatch import process

        def failing_git(args, cwd=None):
            raise subprocess.CalledProcessError(128, args, stderr="fatal: could not resolve host")
        ctx, _ = context(FakeFetcher(), git_runner=failing_git)
        res = process(record("https://github.com/osrf/vrx/wiki", "GitHub wiki clone"), ctx)
        self.assertEqual(res.status, "error")
        self.assertIn("could not resolve host", res.message)
        self.assertNotIn("Traceback", res.message)


if __name__ == "__main__":
    unittest.main()
