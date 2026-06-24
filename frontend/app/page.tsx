"use client";

import { useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

type Chunk = {
  id: string;
  docId: string;
  chunkIndex: number;
  content: string;
  source: string;
  lang: string;
  score: number;
};

export default function Home() {
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<Chunk[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [cached, setCached] = useState(false);
  const [highlight, setHighlight] = useState<number | null>(null);
  const esRef = useRef<EventSource | null>(null);

  function reset() {
    esRef.current?.close();
    setAnswer("");
    setSources([]);
    setCached(false);
    setHighlight(null);
  }

  async function search() {
    reset();
    const res = await fetch(`${API}/api/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    setSources(data.results ?? []);
  }

  function ask() {
    if (!query.trim()) return;
    reset();
    setStreaming(true);
    const es = new EventSource(`${API}/api/ask?q=${encodeURIComponent(query)}`);
    esRef.current = es;

    es.addEventListener("sources", (e) => setSources(JSON.parse((e as MessageEvent).data)));
    es.addEventListener("cache", () => setCached(true));
    es.addEventListener("token", (e) => setAnswer((a) => a + JSON.parse((e as MessageEvent).data)));
    es.addEventListener("done", () => {
      setStreaming(false);
      es.close();
    });
    es.addEventListener("error", () => {
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
            className="mx-0.5 align-super text-xs text-accent hover:underline"
          >
            [{n}]
          </button>
        );
      }
      return <span key={idx}>{part}</span>;
    });
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-10">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold">Recall</h1>
        <p className="text-sm text-gray-500">Hybrid search + grounded RAG QA over a knowledge base.</p>
      </header>

      <div className="flex gap-2">
        <input
          className="flex-1 rounded-lg border border-gray-300 px-4 py-2 outline-none focus:border-accent"
          placeholder="Ask a question or search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
        />
        <button onClick={ask} className="rounded-lg bg-accent px-4 py-2 text-white">
          Ask
        </button>
        <button onClick={search} className="rounded-lg border border-gray-300 px-4 py-2">
          Search
        </button>
      </div>

      <section className="mt-8 grid grid-cols-1 gap-6 md:grid-cols-2">
        <div className="rounded-xl border border-gray-200 bg-white p-5">
          <h2 className="mb-2 text-sm font-medium text-gray-500">
            Answer {streaming && <span className="text-accent">● streaming</span>}
            {cached && <span className="ml-2 rounded bg-accent-soft px-2 text-xs text-accent">cache hit</span>}
          </h2>
          <p className="whitespace-pre-wrap text-[15px] leading-relaxed">
            {answer ? renderAnswer(answer) : <span className="text-gray-400">Ask a question to get a grounded, cited answer.</span>}
          </p>
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-5">
          <h2 className="mb-2 text-sm font-medium text-gray-500">Sources</h2>
          <ol className="space-y-3">
            {sources.map((c, i) => (
              <li
                key={c.id ?? i}
                id={`src-${i}`}
                className={`rounded border-l-2 pl-3 transition ${
                  highlight === i ? "border-accent bg-accent-soft" : "border-accent-soft"
                }`}
              >
                <div className="flex justify-between text-xs text-gray-500">
                  <span>
                    [{i + 1}] {c.source}
                  </span>
                  <span>{c.score?.toFixed(3)}</span>
                </div>
                <p className="mt-1 line-clamp-4 text-sm text-gray-700">{c.content}</p>
              </li>
            ))}
            {sources.length === 0 && <li className="text-sm text-gray-400">No sources yet.</li>}
          </ol>
        </div>
      </section>

      <footer className="mt-10 text-xs text-gray-400">
        Backend: {API} · admin dashboards in Grafana (localhost:3001)
      </footer>
    </main>
  );
}
