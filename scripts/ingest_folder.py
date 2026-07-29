"""Ingest a folder of documents through the real pipeline — bring your own corpus.

Walks a directory, extracts text (.md/.txt/.html built in; .pdf if `pypdf` is installed),
derives a stable docId from the relative path, POSTs each document to /api/ingest
(Kafka → chunk → embed → idempotent upsert, docs/adr/0003), then waits for the async
indexing barrier so "done" means "searchable". Re-running is safe: unchanged content
converges, changed files index as new chunks.

Usage:
    python scripts/ingest_folder.py ~/my-docs
    python scripts/ingest_folder.py ./wiki --ext md,html --dry-run
Env:
    RECALL_API (default http://localhost:8080), RECALL_ES, RECALL_ES_INDEX (barrier)

Stdlib only; PDF support is optional (`pip install pypdf`).
"""
import argparse
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
ES = os.getenv("RECALL_ES", "http://localhost:9200")
INDEX = os.getenv("RECALL_ES_INDEX", "recall-docs")
DEFAULT_EXTS = ("md", "markdown", "txt", "html", "htm", "pdf")


class _TextExtractor(html.parser.HTMLParser):
    """Strip tags; skip script/style content."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def extract_text(path: Path) -> str | None:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("md", "markdown", "txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in ("html", "htm"):
        parser = _TextExtractor()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
    if suffix == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            print(f"  skip {path.name}: PDF needs `pip install pypdf`", file=sys.stderr)
            return None
        return "\n\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return None


def doc_id_for(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    slug = re.sub(r"[^a-zA-Z0-9ㄱ-힝]+", "-", rel.rsplit(".", 1)[0]).strip("-").lower()
    return slug or path.stem.lower()


def detect_lang(text: str) -> str:
    """Cheap heuristic: enough Hangul → ko, else en (bge-m3 is multilingual anyway)."""
    hangul = sum(1 for ch in text[:2000] if "가" <= ch <= "힣")
    return "ko" if hangul > len(text[:2000]) * 0.05 else "en"


def post_ingest(doc: dict, attempts: int = 3) -> int:
    body = json.dumps(doc).encode("utf-8")
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            f"{API}/api/ingest", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as r:
                return r.status
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"ingest failed for {doc['docId']}") from last


def indexed_doc_ids() -> set[str]:
    query = {"size": 0, "aggs": {"docs": {"terms": {"field": "docId", "size": 10_000}}}}
    request = urllib.request.Request(
        f"{ES}/{INDEX}/_search", data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            data = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return set()
    buckets = data.get("aggregations", {}).get("docs", {}).get("buckets", [])
    return {b["key"] for b in buckets}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="directory to ingest (recursive)")
    parser.add_argument("--ext", default=",".join(DEFAULT_EXTS),
                        help="comma-separated extensions to include")
    parser.add_argument("--dry-run", action="store_true", help="list what would be ingested")
    parser.add_argument("--timeout", type=int, default=600,
                        help="seconds to wait for the indexing barrier")
    args = parser.parse_args()

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    exts = {e.strip().lstrip(".").lower() for e in args.ext.split(",") if e.strip()}

    documents = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower().lstrip(".") not in exts:
            continue
        text = extract_text(path)
        if not text or not text.strip():
            continue
        documents.append({
            "docId": doc_id_for(root, path),
            "source": path.relative_to(root).as_posix(),
            "lang": detect_lang(text),
            "content": text.strip(),
        })

    if not documents:
        print(f"nothing to ingest under {root} (extensions: {sorted(exts)})", file=sys.stderr)
        return 1

    print(f"{'would ingest' if args.dry_run else 'ingesting'} {len(documents)} documents from {root}")
    for doc in documents:
        size_kb = len(doc["content"].encode("utf-8")) / 1024
        print(f"  {doc['docId']}  [{doc['lang']}]  {size_kb:.1f} KB  ({doc['source']})")
    if args.dry_run:
        return 0

    for doc in documents:
        status = post_ingest(doc)
        if status != 202:
            print(f"unexpected status {status} for {doc['docId']}", file=sys.stderr)
            return 1

    expected = {d["docId"] for d in documents}
    print(f"waiting for async indexing ({len(expected)} docIds in {INDEX})")
    deadline = time.monotonic() + args.timeout
    last_report = 0.0
    while time.monotonic() < deadline:
        done = indexed_doc_ids() & expected
        if done == expected:
            print(f"indexed {len(expected)}/{len(expected)} — searchable now. "
                  f"Try: {API}/api/search?q=...")
            return 0
        if time.monotonic() - last_report >= 5:
            print(f"  indexed {len(done)}/{len(expected)}")
            last_report = time.monotonic()
        time.sleep(1)

    print(f"timed out after {args.timeout}s; missing: {sorted(expected - indexed_doc_ids())[:5]}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
