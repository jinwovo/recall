"use client";

import { useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";
const GITHUB = "https://github.com/jinwovo/recall";

type Chunk = {
  id: string;
  docId: string;
  chunkIndex: number;
  content: string;
  source: string;
  lang: string;
  score: number;
};

type Groundedness = { verdict: "SUPPORTED" | "PARTIAL" | "UNSUPPORTED"; score: number };

type Mode = "hybrid" | "bm25" | "vector";
const MODES: Mode[] = ["hybrid", "bm25", "vector"];

/** Ask pipeline stages, driven by the SSE events (sources → token → judging → done). */
type Stage = "idle" | "retrieving" | "generating" | "verifying" | "done";
const STEPS: { key: Stage; label: string }[] = [
  { key: "retrieving", label: "retrieve" },
  { key: "generating", label: "generate" },
  { key: "verifying", label: "verify" },
];

const GROUNDEDNESS_BADGE: Record<Groundedness["verdict"], { label: string; className: string }> = {
  SUPPORTED: { label: "grounded", className: "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/30" },
  PARTIAL: { label: "partially grounded", className: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/30" },
  UNSUPPORTED: { label: "unsupported", className: "bg-rose-500/15 text-rose-300 ring-1 ring-rose-400/30" },
};

/** Seed-corpus questions (EN + KO) so a fresh visitor gets a working demo in one click. */
const EXAMPLES = [
  "How do I expose Prometheus metrics from Spring Boot?",
  "파드가 CrashLoopBackOff로 계속 재시작되면 어떻게 확인해?",
  "How do I make an S3 bucket publicly readable?",
];

function fmtMs(ms: number) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export default function Home() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<Chunk[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [searching, setSearching] = useState(false);
  const [cached, setCached] = useState(false);
  const [grounded, setGrounded] = useState<Groundedness | null>(null);
  const [highlight, setHighlight] = useState<number | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [searchMeta, setSearchMeta] = useState<{ ms: number; mode: Mode; count: number } | null>(null);
  const [ttft, setTtft] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  function reset() {
    esRef.current?.close();
    setAnswer("");
    setSources([]);
    setCached(false);
    setGrounded(null);
    setHighlight(null);
    setStage("idle");
    setSearchMeta(null);
    setTtft(null);
    setElapsed(null);
    setError(null);
  }

  async function search(q = query) {
    if (!q.trim()) return;
    setQuery(q);
    reset();
    setSearching(true);
    const started = performance.now();
    try {
      const res = await fetch(`${API}/api/search?q=${encodeURIComponent(q)}&mode=${mode}`);
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const results: Chunk[] = data.results ?? [];
      setSources(results);
      setSearchMeta({ ms: Math.round(performance.now() - started), mode, count: results.length });
    } catch {
      setError("Search failed — is the backend up?");
    } finally {
      setSearching(false);
    }
  }

  function ask(q = query) {
    if (!q.trim()) return;
    setQuery(q);
    reset();
    setStreaming(true);
    setStage("retrieving");
    const started = performance.now();
    let sawToken = false;
    const es = new EventSource(`${API}/api/ask?q=${encodeURIComponent(q)}`);
    esRef.current = es;

    es.addEventListener("sources", (e) => {
      setSources(JSON.parse((e as MessageEvent).data));
      setStage("generating");
    });
    es.addEventListener("cache", () => setCached(true));
    es.addEventListener("token", (e) => {
      if (!sawToken) {
        sawToken = true;
        setTtft(Math.round(performance.now() - started));
      }
      setAnswer((a) => a + JSON.parse((e as MessageEvent).data));
    });
    es.addEventListener("judging", () => setStage("verifying"));
    es.addEventListener("groundedness", (e) => setGrounded(JSON.parse((e as MessageEvent).data)));
    es.addEventListener("done", () => {
      setElapsed(Math.round(performance.now() - started));
      setStage("done");
      setStreaming(false);
      es.close();
    });
    es.addEventListener("error", () => {
      if (!sawToken) setError("Stream failed — is the backend up?");
      setStage("done");
      setStreaming(false);
      es.close();
    });
  }

  function focusSource(i: number) {
    setHighlight(i);
    document.getElementById(`src-${i}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // Render the answer with clickable [n] citation chips that highlight the matching source.
  function renderAnswer(text: string) {
    return text.split(/(\[\d+\])/g).map((part, idx) => {
      const m = part.match(/^\[(\d+)\]$/);
      if (m) {
        const n = parseInt(m[1], 10);
        return (
          <button
            key={idx}
            onClick={() => focusSource(n - 1)}
            className="mx-0.5 rounded align-super text-xs font-medium text-indigo-300 transition hover:text-indigo-200 hover:underline"
          >
            [{n}]
          </button>
        );
      }
      return <span key={idx}>{part}</span>;
    });
  }

  const stageIdx = STEPS.findIndex((s) => s.key === stage);
  const maxScore = Math.max(...sources.map((s) => s.score ?? 0), 1e-9);

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <header className="mb-10 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 text-sm font-bold text-white shadow-lg shadow-indigo-500/25">
              R
            </span>
            <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Recall</h1>
          </div>
          <p className="mt-2 text-sm text-slate-400">
            Hybrid search (BM25 + vector + RRF + rerank) · grounded RAG with citations
          </p>
        </div>
        <div className="hidden items-center gap-2 pt-1 text-[11px] text-slate-500 sm:flex">
          <span className="rounded-full border border-white/10 bg-white/[0.03] px-2.5 py-1">KO / EN</span>
          <span className="rounded-full border border-white/10 bg-white/[0.03] px-2.5 py-1">SSE streaming</span>
          <span className="rounded-full border border-white/10 bg-white/[0.03] px-2.5 py-1">LLM judge</span>
        </div>
      </header>

      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <input
          className="min-w-0 flex-1 rounded-xl border border-white/10 bg-white/[0.04] px-4 py-3 text-[15px] text-slate-100 placeholder-slate-500 outline-none transition focus:border-indigo-400/60 focus:bg-white/[0.06] focus:ring-2 focus:ring-indigo-500/20"
          placeholder="Ask a question or search the knowledge base…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
        />
        <div className="flex items-center gap-2">
          <div
            className="flex rounded-xl border border-white/10 bg-white/[0.03] p-1 text-[11px] font-medium uppercase tracking-wide"
            title="Retrieval mode used by Search"
          >
            {MODES.map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`rounded-lg px-2.5 py-1.5 transition ${
                  mode === m ? "bg-indigo-500/25 text-indigo-200" : "text-slate-500 hover:text-slate-300"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
          <button
            onClick={() => ask()}
            className="rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 px-5 py-3 text-sm font-medium text-white shadow-lg shadow-indigo-500/25 transition hover:brightness-110"
          >
            Ask
          </button>
          <button
            onClick={() => search()}
            className="rounded-xl border border-white/10 bg-white/[0.04] px-5 py-3 text-sm text-slate-200 transition hover:bg-white/[0.08]"
          >
            Search
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-slate-600">Try:</span>
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            onClick={() => ask(ex)}
            className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-slate-400 transition hover:border-indigo-400/40 hover:text-slate-200"
          >
            {ex}
          </button>
        ))}
      </div>

      {error && (
        <p className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {error}
        </p>
      )}

      {stage !== "idle" && (
        <div className="mt-6 flex items-center text-[11px] font-medium uppercase tracking-wider">
          {STEPS.map((s, i) => {
            const done = stage === "done" || (stageIdx >= 0 && i < stageIdx);
            const active = s.key === stage;
            return (
              <div key={s.key} className="flex items-center">
                {i > 0 && <span className={`mx-3 h-px w-8 ${done || active ? "bg-indigo-400/50" : "bg-white/10"}`} />}
                <span
                  className={`flex items-center gap-1.5 ${
                    active ? "text-indigo-300" : done ? "text-slate-300" : "text-slate-600"
                  }`}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      active ? "animate-pulse bg-indigo-400" : done ? "bg-emerald-400" : "bg-slate-600"
                    }`}
                  />
                  {s.label}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <section className="mt-6 grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">Answer</h2>
            {streaming && <span className="text-xs text-indigo-300">● streaming</span>}
            {cached && (
              <span className="rounded-full bg-indigo-500/15 px-2 py-0.5 text-[11px] text-indigo-300 ring-1 ring-indigo-400/30">
                semantic cache hit
              </span>
            )}
            {stage === "verifying" && (
              <span className="text-[11px] text-slate-500">LLM judge checking answer against sources…</span>
            )}
            {grounded && (
              <span
                data-testid="groundedness-badge"
                className={`rounded-full px-2 py-0.5 text-[11px] ${GROUNDEDNESS_BADGE[grounded.verdict].className}`}
              >
                {GROUNDEDNESS_BADGE[grounded.verdict].label}
              </span>
            )}
            {(ttft !== null || elapsed !== null) && (
              <span className="ml-auto font-mono text-[11px] text-slate-500">
                {ttft !== null && <>TTFT {fmtMs(ttft)}</>}
                {ttft !== null && elapsed !== null && " · "}
                {elapsed !== null && <>total {fmtMs(elapsed)}</>}
              </span>
            )}
          </div>
          <div className="whitespace-pre-wrap text-[15px] leading-relaxed text-slate-200">
            {answer ? (
              <>
                {renderAnswer(answer)}
                {streaming && (
                  <span className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-indigo-300 align-middle" />
                )}
              </>
            ) : (
              <p className="text-sm leading-relaxed text-slate-500">
                Ask a question — the answer streams token by token with [n] citations, then a
                post-hoc LLM judge grades it against the retrieved sources (grounded / partial /
                unsupported).
              </p>
            )}
          </div>
        </div>

        <div className="rounded-2xl border border-white/[0.08] bg-white/[0.03] p-6">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">Sources</h2>
            {searchMeta && (
              <span className="font-mono text-[11px] text-slate-500">
                {searchMeta.count} hits · {searchMeta.mode} · {searchMeta.ms} ms
              </span>
            )}
          </div>
          {searching && (
            <div className="space-y-3">
              {[0, 1, 2].map((i) => (
                <div key={i} className="animate-pulse rounded-xl border border-white/[0.06] bg-white/[0.02] p-3.5">
                  <div className="h-2.5 w-1/3 rounded bg-white/[0.08]" />
                  <div className="mt-2.5 h-2 w-full rounded bg-white/[0.05]" />
                  <div className="mt-1.5 h-2 w-2/3 rounded bg-white/[0.05]" />
                </div>
              ))}
            </div>
          )}
          <ol className="space-y-3">
            {!searching &&
              sources.map((c, i) => (
                <li
                  key={c.id ?? i}
                  id={`src-${i}`}
                  onClick={() => setHighlight(i)}
                  className={`cursor-pointer rounded-xl border p-3.5 transition ${
                    highlight === i
                      ? "border-indigo-400/60 bg-indigo-500/10"
                      : "border-white/[0.06] bg-white/[0.02] hover:border-white/[0.14]"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
                    <span className="truncate">
                      <span className="font-mono text-indigo-300">[{i + 1}]</span>{" "}
                      <span className="text-slate-400">{c.docId}</span> · {c.source}
                    </span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      {c.lang && (
                        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 uppercase tracking-wide">{c.lang}</span>
                      )}
                      <span className="font-mono">{c.score?.toFixed(3)}</span>
                    </span>
                  </div>
                  <p className="mt-1.5 line-clamp-3 text-sm leading-relaxed text-slate-300">{c.content}</p>
                  <div className="mt-2.5 h-0.5 overflow-hidden rounded-full bg-white/[0.06]">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-emerald-400"
                      style={{ width: `${Math.max(6, ((c.score ?? 0) / maxScore) * 100)}%` }}
                    />
                  </div>
                </li>
              ))}
            {!searching && sources.length === 0 && (
              <li className="text-sm text-slate-600">No sources yet — run a search or ask a question.</li>
            )}
          </ol>
        </div>
      </section>

      <footer className="mt-12 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-white/[0.06] pt-5 text-xs text-slate-600">
        <a className="transition hover:text-slate-300" href={GITHUB} target="_blank" rel="noreferrer">
          GitHub
        </a>
        <span>·</span>
        <a className="transition hover:text-slate-300" href="http://localhost:3001" target="_blank" rel="noreferrer">
          Grafana
        </a>
        <span>·</span>
        <span>API {API}</span>
        <span className="ml-auto hidden font-mono text-[11px] sm:inline">
          BM25(Nori) + kNN(bge-m3) → RRF → rerank → LLM + judge
        </span>
      </footer>
    </main>
  );
}
