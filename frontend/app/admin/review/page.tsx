"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

/** Mirrors ReviewQueue.PendingItem / Summary (ADR 0018). Deliberately nothing a machine said. */
type Passage = { n: number; source: string; content: string };
type Pending = {
  id: number;
  createdAt: string;
  query: string;
  answer: string;
  passages: Passage[];
  claims: string[];
};
type Summary = { judged: number; selected: number; labelled: number; pending: number; exportable: number };

function Stat({ label, value, hint, testId }: { label: string; value: string; hint: string; testId?: string }) {
  return (
    <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-5">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">{label}</div>
      <div data-testid={testId} className="mt-1.5 font-mono text-3xl font-semibold text-slate-200">
        {value}
      </div>
      <div className="mt-1 text-[11px] text-slate-600">{hint}</div>
    </div>
  );
}

export default function Review() {
  const [item, setItem] = useState<Pending | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [claimLabels, setClaimLabels] = useState<(boolean | null)[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [q, s] = await Promise.all([
        fetch(`${API}/api/admin/review?limit=1`),
        fetch(`${API}/api/admin/review/summary`),
      ]);
      if (!q.ok || !s.ok) throw new Error(String(q.status));
      const next: Pending[] = await q.json();
      setItem(next[0] ?? null);
      setClaimLabels(next[0] ? next[0].claims.map(() => null) : []);
      setSummary(await s.json());
    } catch {
      setError("Could not load the review queue — is the backend up with recall.review.enabled?");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function submit(supported: boolean) {
    if (!item) return;
    setError(null);
    const claims = claimLabels.every((c) => c !== null) && claimLabels.length > 0 ? claimLabels : null;
    try {
      const res = await fetch(`${API}/api/admin/review/${item.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ supported, claims }),
      });
      if (!res.ok) throw new Error(String(res.status));
      await refresh();
    } catch {
      setError("Saving the label failed.");
    }
  }

  const allClaimsLabelled = claimLabels.length > 0 && claimLabels.every((c) => c !== null);
  const anyClaimFalse = claimLabels.some((c) => c === false);

  return (
    <main className="mx-auto max-w-5xl px-6 py-10">
      <header className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
            Recall <span className="text-slate-500">· Review</span>
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-400">
            Answers chosen by a weighted coin, shown blind: no judge verdict, no claim score.
            Each label corrects the judge where it was most likely wrong, and the export turns
            them into the true groundedness rate (ADR 0018).
          </p>
        </div>
        <Link
          href="/admin"
          className="rounded-xl border border-white/10 bg-white/[0.04] px-4 py-2 text-sm text-slate-300 transition hover:bg-white/[0.08]"
        >
          ← Ops
        </Link>
      </header>

      <section className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Judged" value={summary ? String(summary.judged) : "—"} hint="every answer, in the ledger" />
        <Stat label="Selected" value={summary ? String(summary.selected) : "—"} hint="by the coin" />
        <Stat
          label="Waiting"
          testId="review-pending"
          value={summary ? String(summary.pending) : "—"}
          hint="oldest first"
        />
        <Stat
          label="Exportable"
          value={summary ? String(summary.exportable) : "—"}
          hint="rows before the first unlabelled one"
        />
      </section>

      {error && (
        <p className="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {error}
        </p>
      )}

      {!item ? (
        <p className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6 text-sm text-slate-500">
          {loading ? "Loading…" : "Nothing waiting. Every selected answer is labelled."}
        </p>
      ) : (
        <section data-testid="review-item" className="space-y-5">
          <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Question</div>
            <p className="mt-1.5 text-slate-100">{item.query}</p>
          </div>

          <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              Passages the model was shown
            </div>
            <ol className="space-y-3">
              {item.passages.map((p) => (
                <li key={p.n} className="text-sm leading-relaxed text-slate-300">
                  <span className="mr-2 font-mono text-indigo-300">[{p.n}]</span>
                  <span className="mr-2 font-mono text-[11px] text-slate-500">{p.source}</span>
                  {p.content}
                </li>
              ))}
            </ol>
          </div>

          <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              Answer — does at least one passage state or directly imply each sentence?
            </div>
            {item.claims.length === 0 ? (
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-200">{item.answer}</p>
            ) : (
              <ul className="space-y-2">
                {item.claims.map((c, i) => (
                  <li key={i} className="flex items-start gap-3 text-sm">
                    <div className="flex shrink-0 gap-1">
                      {[true, false].map((v) => (
                        <button
                          key={String(v)}
                          data-testid={`claim-${i}-${v ? "yes" : "no"}`}
                          onClick={() => setClaimLabels((ls) => ls.map((l, k) => (k === i ? v : l)))}
                          className={`rounded-md px-2 py-0.5 text-[11px] ring-1 transition ${
                            claimLabels[i] === v
                              ? v
                                ? "bg-emerald-500/20 text-emerald-200 ring-emerald-400/50"
                                : "bg-rose-500/20 text-rose-200 ring-rose-400/50"
                              : "text-slate-500 ring-white/10 hover:text-slate-300"
                          }`}
                        >
                          {v ? "supported" : "not"}
                        </button>
                      ))}
                    </div>
                    <span className="leading-relaxed text-slate-200">{c}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <button
              data-testid="answer-supported"
              onClick={() => submit(true)}
              disabled={anyClaimFalse}
              className="rounded-xl bg-emerald-600/80 px-4 py-2 text-sm font-medium text-white transition enabled:hover:bg-emerald-500 disabled:opacity-30"
            >
              Whole answer supported
            </button>
            <button
              data-testid="answer-unsupported"
              onClick={() => submit(false)}
              className="rounded-xl bg-rose-600/80 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-500"
            >
              Not supported
            </button>
            <span className="text-[11px] text-slate-500">
              {item.claims.length > 0 && !allClaimsLabelled
                ? "Claim labels are optional; label all of them to also recalibrate the claim threshold."
                : "When in doubt, not supported — see docs/LABELING-GUIDE.md."}
            </span>
          </div>
        </section>
      )}

      <footer className="mt-12 border-t border-white/[0.06] pt-5 font-mono text-[11px] text-slate-600">
        curl {API}/api/admin/review/export &gt; review.jsonl &amp;&amp; python eval/active.py review.jsonl --budget 0.05
      </footer>
    </main>
  );
}
