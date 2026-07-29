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
    import torch
    from FlagEmbedding import BGEM3FlagModel, FlagReranker

    use_fp16 = torch.cuda.is_available()  # fp16 helps on GPU; keep fp32 on CPU
    _models["embed"] = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=use_fp16)
    _models["rerank"] = FlagReranker(RERANKER_MODEL, use_fp16=use_fp16)
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


class M3ScoreRequest(BaseModel):
    query: str
    passages: list[str]
    # Fusion weights [dense, sparse, colbert] — see docs/adr/0008 (BGE-M3 paper, §self-hybrid).
    weights: list[float] = [0.4, 0.2, 0.4]


class M3ScoreItem(BaseModel):
    index: int
    score: float
    dense: float
    sparse: float
    colbert: float


class M3ScoreResponse(BaseModel):
    results: list[M3ScoreItem]


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


@app.post("/score_m3", response_model=M3ScoreResponse)
def score_m3(req: M3ScoreRequest) -> M3ScoreResponse:
    """Tri-modal scoring with bge-m3 itself: dense + sparse lexical + ColBERT MaxSim,
    combined as a weighted sum per the BGE-M3 paper's self-hybrid retrieval. An alternative
    rerank stage to the cross-encoder — same model that produced the index embeddings."""
    if not req.passages:
        return M3ScoreResponse(results=[])
    if len(req.weights) != 3:
        raise ValueError("weights must be [dense, sparse, colbert]")
    pairs = [[req.query, p] for p in req.passages]
    scores = _models["embed"].compute_score(pairs, batch_size=8, max_passage_length=1024)
    w_dense, w_sparse, w_colbert = req.weights
    items = [
        M3ScoreItem(
            index=i,
            score=w_dense * d + w_sparse * s + w_colbert * c,
            dense=float(d), sparse=float(s), colbert=float(c),
        )
        for i, (d, s, c) in enumerate(
            zip(scores["dense"], scores["sparse"], scores["colbert"]))
    ]
    items.sort(key=lambda it: it.score, reverse=True)
    return M3ScoreResponse(results=items)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
