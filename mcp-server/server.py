"""Recall MCP server — your self-hosted corpus as agent tools.

Exposes the Recall API (hybrid search + grounded RAG, no API key needed) as MCP tools so
Claude Desktop / Claude Code / any MCP client can search and question YOUR documents:

    recall_search(query, mode="hybrid", top_k=8)   -> ranked passages with scores
    recall_ask(question)                           -> grounded answer + [n] citations
                                                      + groundedness verdict

Run (stdio):
    RECALL_API=http://localhost:18080 python server.py

Requires the Recall stack up (docker compose --profile full up) and `pip install fastmcp`.
"""
import json
import os
import urllib.parse
import urllib.request

from fastmcp import FastMCP

API = os.getenv("RECALL_API", "http://localhost:8080")

mcp = FastMCP("recall")


@mcp.tool()
def recall_search(query: str, mode: str = "hybrid", top_k: int = 8) -> str:
    """Search the Recall knowledge base. mode: hybrid (BM25+vector+rerank, default),
    bm25, vector, or hyde. Returns ranked passages with doc ids and scores."""
    url = (f"{API}/api/search?q=" + urllib.parse.quote(query)
           + f"&mode={urllib.parse.quote(mode)}")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = json.load(r)
    results = data.get("results", [])[: max(1, min(top_k, 20))]
    if not results:
        return "No results."
    lines = []
    for i, chunk in enumerate(results, 1):
        lines.append(f"[{i}] {chunk['docId']} (score {chunk.get('score', 0):.3f}, "
                     f"{chunk.get('lang', '?')})\n{chunk['content']}")
    return "\n\n".join(lines)


@mcp.tool()
def recall_ask(question: str) -> str:
    """Ask the Recall knowledge base a question. Streams the grounded RAG pipeline
    (retrieve -> sufficiency gate -> generate -> groundedness judge) and returns the
    final answer with [n] citations, the cited sources, and the judge's verdict."""
    url = f"{API}/api/ask?q=" + urllib.parse.quote(question)
    answer_parts: list[str] = []
    sources: list[dict] = []
    verdict: str | None = None
    insufficient = False

    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=600) as stream:
        event = ""
        for raw in stream:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].lstrip()
                if event == "token":
                    answer_parts.append(json.loads(data))       # JSON-encoded token
                elif event == "sources":
                    sources = json.loads(data)
                elif event == "groundedness":
                    verdict = json.loads(data).get("verdict")
                elif event == "sufficiency":
                    insufficient = True
                elif event == "done":
                    break
                elif event == "error":
                    return f"Recall error: {data}"

    answer = "".join(answer_parts).strip() or "(no answer)"
    out = [answer, ""]
    if insufficient:
        out.append("note: retrieval judged the context INSUFFICIENT — answered by abstaining.")
    if verdict:
        out.append(f"groundedness: {verdict}")
    if sources:
        out.append("sources:")
        for i, chunk in enumerate(sources, 1):
            preview = chunk["content"][:160].replace("\n", " ")
            out.append(f"  [{i}] {chunk['docId']} — {preview}…")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
