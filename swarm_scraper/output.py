"""Write documents with metadata headers and keep a manifest for resuming."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len].rstrip("-") or "untitled"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class DocResult:
    doc_id: str
    status: str  # ok | partial | manual | skipped | error
    files: list[str] = field(default_factory=list)
    message: str = ""
    retrieved_at: str = field(default_factory=now_iso)


class OutputStore:
    """Layout:
        out/<technology-slug>/<doc_id>-<title-slug>.md          single document
        out/<technology-slug>/<doc_id>-<title-slug>/<page>.md   multi-page document
        out/<technology-slug>/<doc_id>-<title-slug>.pdf         original PDF, when kept
        out/manifest.jsonl                                       one line per attempt
        out/manual_queue.csv                                     documents to collect by hand
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.jsonl"
        self.manual_path = self.root / "manual_queue.csv"
        self._lock = threading.Lock()

    def base_path(self, record) -> Path:
        d = self.root / slugify(record.technology, 50)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{record.doc_id}-{slugify(record.title)}"

    def write_markdown(self, path: Path, body: str, metadata: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {k: v for k, v in metadata.items() if v not in (None, "")}
        meta["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
        path.write_text(f"---\n{header}---\n\n{body}", encoding="utf-8")
        return path

    def write_bytes(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def record_result(self, result: DocResult) -> None:
        line = json.dumps(result.__dict__, ensure_ascii=False)
        with self._lock, self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def add_manual(self, record, reason: str) -> None:
        """Queue a document for manual collection, once per doc ID across runs."""
        with self._lock:
            new = not self.manual_path.exists()
            if not new:
                with self.manual_path.open(newline="", encoding="utf-8") as f:
                    if any(row.get("doc_id") == record.doc_id for row in csv.DictReader(f)):
                        return
            with self.manual_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["doc_id", "technology", "title", "url", "access", "reason"])
                w.writerow([record.doc_id, record.technology, record.title, record.url, record.access, reason])

    def completed_ids(self) -> set[str]:
        """Doc IDs whose latest manifest entry is ok or partial."""
        latest: dict[str, str] = {}
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                latest[entry["doc_id"]] = entry["status"]
        return {k for k, v in latest.items() if v in ("ok", "partial")}
