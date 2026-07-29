"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

/** Mirrors DlqSnapshot / DlqReplayResult from the backend admin API (ADR 0006). */
type DlqRecord = {
  key: string;
  partition: number;
  offset: number;
  timestamp: string;
  pending: boolean;
  originalTopic: string | null;
  originalOffset: number | null;
  exceptionType: string | null;
  exceptionMessage: string | null;
  replays: number;
  payloadPreview: string | null;
};
type Snapshot = { depth: number; records: DlqRecord[] };
type ReplayResult = { replayed: number; remaining: number };

function shortType(fqcn: string | null) {
  return fqcn ? fqcn.split(".").pop() : "—";
}

/** The recoverer stores the listener wrapper's message — surface the root cause after it. */
function shortMessage(message: string | null) {
  if (!message) return null;
  const parts = message.split("threw exception; ");
  return parts[parts.length - 1];
}

function age(iso: string) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default function Admin() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [replaying, setReplaying] = useState(false);
  const [lastReplay, setLastReplay] = useState<ReplayResult | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/admin/dlq?limit=50`);
      if (!res.ok) throw new Error(String(res.status));
      setSnapshot(await res.json());
      setRefreshedAt(new Date());
    } catch {
      setError("Could not load the DLQ — is the backend up?");
    } finally {
      setLoading(false);
    }
  }, []);

  async function replay() {
    setReplaying(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/admin/dlq/replay?max=200`, { method: "POST" });
      if (!res.ok) throw new Error(String(res.status));
      setLastReplay(await res.json());
      // Give replayed records a beat to reprocess (or bounce back) before refreshing.
      setTimeout(refresh, 1500);
    } catch {
      setError("Replay failed — is the backend up?");
    } finally {
      setReplaying(false);
    }
  }

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => clearInterval(timer);
  }, [refresh]);

  const depth = snapshot?.depth ?? 0;
  const records = snapshot?.records ?? [];

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <header className="mb-10 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 text-sm font-bold text-white shadow-lg shadow-indigo-500/25">
              R
            </span>
            <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
              Recall <span className="text-slate-500">· Ops</span>
            </h1>
          </div>
          <p className="mt-2 text-sm text-slate-400">
            Ingestion dead-letter queue — inspect what failed, fix the fault, replay the backlog
          </p>
        </div>
        <Link
          href="/"
          className="rounded-xl border border-white/10 bg-white/[0.04] px-4 py-2 text-sm text-slate-300 transition hover:bg-white/[0.08]"
        >
          ← Search
        </Link>
      </header>

      <section className="mb-5 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-5">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Pending</div>
          <div
            data-testid="dlq-depth"
            className={`mt-1.5 font-mono text-3xl font-semibold ${depth > 0 ? "text-amber-300" : "text-emerald-300"}`}
          >
            {snapshot ? depth : "—"}
          </div>
          <div className="mt-1 text-[11px] text-slate-600">awaiting replay</div>
        </div>
        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-5">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">On topic</div>
          <div className="mt-1.5 font-mono text-3xl font-semibold text-slate-200">
            {snapshot ? records.length : "—"}
          </div>
          <div className="mt-1 text-[11px] text-slate-600">incl. replayed history</div>
        </div>
        <div className="col-span-2 flex flex-col justify-between rounded-2xl border border-white/[0.08] bg-white/[0.03] p-5">
          <div className="flex items-center gap-2">
            <button
              onClick={replay}
              disabled={replaying || depth === 0}
              className="rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 px-4 py-2 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition enabled:hover:brightness-110 disabled:opacity-40"
            >
              {replaying ? "Replaying…" : `Replay pending (${depth})`}
            </button>
            <button
              onClick={refresh}
              disabled={loading}
              className="rounded-xl border border-white/10 bg-white/[0.04] px-4 py-2 text-sm text-slate-200 transition enabled:hover:bg-white/[0.08] disabled:opacity-40"
            >
              Refresh
            </button>
          </div>
          <div className="mt-2 text-[11px] text-slate-500">
            {lastReplay && (
              <span className="text-indigo-300">
                replayed {lastReplay.replayed} · remaining {lastReplay.remaining} —{" "}
              </span>
            )}
            re-publishes to the ingestion topic with provenance headers; a still-broken record
            simply dead-letters again (nothing is lost).
            {refreshedAt && (
              <span className="ml-1 font-mono text-slate-600">refreshed {age(refreshedAt.toISOString())}</span>
            )}
          </div>
        </div>
      </section>

      {error && (
        <p className="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {error}
        </p>
      )}

      <section className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Dead-lettered records
          </h2>
          <span className="font-mono text-[11px] text-slate-500">
            forensic headers from the error handler (ADR 0005)
          </span>
        </div>
        {records.length === 0 ? (
          <p className="text-sm text-slate-600">
            {snapshot ? "The DLQ is empty — no ingestion failures on record." : "Loading…"}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table data-testid="dlq-table" className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-white/[0.08] text-[11px] uppercase tracking-wider text-slate-500">
                  <th className="py-2 pr-4 font-medium">status</th>
                  <th className="py-2 pr-4 font-medium">doc key</th>
                  <th className="py-2 pr-4 font-medium">failure</th>
                  <th className="py-2 pr-4 font-medium">origin</th>
                  <th className="py-2 pr-4 font-medium">replays</th>
                  <th className="py-2 font-medium">age</th>
                </tr>
              </thead>
              <tbody>
                {records.map((r) => (
                  <tr
                    key={`${r.partition}-${r.offset}`}
                    className="border-b border-white/[0.04] align-top last:border-0"
                  >
                    <td className="py-3 pr-4">
                      {r.pending ? (
                        <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-300 ring-1 ring-amber-400/30">
                          pending
                        </span>
                      ) : (
                        <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-300 ring-1 ring-emerald-400/30">
                          replayed
                        </span>
                      )}
                    </td>
                    <td className="max-w-[180px] py-3 pr-4">
                      <div className="truncate font-mono text-[13px] text-slate-200" title={r.key ?? undefined}>
                        {r.key ?? "—"}
                      </div>
                      {r.payloadPreview && (
                        <div className="mt-1 line-clamp-2 max-w-[280px] font-mono text-[11px] leading-relaxed text-slate-600" title={r.payloadPreview}>
                          {r.payloadPreview}
                        </div>
                      )}
                    </td>
                    <td className="max-w-[260px] py-3 pr-4">
                      <div className="font-mono text-[13px] text-rose-300">{shortType(r.exceptionType)}</div>
                      {r.exceptionMessage && (
                        <div className="mt-1 line-clamp-2 text-[12px] leading-relaxed text-slate-400" title={r.exceptionMessage}>
                          {shortMessage(r.exceptionMessage)}
                        </div>
                      )}
                    </td>
                    <td className="py-3 pr-4 font-mono text-[12px] text-slate-400">
                      {r.originalTopic ?? "—"}
                      {r.originalOffset !== null && <span className="text-slate-600">@{r.originalOffset}</span>}
                      <div className="text-[11px] text-slate-600">
                        dlq {r.partition}@{r.offset}
                      </div>
                    </td>
                    <td className="py-3 pr-4 font-mono text-[13px] text-slate-300">{r.replays}</td>
                    <td className="py-3 font-mono text-[12px] text-slate-400">{age(r.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <footer className="mt-12 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-white/[0.06] pt-5 text-xs text-slate-600">
        <Link className="transition hover:text-slate-300" href="/">
          Search
        </Link>
        <span>·</span>
        <a className="transition hover:text-slate-300" href="http://localhost:3001" target="_blank" rel="noreferrer">
          Grafana
        </a>
        <span>·</span>
        <span>API {API}</span>
        <span className="ml-auto hidden font-mono text-[11px] sm:inline">
          retry/backoff → DLQ → inspect → fix → replay (ADR 0005 / 0006)
        </span>
      </footer>
    </main>
  );
}
