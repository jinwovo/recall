"""Fetch a BEIR benchmark and convert it to this repo's corpus and gold format.

Every statistical tool in this directory has been pointing at a ten-query gold set, and
saying the same thing about it: ten queries cannot resolve the differences anyone wants to
act on. `power_report.py` puts the number at thousands of queries to settle a 0.02 MRR
move. This is the fix — a standard benchmark, so the harness measures something it can
actually resolve, and the numbers are comparable to published work instead of to nothing.

BEIR (Thakur, Reimers, Rücklé, Srivastava & Gurevych, NeurIPS 2021 Datasets & Benchmarks)
is the retrieval benchmark suite the field reports against. SciFact is the default here
because it is small enough to index on a laptop, its relevance judgements are binary — so
the metrics this repo computes mean the same thing as the published ones — and scientific
claim verification is a realistic shape for a technical knowledge base.

Standard library only. Corpus and queries come from the Hugging Face datasets-server as
JSON, relevance judgements from the qrels repositories as TSV; nothing here needs pyarrow,
`datasets`, or a BEIR install.

    python beir.py scifact                      # full benchmark -> beir-scifact/
    python beir.py nfcorpus --max-queries 100   # a subset for a faster loop
    python beir.py --list

Then point the rest of the harness at it:

    python ../scripts/seed_corpus.py beir-scifact/corpus.jsonl
    python run_eval.py beir-scifact/gold.jsonl
    python calibrate.py beir-scifact/gold.jsonl
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import stats

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

ROWS_API = "https://datasets-server.huggingface.co/rows"
QRELS_URL = "https://huggingface.co/datasets/BeIR/{dataset}-qrels/resolve/main/{split}.tsv"
PAGE = 100                       # the rows endpoint's maximum page size
PAGE_DELAY = 0.35                # polite pacing; cheaper than being rate limited
RATE_LIMIT_WAIT = 30             # fallback wait when a 429 carries no Retry-After
TARGET_EFFECT = 0.02             # the MRR move the design analysis prices out


class Benchmark:
    """A BEIR dataset and the one thing that decides whether its nDCG is comparable."""

    __slots__ = ("name", "docs", "queries", "graded", "note")

    def __init__(self, name: str, docs: int, queries: int, graded: bool, note: str):
        self.name = name
        self.docs = docs
        self.queries = queries
        # Graded qrels (0/1/2) have to be binarised for this repo's metrics, which makes
        # the resulting nDCG incomparable to published BEIR numbers computed with graded
        # gains. Recall and MRR are unaffected.
        self.graded = graded
        self.note = note


CATALOG = {
    b.name: b for b in [
        Benchmark("scifact", 5183, 300, False,
                  "scientific claim verification; binary qrels, so every metric is "
                  "directly comparable to published BEIR results"),
        Benchmark("nfcorpus", 3633, 323, True,
                  "medical information retrieval; graded qrels, dense judgements"),
        Benchmark("fiqa", 57638, 648, False,
                  "financial question answering; binary qrels"),
        Benchmark("arguana", 8674, 1406, False,
                  "counter-argument retrieval; one relevant document per query"),
        Benchmark("scidocs", 25657, 1000, False,
                  "citation prediction; five relevant documents per query"),
        Benchmark("trec-covid", 171332, 50, True,
                  "COVID-19 literature; graded qrels and only 50 queries — deeply judged "
                  "but too few queries to resolve small differences"),
    ]
}
DEFAULT_SPLIT = {"nfcorpus": "test", "scifact": "test"}


def fetch_json(url: str, attempts: int = 8) -> dict:
    """GET with backoff. Rate limiting gets its own, much longer, wait.

    Paging a 5,000-document corpus is fifty-odd requests, and the datasets-server starts
    returning 429 partway through. A doubling backoff from one second gives up long before
    the window resets, so 429 is treated separately: honour Retry-After when it is offered,
    and wait in tens of seconds when it is not.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "recall-beir"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429:
                time.sleep(2 ** attempt)
                continue
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = int(retry_after) if (retry_after or "").isdigit() else RATE_LIMIT_WAIT
            print(f"\n  rate limited, waiting {delay}s", flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"failed after {attempts} attempts: {url}") from last


def fetch_text(url: str, attempts: int = 4) -> str:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "recall-beir"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"failed after {attempts} attempts: {url}") from last


def fetch_rows(dataset: str, config: str, label: str,
               checkpoint: Path | None = None) -> list[dict]:
    """Page the whole of one config out of the datasets-server, resumably.

    Fifty requests against a rate-limited endpoint will sometimes not finish, and losing
    forty completed pages to the fifty-first is the difference between a tool people run
    and one they run once. Progress is written after every page and picked up on restart.
    """
    encoded = urllib.parse.quote(f"BeIR/{dataset}", safe="")
    rows: list[dict] = []
    if checkpoint and checkpoint.exists():
        rows = json.loads(checkpoint.read_text(encoding="utf-8"))
        print(f"  {label}: resuming from {len(rows)}")
    while True:
        page = fetch_json(f"{ROWS_API}?dataset={encoded}&config={config}&split={config}"
                          f"&offset={len(rows)}&length={PAGE}")
        batch = [entry["row"] for entry in page.get("rows", [])]
        rows.extend(batch)
        total = page.get("num_rows_total", len(rows))
        print(f"\r  {label}: {len(rows)}/{total}", end="", flush=True)
        if checkpoint:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        if len(batch) < PAGE or len(rows) >= total:
            break
        time.sleep(PAGE_DELAY)                   # stay under the rate limit rather than hit it
    print()
    return rows


def fetch_qrels(dataset: str, split: str, min_relevance: int) -> dict[str, set[str]]:
    """query-id -> relevant corpus-ids, above the relevance cutoff."""
    text = fetch_text(QRELS_URL.format(dataset=dataset, split=split))
    relevant: dict[str, set[str]] = {}
    for line in text.splitlines()[1:]:           # skip the header row
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        query_id, doc_id, score = parts[0], parts[1], parts[2]
        try:
            if int(score) < min_relevance:
                continue
        except ValueError:
            continue
        relevant.setdefault(query_id, set()).add(doc_id)
    return relevant


def build(dataset: str, split: str, min_relevance: int, max_queries: int | None,
          cache: Path) -> tuple[list[dict], list[dict], dict]:
    """Corpus records, gold records, and what had to be dropped to get there."""
    corpus_rows = cached(cache / "corpus.json",
                         lambda: fetch_rows(dataset, "corpus", "corpus",
                                            cache / "corpus.partial.json"))
    query_rows = cached(cache / "queries.json",
                        lambda: fetch_rows(dataset, "queries", "queries",
                                           cache / "queries.partial.json"))
    qrels = cached(cache / f"qrels-{split}-{min_relevance}.json",
                   lambda: {k: sorted(v) for k, v in
                            fetch_qrels(dataset, split, min_relevance).items()})

    documents = {row["_id"]: row for row in corpus_rows}
    judged = [(qid, [d for d in docs if d in documents]) for qid, docs in qrels.items()]
    # A query whose judged documents are all missing from the corpus would score zero for
    # reasons that have nothing to do with retrieval. Drop it and say how many.
    usable = [(qid, docs) for qid, docs in judged if docs]
    dangling = sum(len(docs) for _, docs in qrels.items()) - sum(len(d) for _, d in judged)

    texts = {row["_id"]: row for row in query_rows}
    gold = []
    for query_id, doc_ids in sorted(usable, key=lambda item: item[0]):
        row = texts.get(query_id)
        if not row:
            continue
        gold.append({"query": row.get("text", "").strip(),
                     "relevant_doc_ids": sorted(doc_ids)})
    if max_queries and len(gold) > max_queries:
        # Evenly spaced rather than the first N: qrels files are ordered by query id, and
        # a head slice can pick up whatever bias that ordering carries.
        step = len(gold) / max_queries
        gold = [gold[int(i * step)] for i in range(max_queries)]

    corpus = [{
        "docId": row["_id"],
        "source": f"beir/{dataset}",
        "lang": "en",
        "content": "\n\n".join(part for part in (row.get("title", "").strip(),
                                                 row.get("text", "").strip()) if part),
    } for row in corpus_rows]
    corpus = [record for record in corpus if record["content"]]

    dropped = {
        "queries_without_judgements": len(qrels) - len(usable),
        "judgements_pointing_outside_the_corpus": dangling,
        "empty_documents": len(corpus_rows) - len(corpus),
    }
    return corpus, gold, dropped


def cached(path: Path, produce):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    value = produce()
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def report(benchmark: Benchmark, corpus: list[dict], gold: list[dict], dropped: dict,
           out: Path, split: str, subset: bool) -> None:
    counts = [len(g["relevant_doc_ids"]) for g in gold]
    print(f"\n{benchmark.name} ({split})")
    print(f"  documents             {len(corpus):,}")
    print(f"  queries               {len(gold):,}"
          + ("   (subset — not the full benchmark)" if subset else ""))
    print(f"  relevant per query    {stats.mean(counts):.2f} "
          f"(min {min(counts)}, max {max(counts)})")
    for name, value in dropped.items():
        if value:
            print(f"  dropped               {value:,} {name.replace('_', ' ')}")
    print(f"  written to            {out}/corpus.jsonl, {out}/gold.jsonl")

    print("\nWhat this benchmark can resolve")
    floor = stats.min_attainable_p(len(gold))
    print(f"  smallest attainable p {floor:.2e} (against 2.0e-01 on a 10-query set)")
    for spread in (0.20, 0.30):
        needed = stats.required_queries(TARGET_EFFECT, spread)
        verdict = "enough" if len(gold) >= needed else f"short by {needed - len(gold):,}"
        print(f"  dMRR@10 = {TARGET_EFFECT} at sd {spread:.2f} needs {needed:,} queries "
              f"-> {verdict}")

    if benchmark.graded:
        print("\n  NOTE: this benchmark has graded relevance judgements, binarised here at "
              "the\n  cutoff. Recall and MRR are unaffected; nDCG is NOT comparable to "
              "published\n  BEIR numbers, which use graded gains. Prefer scifact for "
              "comparisons.")
    if subset:
        print("\n  NOTE: a query subset changes nothing about the corpus, so retrieval is "
              "as hard\n  as the full benchmark — but the metrics carry the wider interval "
              "of the smaller\n  sample. Report the full split for anything anyone will "
              "quote.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", nargs="?", default="scifact", choices=list(CATALOG))
    parser.add_argument("--split", default=None, help="qrels split (default: test)")
    parser.add_argument("--min-relevance", type=int, default=1,
                        help="qrels score at or above which a document counts as relevant")
    parser.add_argument("--max-queries", type=int, default=None,
                        help="evenly spaced query subset, for a faster loop")
    parser.add_argument("--out", default=None, help="output directory (default beir-DATASET)")
    parser.add_argument("--cache", default=".beir-cache", help="where downloads are kept")
    parser.add_argument("--list", action="store_true", help="show the catalog and exit")
    args = parser.parse_args()

    if args.list:
        print(f"{'dataset':<12}{'docs':>9}{'queries':>9}  notes")
        for benchmark in CATALOG.values():
            print(f"{benchmark.name:<12}{benchmark.docs:>9,}{benchmark.queries:>9,}  "
                  f"{benchmark.note}")
        return 0

    benchmark = CATALOG[args.dataset]
    split = args.split or DEFAULT_SPLIT.get(args.dataset, "test")
    out = Path(args.out or f"beir-{args.dataset}")
    cache = Path(args.cache) / args.dataset

    print(f"fetching BeIR/{args.dataset} ({split} qrels)")
    corpus, gold, dropped = build(args.dataset, split, args.min_relevance,
                                  args.max_queries, cache)
    if not gold:
        raise SystemExit(f"no usable queries for {args.dataset}/{split}")

    write_jsonl(out / "corpus.jsonl", corpus)
    write_jsonl(out / "gold.jsonl", gold)
    report(benchmark, corpus, gold, dropped, out, split, bool(args.max_queries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
