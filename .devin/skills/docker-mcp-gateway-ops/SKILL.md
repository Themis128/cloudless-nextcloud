---
name: docker-mcp-gateway-ops
description: Operate the Docker MCP gateway (8013) that serves Devin Desktop — health checks, token refresh, restarts, full recovery, and adding remote servers. Use when MCP tools are missing, failing, or the gateway is down.
---

# Docker MCP Gateway Ops (Devin-side)

Canonical ops skill lives at `C:\Users\tbaltzakis\.agents\skills\docker-mcp-gateway-ops\SKILL.md` (registered with the local agent CLI). This Devin-side copy gives Devin agents the operational reflexes for the gateway stack on this machine.

## The stack (one paragraph)

Docker Desktop's MCP Toolkit runs a managed gateway on 8012 (127.0.0.1, respawns after kill — ignore it). The real gateway is the **8013 profile gateway**: `docker mcp gateway run --port 8013 --profile github_minimal --transport streamable-http --host 0.0.0.0 --allow-unauthenticated`, reachable from Devin Desktop via Windows Tailscale IP `http://100.83.120.93:8013/mcp` (streamable-http, no auth). Profile + catalog live in `~/.docker/mcp/mcp-toolkit.db`; a watchdog (`C:\Users\tbaltzakis\.docker\mcp-gateway-watchdog.ps1`, started by the Startup folder launcher) relaunches it if it dies and runs the token-permanence engine (`C:\Users\tbaltzakis\mcp-token-permanence.py`) every ~4h. **Devin Desktop's permanent wiring**: `%APPDATA%\Devin\mcp_config.json` `docker-mcp` → `http://127.0.0.1:8013/mcp` (updated 2026-11-20; 400 tools load lazily per session).

## Tools Devin can run directly (PowerShell/cmd — from WSL, use the ~/.local/bin wrappers)

- **Health check**: `python -I %USERPROFILE%\mcp-health-check.py` (WSL: bare `mcp-health-check`) — engine, watchdog, handshake, live canary call (`github-official__get_me`), per-server status, linear token expiry. Exit 0 = healthy, 1 = degraded.
- **One-click restart**: `%USERPROFILE%\mcp-gateway-restart.cmd` — kills, relaunches via docker CLI, waits 100s, prints boot result.
- **Token permanence**: `python -I %USERPROFILE%\mcp-token-permanence.py` (WSL: bare `mcp-token-permanence`) — refreshes linear (PKCE via Devin's WSL store, rotated state persists), reseeds greptile, harvests wrangler's fresh CF token when its grant is valid.
- **Browser OAuth alternative**: `%USERPROFILE%\mcp-oauth-refresh.cmd` — opens fresh authorization tabs for the 4 Cloudflare product servers + linear; complete each in the browser, then run token-permanence + restart.
- **Full recovery**: `%USERPROFILE%\mcp-full-recovery.cmd` (WSL: bare `mcp-full-recovery`) — worst-case orchestrator: engine → gateway → tokens → verify. Safe to run any time.

## What Devin should do when...

| Situation | Action |
|-----------|--------|
| MCP tools missing / gateway unreachable | Run health check; if gateway down → restart tool; report verdict |
| Worst case (engine down, everything broken) | Run full recovery; report the [5/5] health verdict |
| Linear tools vanish (70 → 0) | Token expired → run token-permanence, then restart gateway via the .cmd |
| A server shows "Can't start ... Forbidden/Unauthorized" | CF product servers: either `wrangler login` (one browser action; engine auto-seeds after) or the OAuth refresh tool; report to user |
| Gateway crash-loops (EOF on calls) | Clean kill + relaunch (restart .cmd); do not retry calls against a degraded instance |
| User asks to add a remote MCP server | Follow "Adding a remote server" in the canonical skill; model snapshots on apify (id=10 in catalog_server); seed secrets, restart, verify with a live call |

## Hard rules

- Never fight the 8012 managed gateway — use 8013.
- Token values exist only in memory/pipes; never print, log, or persist them anywhere new.
- After reseeding secrets, RESTART the gateway — secrets load at boot only.
- `tools/list` counts are cached metadata; trust only live `tools/call` results.
- All gateway HTTP endpoints are fixed constants (`100.83.120.93:8013`, `127.0.0.1:8013`); never construct URLs from user input.
- Launch the gateway ONLY via `docker mcp gateway run ...` (docker CLI dispatch) — direct docker-mcp.exe invocation fails ("unknown flag").
