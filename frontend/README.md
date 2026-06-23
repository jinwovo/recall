# Recall frontend

Next.js (App Router) + TypeScript + Tailwind. Search box → hybrid search results;
Ask → streamed (SSE) grounded answer with a live sources panel.

```bash
cp .env.local.example .env.local   # NEXT_PUBLIC_API_BASE=http://localhost:8080
npm install
npm run dev                         # http://localhost:3000
```

## Next steps
- Add [shadcn/ui](https://ui.shadcn.com) for polished components (`npx shadcn@latest init`).
- Inline citation highlighting: click `[n]` in the answer → scroll/highlight source `n`.
- Admin dashboard page embedding Grafana panels (latency / $/query / Recall@10).
