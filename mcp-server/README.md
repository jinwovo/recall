# Recall MCP server

Your self-hosted corpus as agent tools: exposes Recall's hybrid search and grounded RAG
over the [Model Context Protocol](https://modelcontextprotocol.io), so Claude Desktop,
Claude Code, or any MCP client can search and question **your** documents — locally, with
no API key.

| tool | does |
|---|---|
| `recall_search(query, mode, top_k)` | ranked passages (hybrid / bm25 / vector / hyde) |
| `recall_ask(question)` | grounded answer with `[n]` citations + groundedness verdict |

## Setup

```bash
# 1. Recall stack up (repo root):
docker compose --profile full up -d
python scripts/ingest_folder.py ~/my-docs     # or seed the demo corpus

# 2. Server deps:
cd mcp-server && pip install -r requirements.txt
```

Claude Desktop (`claude_desktop_config.json`) / Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "recall": {
      "command": "python",
      "args": ["/path/to/recall/mcp-server/server.py"],
      "env": { "RECALL_API": "http://localhost:8080" }
    }
  }
}
```

Then ask your client things like *"search recall for pod restart troubleshooting"* or
*"ask recall why my Kafka consumer is lagging"* — answers come back cited against your
own documents, with the groundedness judge's verdict attached.
