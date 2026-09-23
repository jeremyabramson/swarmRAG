"""GitHub handlers: README, docs folder, releases, organization listing, wiki."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..convert import html_to_markdown
from ..http import FetchError
from ..output import DocResult, now_iso
from . import Context

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DOC_EXTS = (".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc")
# Used only when an explicitly named folder has no prose docs: message definitions, schemas, HTML
SPEC_EXTS = (".xml", ".xsd", ".dtd", ".html", ".htm", ".json", ".yaml", ".yml", ".proto", ".idl", ".msg", ".srv",
             ".action", ".fbs", ".capnp")
SKIP_DIRS = ("node_modules/", ".github/", "vendor/", "third_party/", "3rdparty/", "test/", "tests/")
PREFERRED_DOC_DIRS = ("docs/", "doc/", "documentation/", "en/", "_docs/")
RESERVED = {"orgs", "topics", "settings", "marketplace", "sponsors", "features"}


@dataclass
class GitHubRef:
    owner: str
    repo: str | None = None
    kind: str = "repo"   # repo | blob | tree | wiki | releases | org
    ref: str | None = None
    path: str = ""


def parse_github_url(url: str) -> GitHubRef:
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host != "github.com":
        raise ValueError(f"Not a github.com URL: {url}")
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        raise ValueError(f"No owner in URL: {url}")
    if segs[0] == "orgs" and len(segs) >= 2:
        return GitHubRef(owner=segs[1], kind="org")
    if len(segs) == 1:
        return GitHubRef(owner=segs[0], kind="org")
    owner, repo = segs[0], segs[1].removesuffix(".git")
    rest = segs[2:]
    if not rest:
        return GitHubRef(owner, repo)
    if rest[0] in ("blob", "tree") and len(rest) >= 2:
        return GitHubRef(owner, repo, kind=rest[0], ref=rest[1], path="/".join(rest[2:]))
    if rest[0] == "wiki":
        return GitHubRef(owner, repo, kind="wiki", path="/".join(rest[1:]))
    if rest[0] == "releases":
        return GitHubRef(owner, repo, kind="releases")
    return GitHubRef(owner, repo)


def _raw_url(g: GitHubRef, ref: str, path: str) -> str:
    return f"{RAW}/{g.owner}/{g.repo}/{ref}/{path}"


def _default_branch(ctx: Context, g: GitHubRef) -> str:
    data = ctx.fetcher.get_ok(f"{API}/repos/{g.owner}/{g.repo}").json()
    return data.get("default_branch", "main")


def _meta(record, **extra) -> dict:
    m = record.metadata()
    m["retrieved_at"] = now_iso()
    m.update(extra)
    return m


# ---------------------------------------------------------------------
def readme(record, ctx: Context) -> DocResult:
    g = parse_github_url(record.url)
    if g.kind == "org":
        return org_listing(record, ctx)
    if g.kind == "releases":
        return releases(record, ctx)
    if g.kind == "blob" and g.path:
        url = _raw_url(g, g.ref, g.path)
        body = ctx.fetcher.get_ok(url).text
        fetched = url
    else:
        api = f"{API}/repos/{g.owner}/{g.repo}/readme"
        if g.kind == "tree" and g.path:  # README inside a sub-folder
            api = f"{API}/repos/{g.owner}/{g.repo}/readme/{g.path}"
        if g.ref:
            api += f"?ref={g.ref}"
        try:
            resp = ctx.fetcher.get_ok(api, headers={"Accept": "application/vnd.github.raw"})
            body, fetched = resp.text, api
        except FetchError:
            # API rate limit or missing API access: fall back to the raw README
            fetched = _raw_url(g, g.ref or "HEAD", (g.path + "/" if g.path else "") + "README.md")
            body = ctx.fetcher.get_ok(fetched).text
    path = ctx.store.base_path(record).with_suffix(".md")
    ctx.store.write_markdown(path, body, _meta(record, fetched_url=fetched))
    return DocResult(record.doc_id, "ok", [str(path)])


def _similar_doc_dirs(files: list[str], prefix: str, limit: int = 3) -> list[str]:
    """Folders holding documentation whose name resembles the missing prefix (for the error message)."""
    want = [s.lower()[:4] for s in prefix.strip("/").split("/") if s]
    if not want:
        return []
    dirs = {f.rsplit("/", 1)[0] + "/" for f in files if "/" in f and f.lower().endswith(DOC_EXTS)}
    scored = sorted(((sum(w in d.lower() for w in want), d) for d in dirs), key=lambda t: (-t[0], len(t[1])))
    return [d for score, d in scored if score == len(want)][:limit]


def docs_folder(record, ctx: Context) -> DocResult:
    g = parse_github_url(record.url)
    if g.kind == "blob":
        return readme(record, ctx)  # a single file
    if g.kind == "wiki":
        return wiki(record, ctx)
    ref = g.ref or _default_branch(ctx, g)
    tree = ctx.fetcher.get_ok(f"{API}/repos/{g.owner}/{g.repo}/git/trees/{ref}?recursive=1").json()
    files = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    prefix = (g.path.rstrip("/") + "/") if g.path else ""
    if not prefix:  # repository root given: prefer a documentation folder if there is one
        for d in PREFERRED_DOC_DIRS:
            if any(f.startswith(d) for f in files):
                prefix = d
                break
    def pick(exts):
        return sorted(f for f in files if f.startswith(prefix) and f.lower().endswith(exts)
                      and not any(("/" + s) in ("/" + f) for s in SKIP_DIRS))
    picked = pick(DOC_EXTS)
    if not picked and g.path:  # e.g. OpenAMASE/docs/lmcp holds only CMASI.xml message definitions
        picked = pick(SPEC_EXTS)
    truncated = tree.get("truncated", False) or len(picked) > ctx.max_repo_files
    picked = picked[: ctx.max_repo_files]
    if not picked:
        msg = f"No documentation files under '{prefix or '/'}'"
        hints = _similar_doc_dirs(files, prefix)
        if hints:
            msg += f"; did the folder move? Candidates: {', '.join(hints)}"
        return DocResult(record.doc_id, "error", message=msg)
    out_dir = ctx.store.base_path(record)
    written, failures = [], 0
    for f in picked:
        url = _raw_url(g, ref, f)
        try:
            body = ctx.fetcher.get_ok(url).text
        except FetchError as exc:
            failures += 1
            ctx.log(f"  ! {f}: {exc}")
            continue
        suffix = Path(f).suffix.lower()
        if suffix in (".html", ".htm"):
            body = html_to_markdown(body, url=f"https://github.com/{g.owner}/{g.repo}/blob/{ref}/{f}")
        # keep the original extension for non-prose files so CMASI.xml and CMASI.html do not collide
        rel = Path(f[len(prefix):])
        rel = rel.with_suffix(".md") if suffix in DOC_EXTS else rel.with_name(rel.name + ".md")
        meta = _meta(record, fetched_url=url, repo_path=f, source_format=suffix.lstrip("."))
        written.append(str(ctx.store.write_markdown(out_dir / rel, body, meta)))
    status = "ok" if written and not failures and not truncated else ("partial" if written else "error")
    msg = f"{len(written)} files from {g.owner}/{g.repo}/{prefix}"
    if truncated:
        msg += f" (capped at {ctx.max_repo_files})"
    if failures:
        msg += f", {failures} failed"
    return DocResult(record.doc_id, status, written, msg)


def releases(record, ctx: Context) -> DocResult:
    g = parse_github_url(record.url)
    data = ctx.fetcher.get_ok(f"{API}/repos/{g.owner}/{g.repo}/releases?per_page=100").json()
    if not data:
        return DocResult(record.doc_id, "error", message="No releases")
    parts = [f"# Releases of {g.owner}/{g.repo}\n"]
    for rel in data:
        name = rel.get("name") or rel.get("tag_name")
        parts.append(f"## {name} ({rel.get('tag_name')}, {(rel.get('published_at') or '')[:10]})\n\n"
                     f"{(rel.get('body') or '').strip()}\n")
    path = ctx.store.base_path(record).with_suffix(".md")
    ctx.store.write_markdown(path, "\n".join(parts), _meta(record, release_count=len(data)))
    return DocResult(record.doc_id, "ok", [str(path)], f"{len(data)} releases")


def org_listing(record, ctx: Context) -> DocResult:
    g = parse_github_url(record.url)
    repos = None
    for kind in ("orgs", "users"):
        try:
            repos = ctx.fetcher.get_ok(f"{API}/{kind}/{g.owner}/repos?per_page=100").json()
            break
        except FetchError:
            continue
    if repos is None:
        return DocResult(record.doc_id, "error", message=f"Could not list repositories for {g.owner}")
    repos = [r for r in repos if not r.get("fork")]
    repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
    out_dir = ctx.store.base_path(record)
    lines = [f"# Repositories in {g.owner}\n",
             "| Repository | Stars | Archived | Updated | Description |", "|---|---|---|---|---|"]
    for r in repos:
        desc = (r.get("description") or "").replace("|", "/")
        lines.append(f"| [{r['name']}]({r['html_url']}) | {r.get('stargazers_count', 0)} | "
                     f"{r.get('archived', False)} | {(r.get('pushed_at') or '')[:10]} | {desc} |")
    written = [str(ctx.store.write_markdown(out_dir / "_index.md", "\n".join(lines) + "\n",
                                            _meta(record, repo_count=len(repos))))]
    failures = 0
    for r in [r for r in repos if not r.get("archived")][: ctx.max_org_readmes]:
        try:
            body = ctx.fetcher.get_ok(f"{API}/repos/{g.owner}/{r['name']}/readme",
                                      headers={"Accept": "application/vnd.github.raw"}).text
        except FetchError:
            failures += 1
            continue
        written.append(str(ctx.store.write_markdown(out_dir / f"{r['name']}.md", body,
                                                    _meta(record, fetched_url=r["html_url"], repo=r["name"]))))
    return DocResult(record.doc_id, "ok" if not failures else "partial", written,
                     f"{len(repos)} repositories, {len(written) - 1} READMEs")


def wiki(record, ctx: Context) -> DocResult:
    g = parse_github_url(record.url)
    tmp = tempfile.mkdtemp(prefix="wiki-")
    try:
        try:
            ctx.git_runner(["git", "clone", "--depth", "1", f"https://github.com/{g.owner}/{g.repo}.wiki.git", tmp])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise FetchError(f"git clone of wiki failed: {str(detail).strip()[:300]}") from exc
        out_dir = ctx.store.base_path(record)
        written = []
        for f in sorted(Path(tmp).rglob("*")):
            if f.is_file() and f.suffix.lower() in DOC_EXTS and ".git" not in f.parts:
                rel = f.relative_to(tmp).with_suffix(".md")
                meta = _meta(record, fetched_url=f"https://github.com/{g.owner}/{g.repo}/wiki/{f.stem}",
                             wiki_page=f.stem)
                written.append(str(ctx.store.write_markdown(out_dir / rel, f.read_text(errors="replace"), meta)))
        if not written:
            return DocResult(record.doc_id, "error", message="Wiki clone had no pages")
        return DocResult(record.doc_id, "ok", written, f"{len(written)} wiki pages")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
