# MCP Troubleshooting Log

This document tracks everything we've tried to get the OutcomeOps MCP server working with Claude Code, so we stop repeating the same steps.

## The Problem

The OutcomeOps MCP server at `https://outcomeops.outcomeops.ai/mcp/v1` works perfectly via curl but Claude Code does not load the tools (`query_knowledge_base`, `get_coding_standards`, `list_workspaces`, `list_workspace_documents`, plus proxied SonarQube tools). The `/mcp` command in Claude Code only shows cloud-based connectors (Gmail, Gdrive, Atlassian) but not the outcomeops server.

## What We Know Works

- **curl to the server works every time** - initialize, tools/list, and actual tool calls all succeed
- The server returns valid MCP JSON-RPC responses with `protocolVersion: "2025-03-26"`
- The server advertises all tools correctly via `tools/list`
- Claude Code version: 2.1.72

## Config Locations Tried

### 1. `~/.claude/mcp.json` (user-level, PREFERRED)

This is where we want the config to live. Current contents:

```json
{
  "mcpServers": {
    "outcomeops": {
      "type": "streamable-http",
      "url": "https://outcomeops.outcomeops.ai/mcp/v1",
      "headers": {
        "Authorization": "Bearer <key>"
      }
    }
  }
}
```

**Result:** Claude Code does NOT load the tools. `/mcp` does not show outcomeops.

### 2. `.claude.json` project config (via `claude mcp add` CLI)

Added via:
```bash
claude mcp add --transport http outcomeops https://outcomeops.outcomeops.ai/mcp/v1 \
  --header "Authorization: Bearer <key>"
```

This writes to `~/.claude.json` under `projects.<project-path>.mcpServers` with `"type": "http"`.

Also tried via `add-json`:
```bash
claude mcp add-json outcomeops '{"type":"http","url":"...","headers":{...}}'
```

**Result:** Config is written but `claude mcp list` returns empty output. Tools not loaded.

**Note:** `claude mcp add-json` rejects `"type": "streamable-http"` with "Invalid input". The CLI only accepts `"type": "http"`.

## Transport Type Confusion

This has caused us to go back and forth multiple times:

| Config location | `"http"` | `"streamable-http"` |
|---|---|---|
| `~/.claude/mcp.json` | Untested (we kept using streamable-http here) | Does not load tools |
| `.claude.json` project config | Does not load tools | Rejected by CLI validator |
| `claude mcp add --transport` | `--transport http` (only option) | Not accepted |
| VS Code | N/A | Uses `"streamableHttp"` (camelCase) |

**Key insight:** The CLI and the mcp.json file may use different type identifiers. The CLI uses `"http"`, the mcp.json format may expect `"streamable-http"`. Neither has resulted in tools loading.

## Things That Did NOT Help

1. Switching between `"http"` and `"streamable-http"` in `~/.claude/mcp.json` - neither works
2. Adding via `claude mcp add` CLI to project config - config is written but tools don't load
3. Having the config in BOTH locations simultaneously - no effect, may cause conflicts
4. Restarting Claude Code after config changes - tools still don't appear
5. `claude mcp list` - always returns empty output regardless of config state
6. `claude mcp get outcomeops` - returns empty output

## Server Verification Commands

Use these to confirm the server is healthy (it always is):

```bash
# Test initialize
curl -s -X POST https://outcomeops.outcomeops.ai/mcp/v1 \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# Test tools/list
curl -s -X POST https://outcomeops.outcomeops.ai/mcp/v1 \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

## Things Tried That Produced No Useful Output

- [x] `CLAUDE_DEBUG=1 claude` - No MCP-related debug output. `/mcp` still only shows cloud connectors.
- [x] `claude mcp list` - Always returns empty output regardless of config state
- [x] `claude mcp get outcomeops` - Returns empty output

## Things Still To Try

- [ ] Try `MCP_TIMEOUT=15000 claude` to rule out connection timeout at startup
- [ ] **Test with a known-working public HTTP MCP server** (e.g., `claude mcp add --transport http sentry https://mcp.sentry.dev/mcp`) - if Sentry loads but outcomeops doesn't, the issue is server-specific; if neither loads, Claude Code's HTTP MCP client is broken
- [ ] Try a `.mcp.json` file in the project root (project-scoped config, team-shareable, different from `.claude.json`)
- [ ] Check if the server needs to support SSE streaming or session management that curl doesn't test
- [ ] Check Claude Code GitHub issues for HTTP MCP server bugs
- [ ] Upgrade Claude Code to latest version and retry
- [ ] File a bug report at https://github.com/anthropics/claude-code/issues

## MCP Config Location Reference

| Location | Scope | Managed by | VCS? |
|---|---|---|---|
| `~/.claude/mcp.json` | User-level (all projects) | Manual edit | No |
| `.mcp.json` (project root) | Project-level (team-shared) | Manual edit | Yes |
| `~/.claude.json` projects.*.mcpServers | Local project config | `claude mcp add` CLI | No |

## ROOT CAUSE FOUND (2026-03-11)

**`~/.claude/mcp.json` is NOT read by Claude Code v2.1.72 for HTTP MCP servers.**

We proved this by adding a known-working public server (Sentry) via the CLI:
```bash
claude mcp add --transport http sentry https://mcp.sentry.dev/mcp
```
This wrote to `.claude.json` project config and **Sentry appeared in `/mcp` immediately**.
Meanwhile, outcomeops in `~/.claude/mcp.json` continued to be invisible.

### The Fix

Use `claude mcp add` CLI to add servers to the project config (`.claude.json`):
```bash
claude mcp add --transport http outcomeops https://outcomeops.outcomeops.ai/mcp/v1 \
  --header "Authorization: Bearer <key>"
```

This stores the config in `~/.claude.json` under `projects.<project-path>.mcpServers`.

### Current State

- outcomeops added via `claude mcp add` to project config in `.claude.json`
- `~/.claude/mcp.json` cleared (not used)
- Transport type: `"http"` (what the CLI writes)
- **VERIFIED WORKING** - outcomeops shows "connected" in `/mcp` and tools load successfully

### Files Updated

- `ui/src/pages/Settings.tsx` - Removed manual mcp.json option, CLI uses `--transport http`
- `docs/mcp-server.md` - Same changes
- `docs/getting-started.md` - Same changes
