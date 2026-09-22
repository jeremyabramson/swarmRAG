"""One handler per 'Scrape method' value in the inventory."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable

from ..http import Fetcher
from ..output import OutputStore


def default_git_runner(args: list[str], cwd: str | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=600)


@dataclass
class Context:
    fetcher: Fetcher
    store: OutputStore
    max_pages: int = 200          # per documentation-site crawl
    max_repo_files: int = 400     # per repository docs folder
    max_org_readmes: int = 25     # per GitHub organization
    keep_pdf: bool = True
    git_runner: Callable = field(default=default_git_runner)
    log: Callable[[str], None] = field(default=print)
