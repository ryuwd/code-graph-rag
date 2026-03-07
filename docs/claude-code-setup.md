# Claude Code Setup for Code-Graph-RAG MCP Server

Connect Code-Graph-RAG to Claude Code for powerful codebase analysis and editing.

## Quick Setup (No API Key Required)

If your MCP client already has LLM capabilities (e.g. Claude Code with a Max subscription), you don't need a separate API key. The client generates Cypher queries itself using `run_cypher` and `get_graph_schema`.

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH="$(pwd)" \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

**Replace** `/absolute/path/to/code-graph-rag` with where you cloned this repo.

## Setup with LLM Provider

If you want the server to handle natural language → Cypher translation internally via `query_code_graph`, configure an LLM provider:

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH="$(pwd)" \
  --env CYPHER_PROVIDER=google \
  --env CYPHER_MODEL=gemini-2.0-flash \
  --env CYPHER_API_KEY=your-google-api-key \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

## Explicit Repository Path

Specify the repository path explicitly instead of using `$(pwd)`:

```bash
claude mcp add --transport stdio code-graph-rag \
  --env TARGET_REPO_PATH=/absolute/path/to/your/project \
  -- uv run --directory /absolute/path/to/code-graph-rag code-graph-rag mcp-server
```

## Prerequisites

```bash
# 1. Install code-graph-rag
git clone https://github.com/vitali87/code-graph-rag.git
cd code-graph-rag
uv sync

# 2. Start Memgraph
docker run -p 7687:7687 -p 7444:7444 memgraph/memgraph-platform
```

## Usage

```
> Index this repository
> What functions call UserService.create_user?
> Show me how authentication works
> Update the login function to add rate limiting
```

**Important**: Only one repository can be indexed at a time. When you index a new repository, the previous repository's data is automatically cleared from the database. If you need to switch between multiple projects, you'll need to re-index when switching.

## Available Tools

### Graph querying (no API key needed)
- **get_graph_schema** - Introspect node types, properties, and relationships
- **run_cypher** - Execute read-only Cypher queries directly against Memgraph

### Graph querying (requires LLM provider)
- **query_code_graph** - Natural language queries (server translates to Cypher internally)

### Code operations
- **index_repository** - Build knowledge graph (clears previous repository data)
- **get_code_snippet** - Retrieve code by name
- **surgical_replace_code** - Precise code edits
- **read_file / write_file** - File operations
- **list_directory** - Browse directories
- **list_projects / delete_project / wipe_database** - Manage indexed projects

### How `run_cypher` works

When no LLM provider is configured, the MCP client (e.g. Claude Code) acts as
the intelligence layer. A typical workflow:

1. Call `get_graph_schema` to discover node labels, properties, and relationship types
2. Generate a Cypher query based on the schema and the user's question
3. Call `run_cypher` with the query to get results

This avoids the need for a separate API key — the LLM capabilities of the MCP
client are sufficient.

## LLM Provider Options

Only needed if you want to use `query_code_graph` for server-side natural language → Cypher translation.

**OpenAI**:
```bash
--env CYPHER_PROVIDER=openai \
--env CYPHER_MODEL=gpt-4 \
--env CYPHER_API_KEY=sk-...
```

**Google Gemini**:
```bash
--env CYPHER_PROVIDER=google \
--env CYPHER_MODEL=gemini-2.5-flash \
--env CYPHER_API_KEY=...
```

**Ollama** (free, local):
```bash
--env CYPHER_PROVIDER=ollama \
--env CYPHER_MODEL=llama3.2
```

## Multi-Repository Setup

Add separate named instances for different projects:

```bash
claude mcp add --transport stdio code-graph-rag-backend \
  --env TARGET_REPO_PATH=/path/to/backend \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server

claude mcp add --transport stdio code-graph-rag-frontend \
  --env TARGET_REPO_PATH=/path/to/frontend \
  --env CYPHER_PROVIDER=openai \
  --env CYPHER_MODEL=gpt-4 \
  --env CYPHER_API_KEY=your-api-key \
  -- uv run --directory /path/to/code-graph-rag code-graph-rag mcp-server
```

## Troubleshooting

**Can't find uv/code-graph-rag**: Use absolute paths from `which uv`

**Wrong repository analyzed**:
- Without `TARGET_REPO_PATH`: MCP uses the directory where Claude Code is opened
- With `TARGET_REPO_PATH`: MCP always uses that specific path (must be absolute)

**Memgraph connection failed**: Ensure `docker ps` shows Memgraph running

**Tools not showing**: Run `claude mcp list` to verify installation

## Remove

```bash
claude mcp remove code-graph-rag
```
