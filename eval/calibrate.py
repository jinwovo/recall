"""Turn two magic constants into certificates (docs/adr/0013).

The serving path has two numbers that decide what the system costs and how often it makes
things up:

    recall.retrieval.top-k                       how many passages reach the LLM
    recall.rag.sufficiency.confidence-threshold  when to answer at all

Both were picked by hand. This replaces them with thresholds calibrated on held-out queries
and carrying a finite-sample, distribution-free guarantee, and prints the configuration to
paste. It needs one run over the gold set and no extra labelling: the same retrieval that
scores Recall@5 also says, for every query, where the relevant document landed and how
confident the reranker was — which is everything both calibrations need.

    coverage   P(the passages sent to the LLM contain a relevant document) >= 1 - alpha
    risk       P( P(answering from context with no relevant document) <= risk-alpha )
               >= 1 - delta

The second is the one worth reading twice. It bounds *answering when the context cannot
support an answer* — the condition that makes hallucination possible — and it bounds it with
confidence over the calibration draw, not on average, not in-sample.

Three splits, because two would quietly break the guarantee:

    tuning        picks the softmax temperature
    calibration   fits the conformal quantile
    held-out      scores both, having informed neither

Coverage holds for any temperature fixed in advance, so choosing one costs nothing in
validity — but only if the choice is made on data the quantile is not then fitted on.
Sharing a split between the two breaks exchangeability, and the resulting under-coverage
would be invisible in every in-sample number the tool prints.

Usage:
    python calibrate.py gold.jsonl                             # calibrate and report
    python calibrate.py gold.jsonl --json certificate.json     # machine-readable
    python calibrate.py gold.jsonl --alpha 0.05 --risk-alpha 0.02
Env:
    RECALL_API (default http://localhost:8080)

Stdlib only.
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import conformal as cf
import stats

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

API = os.getenv("RECALL_API", "http://localhost:8080")
SPLIT_SEED = 20240917
# Abstention thresholds to search, most conservative first — the fixed sequence the risk
# certificate depends on. Reranker scores are normalised to [0, 1].
RISK_GRID = [round(1.0 - 0.02 * i, 2) for i in range(46)]
# Softmax temperatures to search. The right one depends entirely on the scale a reranker
# head happens to emit: raw cross-encoder logits span several units and want T near 1, while
# scores squashed into [0, 1] want T near 0.05 or the distribution comes out nearly uniform
# and every set is the whole candidate list. Guessing this wrong does not break the
# guarantee — it silently costs all of the benefit, which is worse, because nothing fails.
TEMPERATURE_GRID = (0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 1.0, 2.0)


def search(query: str, attempts: int = 3) -> list[dict]:
    """Reranked chunks for a query, scores included."""
    url = f"{API}/api/search?q=" + urllib.parse.quote(query) + "&mode=hybrid"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                return json.load(r).get("results", [])
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"search failed after {attempts} attempts: {url}") from last


def collect(examples: list[dict]) -> list[dict]:
    """One record per query: reranker scores and where the first relevant document landed.

    Scores stay at chunk level because that is what the serving path sizes over; the
    relevant index is the first chunk belonging to a gold document.
    """
    records = []
    for i, example in enumerate(examples, start=1):
        gold = set(example["relevant_doc_ids"])
        results = search(example["query"])
        scores = [float(r.get("score", 0.0)) for r in results]
        relevant = next((j for j, r in enumerate(results) if r.get("docId") in gold), None)
        records.append({"query": example["query"], "scores": scores,
                        "relevant_index": relevant})
        print(f"  [{i}/{len(examples)}] {len(scores)} candidates, "
              f"relevant at {relevant if relevant is not None else 'miss'}", flush=True)
    return records


def split(records: list[dict], fraction: float, seed: int) -> tuple[list[dict], list[dict]]:
    shuffled = sorted(records, key=lambda r: r["query"])
    random.Random(seed).shuffle(shuffled)
    cut = round(len(shuffled) * fraction)
    return shuffled[:cut], shuffled[cut:]


def usable_pairs(records: list[dict]) -> list[tuple[list[float], int]]:
    """(scores, relevant index) for queries whose relevant document was retrieved at all.

    Queries where it never appeared in the candidate list are a recall failure upstream. No
    context size fixes them, and folding them in here would inflate every prompt to full
    length to paper over a miss that belongs to the retriever.
    """
    return [(r["scores"], r["relevant_index"]) for r in records
            if r["scores"] and r["relevant_index"] is not None]


# --------------------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------------------

def choose_temperature(tuning: list[dict], alpha: float,
                       max_k: int | None) -> tuple[float, list[dict]]:
    """Pick the temperature giving the shortest contexts, on a split of its own."""
    pairs = usable_pairs(tuning)
    sweep = []
    for temperature in TEMPERATURE_GRID:
        sizer = cf.AdaptiveSetSizer(alpha=alpha, temperature=temperature, max_k=max_k)
        report = sizer.calibrate(pairs)
        sweep.append({"temperature": temperature, "certified": report.certified,
                      "mean_k": report.mean_k_on_calibration,
                      "coverage": report.coverage_on_calibration})
    viable = [row for row in sweep if row["certified"]]
    best = min(viable, key=lambda row: row["mean_k"]) if viable else None
    return (best["temperature"] if best else 1.0), sweep


def fit_coverage(calibration: list[dict], alpha: float, temperature: float,
                 max_k: int | None) -> tuple[cf.AdaptiveSetSizer, dict]:
    pairs = usable_pairs(calibration)
    sizer = cf.AdaptiveSetSizer(alpha=alpha, temperature=temperature, max_k=max_k)
    report = sizer.calibrate(pairs).as_dict()
    report["dropped_recall_misses"] = len(calibration) - len(pairs)
    return sizer, report


def validate_coverage(sizer: cf.AdaptiveSetSizer, held_out: list[dict], fixed_k: int) -> dict:
    usable = [r for r in held_out if r["scores"] and r["relevant_index"] is not None]
    if not usable:
        return {"n": 0}
    sizes = [sizer.size(r["scores"]) for r in usable]
    covered = sum(1 for r, k in zip(usable, sizes) if r["relevant_index"] < k)
    fixed_covered = sum(1 for r in usable if r["relevant_index"] < fixed_k)
    mean_k = sum(sizes) / len(sizes)
    # A held-out coverage number is itself an estimate from a finite sample, and reading it
    # against the promise without its interval is how a perfectly valid calibration gets
    # mistaken for a broken one: the guarantee is marginal over calibration draws, so a
    # single split lands somewhere in a Beta around 1 - alpha rather than exactly on it.
    lo, hi = stats.clopper_pearson(covered, len(usable))
    return {
        "n": len(usable),
        "coverage": covered / len(usable),
        "coverage_ci": [lo, hi],
        "mean_k": mean_k,
        "fixed_k": fixed_k,
        "fixed_coverage": fixed_covered / len(usable) if fixed_k else 0.0,
        "context_saved_vs_fixed": 1.0 - mean_k / fixed_k if fixed_k else 0.0,
    }


# --------------------------------------------------------------------------------------
# risk
# --------------------------------------------------------------------------------------

def answerable(record: dict, sizer: cf.AdaptiveSetSizer) -> bool:
    """Whether the context this query would actually receive contains a relevant document."""
    if not record["scores"] or record["relevant_index"] is None:
        return False
    return record["relevant_index"] < sizer.size(record["scores"])


def risk_at(records: list[dict], sizer: cf.AdaptiveSetSizer,
            threshold: float) -> tuple[float, int]:
    """Share of queries answered from a context with no relevant document in it."""
    if not records:
        return 0.0, 0
    bad = sum(1 for r in records
              if r["scores"] and r["scores"][0] >= threshold and not answerable(r, sizer))
    return bad / len(records), len(records)


def abstention_at(records: list[dict], threshold: float) -> float:
    if not records:
        return 0.0
    return sum(1 for r in records
               if not r["scores"] or r["scores"][0] < threshold) / len(records)


def fit_risk(calibration: list[dict], sizer: cf.AdaptiveSetSizer, alpha: float,
             delta: float) -> cf.RiskCertificate:
    return cf.risk_controlling_threshold(
        RISK_GRID,
        lambda t: risk_at(calibration, sizer, t),
        alpha, delta,
        abstention_fn=lambda t: abstention_at(calibration, t))


def irreducible_risk(records: list[dict], sizer: cf.AdaptiveSetSizer) -> float:
    """Share of queries no threshold can save: answered confidently, context still wrong.

    When this already exceeds the target risk, no abstention policy can meet it, and the
    certificate's refusal is a statement about retrieval quality rather than about the
    threshold search.
    """
    if not records:
        return 0.0
    return sum(1 for r in records if not answerable(r, sizer)) / len(records)


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------

def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def render(coverage: dict, coverage_check: dict, certificate: cf.RiskCertificate,
           risk_check: dict, splits: dict, temperature: float, sweep: list[dict],
           floor: float) -> str:
    out = [
        "Calibration certificate", "",
        f"  gold queries          {splits['total']}",
        f"  temperature split     {splits['tuning']}  (chooses the softmax temperature)",
        f"  calibration split     {splits['calibration']}  (fits the conformal quantile)",
        f"  held-out split        {splits['held_out']}  (informed neither)",
        "",
        "1. Context coverage — how many passages reach the LLM", "",
    ]
    if sweep:
        out.append(f"  temperature           {temperature} chosen from "
                   + ", ".join(str(row["temperature"]) for row in sweep))
        best = [row for row in sweep if row["certified"]]
        if best:
            widest = max(best, key=lambda row: row["mean_k"])
            out.append(f"                        (worst on the grid would have sent "
                       f"{widest['mean_k']:.1f} passages at T={widest['temperature']})")
        out.append("")

    if not coverage["certified"]:
        out += [f"  NOT CERTIFIED: {coverage['n_calibration']} calibration queries cannot "
                f"support alpha = {coverage['alpha']};",
                f"  {coverage['minimum_calibration_size']} is the minimum. Adaptive sizing "
                f"stays off and the pipeline keeps its fixed top-K.", ""]
    else:
        out += [f"  promise               P(relevant passage in context) >= "
                f"{1 - coverage['alpha']:.0%}",
                f"  calibrated threshold  {coverage['threshold']:.6f}",
                f"  fitted on             {coverage['n_calibration']} queries"
                + (f", {coverage['dropped_recall_misses']} dropped as recall misses"
                   if coverage["dropped_recall_misses"] else ""),
                ""]
        if coverage_check.get("n"):
            lo, hi = coverage_check["coverage_ci"]
            met = hi >= 1 - coverage["alpha"]
            out += [f"  held-out coverage     {coverage_check['coverage']:.1%} "
                    f"[{lo:.1%}, {hi:.1%}] on {coverage_check['n']} queries"
                    + ("" if met else "   <- below the promise, and not by sampling noise"),
                    f"  held-out mean K       {coverage_check['mean_k']:.2f} passages",
                    f"  vs conformal fixed K  {coverage_check['fixed_k']} passages, "
                    f"{coverage_check['fixed_coverage']:.1%} coverage",
                    f"  context saved         {coverage_check['context_saved_vs_fixed']:+.0%} "
                    f"at the same promise", ""]
        if not coverage["guarantee_intact"]:
            out += [f"  NOTE: max-k truncated {coverage['cap_binds']:.0%} of calibration "
                    f"sets — on those queries the cap, not alpha, is the binding promise.",
                    ""]

    out += ["2. Abstention risk — when to answer at all", ""]
    out += ["  " + line for line in _wrap(certificate.statement(), 84)]
    out.append("")
    if certificate.certified:
        out += [f"  abstains on           {certificate.empirical_abstention:.1%} of "
                f"calibration queries — the price of the guarantee",
                f"  candidates tested     {certificate.candidates_tested} of "
                f"{len(RISK_GRID)} (fixed sequence: no multiplicity correction needed)", ""]
        if risk_check.get("n"):
            verdict = "held" if risk_check["risk"] <= certificate.alpha else "EXCEEDED"
            out += [f"  held-out risk         {risk_check['risk']:.1%} "
                    f"(promised <= {certificate.alpha:.0%}) — {verdict}",
                    f"  held-out abstention   {risk_check['abstention']:.1%}", ""]
        blocked = certificate.blocked_by
        if certificate.degenerate:
            out += ["  " + line for line in _wrap(
                "This certificate answers nothing. It is a valid guarantee attached to a "
                "useless policy, and shipping it would be worse than shipping the constant "
                "it replaced.", 84)]
            out.append("")
        if blocked:
            needed = blocked["queries_needed"]
            reason = (f"needs about {needed:,} calibration queries to prove, against "
                      f"{blocked['n_calibration']} here"
                      if needed else "cannot be proven at any sample size — its risk is "
                                     "already at or above the target")
            out += ["  " + line for line in _wrap(
                f"The walk stopped at {blocked['threshold']}, which would answer "
                f"{1 - (blocked['abstention'] or 0):.0%} of queries at an observed risk of "
                f"{blocked['empirical_risk']:.1%}. That is under the "
                f"{certificate.alpha:.0%} target, but {reason}. The gap is evidence, not "
                f"safety: more calibration data is the fix, not a looser target.", 84)]
            out.append("")
    else:
        out += ["  " + line for line in _wrap(
            f"On this corpus {floor:.1%} of queries end up with a context holding no "
            f"relevant document at all, so no abstention threshold can bound the risk at "
            f"{certificate.alpha:.0%} — the ceiling is set by retrieval, not by the "
            f"threshold search. Raise recall, widen alpha, or accept a lower risk target.",
            84)]
        out.append("")

    out += ["3. Configuration", ""]
    if coverage["certified"]:
        out += ["  recall.rag.conformal:",
                "    enabled: true",
                f"    alpha: {coverage['alpha']}",
                f"    threshold: {coverage['threshold']:.6f}",
                f"    temperature: {coverage['temperature']}", ""]
    if certificate.certified:
        out += ["  recall.rag.sufficiency:",
                f"    confidence-threshold: {certificate.threshold}", ""]
    if not coverage["certified"] and not certificate.certified:
        out += ["  Nothing to apply — leave the current defaults in place.", ""]
    out += ["  A threshold is valid only for the alpha and temperature it was calibrated at,",
            "  and only for a corpus and reranker like the one it was calibrated on.",
            "  Recalibrate when any of those change."]
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gold", nargs="?", default="gold.jsonl")
    parser.add_argument("--alpha", type=float, default=0.10,
                        help="allowed chance the context misses every relevant document")
    parser.add_argument("--risk-alpha", type=float, default=0.05,
                        help="allowed chance of answering from a context with no relevant doc")
    parser.add_argument("--delta", type=float, default=0.05,
                        help="allowed chance the risk bound itself fails, over calibration draws")
    parser.add_argument("--temperature", type=float, default=None,
                        help="softmax temperature; omit to choose it on a split of its own")
    parser.add_argument("--max-k", type=int, default=12)
    parser.add_argument("--fit-fraction", type=float, default=0.6,
                        help="share of the gold set used for fitting, before the held-out split")
    parser.add_argument("--temperature-fraction", type=float, default=0.4,
                        help="share of the fitting data spent choosing the temperature")
    parser.add_argument("--split-seed", type=int, default=SPLIT_SEED)
    parser.add_argument("--json", metavar="PATH", help="write the certificate as JSON")
    parser.add_argument("--records", metavar="PATH",
                        help="cache the collected scores here, or reuse them if present")
    args = parser.parse_args()

    with open(args.gold, encoding="utf-8") as f:
        examples = [json.loads(line) for line in f if line.strip()]

    if args.records and Path(args.records).exists():
        records = json.loads(Path(args.records).read_text(encoding="utf-8"))
        print(f"reusing {len(records)} cached records from {args.records}\n")
    else:
        print(f"collecting reranker scores for {len(examples)} queries from {API}")
        records = collect(examples)
        if args.records:
            Path(args.records).write_text(json.dumps(records, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        print()

    fitting, held_out = split(records, args.fit_fraction, args.split_seed)
    if args.temperature is None:
        tuning, calibration = split(fitting, args.temperature_fraction, args.split_seed + 1)
        temperature, sweep = choose_temperature(tuning, args.alpha, args.max_k)
    else:
        tuning, calibration, sweep = [], fitting, []
        temperature = args.temperature

    sizer, coverage = fit_coverage(calibration, args.alpha, temperature, args.max_k)
    fixed_k = cf.conformal_quantile(
        [r["relevant_index"] + 1 for r in calibration if r["relevant_index"] is not None],
        args.alpha)
    coverage_check = validate_coverage(sizer, held_out,
                                       int(fixed_k) if fixed_k != float("inf") else 0)

    certificate = fit_risk(calibration, sizer, args.risk_alpha, args.delta)
    floor = irreducible_risk(calibration, sizer)
    risk_check = {}
    if certificate.certified and held_out:
        risk, n = risk_at(held_out, sizer, certificate.threshold)
        risk_check = {"n": n, "risk": risk,
                      "abstention": abstention_at(held_out, certificate.threshold)}

    splits = {"total": len(records), "tuning": len(tuning),
              "calibration": len(calibration), "held_out": len(held_out)}
    print(render(coverage, coverage_check, certificate, risk_check, splits,
                 temperature, sweep, floor))

    if args.json:
        Path(args.json).write_text(json.dumps({
            "api": API, "splits": splits, "temperature": temperature,
            "temperature_sweep": sweep, "coverage": coverage,
            "coverage_held_out": coverage_check, "irreducible_risk": floor,
            "risk": certificate.as_dict(), "risk_held_out": risk_check,
        }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
