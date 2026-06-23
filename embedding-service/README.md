# Embedding sidecar

FastAPI service wrapping **bge-m3** (multilingual KO/EN dense embeddings, 1024-dim)
and **bge-reranker-v2-m3** (cross-encoder reranking). Kept as a separate service so
the JVM backend stays clean and the model runtime can scale (CPU/GPU) independently.

## Run

```bash
# via docker compose (recommended — caches model weights in a volume)
docker compose up -d embedding-service

# or locally
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

API docs at http://localhost:8000/docs

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/embed` | `{ "texts": ["..."] }` | `{ "embeddings": [[...]] }` |
| POST | `/rerank` | `{ "query": "...", "passages": ["..."], "topK": 8 }` | `{ "results": [{ "index": 0, "score": 0.97 }] }` |
| GET | `/health` | — | `{ "status": "ok" }` |

> First request downloads model weights from Hugging Face (cached afterwards).
