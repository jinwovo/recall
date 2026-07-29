# Recall frontend

Next.js (App Router) + TypeScript + Tailwind. Search box → hybrid search results;
Ask → streamed (SSE) grounded answer with a live sources panel; `/admin` → ops page for
the ingestion DLQ (depth, decoded forensic headers, one-click replay — ADR 0006).

```bash
cp .env.local.example .env.local   # NEXT_PUBLIC_API_BASE=http://localhost:8080
npm install
npm run dev                         # http://localhost:3000
```

## Next steps
- Add [shadcn/ui](https://ui.shadcn.com) for polished components (`npx shadcn@latest init`).
- Embed Grafana panels on `/admin` (latency / $/query / groundedness) next to the DLQ view.
