"""Recall embedding/rerank sidecar.

Exposes:
  POST /embed   -> dense embeddings (bge-m3, multilingual KO/EN, 1024-dim)
  POST /rerank  -> cross-encoder reranking (bge-reranker-v2-m3)
  GET  /health

Model weights download on first run and are cached (mount ~/.cache/huggingface).
Matches the Java EmbeddingClient DTOs (note the camelCase `topK`).
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

_models: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    from FlagEmbedding import BGEM3FlagModel, FlagReranker

    _models["embed"] = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)
    _models["rerank"] = FlagReranker(RERANKER_MODEL, use_fp16=True)
    yield
    _models.clear()


app = FastAPI(title="Recall Embedding Sidecar", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]


class RerankRequest(BaseModel):
    query: str
    passages: list[str]
    topK: int = 8


class RerankItem(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    results: list[RerankItem]


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest) -> EmbedResponse:
    vecs = _models["embed"].encode(req.texts, batch_size=16, max_length=1024)["dense_vecs"]
    return EmbedResponse(embeddings=[v.tolist() for v in vecs])


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest) -> RerankResponse:
    if not req.passages:
        return RerankResponse(results=[])
    pairs = [[req.query, p] for p in req.passages]
    scores = _models["rerank"].compute_score(pairs, normalize=True)
    if isinstance(scores, float):
        scores = [scores]
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[: req.topK]
    return RerankResponse(results=[RerankItem(index=i, score=float(s)) for i, s in ranked])


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
