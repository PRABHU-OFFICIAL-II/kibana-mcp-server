# Kibana MCP Server — Project Brief

## What this is

An MCP (Model Context Protocol) server for Kibana/Elasticsearch, modelled exactly on the
sibling `../grafana-mcp-server` project. It exposes Kibana operations as tools that
Claude can call from Claude Code or any MCP-compatible client.

## Architecture

```
kibana-mcp-server/
├── server.py                        ← Entry point. Supports stdio (Claude Code) and HTTP/SSE modes.
├── kibana_mcp/
│   ├── config.py                    ← All config via env vars (.env file)
│   ├── auth/
│   │   ├── session.py               ← Session dataclass, load/save to .kibana-session.json
│   │   ├── manager.py               ← get_session(), inject_session(), login stub
│   │   └── okta.py                  ← TODO: Okta SSO login (implement after HAR analysis)
│   ├── kibana/
│   │   ├── client.py                ← kibana_get(), kibana_post(), es_post() — all HTTP
│   │   └── api.py                   ← All Kibana/ES API calls (one function per endpoint)
│   └── tools/
│       └── index.py                 ← register_tools() — all MCP tool definitions + dispatch
├── tests/
├── .env.example                     ← Template for .env file
├── requirements.txt
└── CLAUDE.md                        ← This file
```

## Current state (what is built vs what is pending)

### Built (skeleton — compiles and runs)
- `server.py` — stdio + HTTP/SSE server, identical pattern to Grafana server
- `config.py` — env-driven config for Kibana URL, space, auth method
- `auth/session.py` — Session model with cookie_name + cookie_value (flexible, handles any cookie name)
- `auth/manager.py` — inject_session(), get_session(), SessionExpiredError
- `kibana/client.py` — HTTP client with both cookie and API key auth modes
- `kibana/api.py` — API function stubs for all planned tools
- `tools/index.py` — Full MCP tool registry with 12 tools

### Tools registered (ready to test once auth works)
| Tool | What it does |
|------|-------------|
| `inject_session` | Inject cookie from browser/HAR |
| `auth_status` | Check session expiry |
| `list_spaces` | List Kibana spaces |
| `list_dashboards` | List dashboards in a space |
| `get_dashboard_info` | Dashboard panel layout |
| `list_data_views` | List index patterns / data views |
| `get_data_view` | Fields and config for a data view |
| `search_logs` | KQL search against any index pattern |
| `run_esql` | ES\|QL query (ES 8.11+) |
| `get_alert_rules` | List Kibana alerting rules |
| `get_active_alerts` | Active instances for a rule |
| `get_apm_services` | APM service list |
| `list_ml_jobs` | ML anomaly detection jobs |

### TODO (blocked on HAR analysis)
- [ ] `auth/okta.py` — Okta SSO login flow (mirror of Grafana's `okta.py`)
- [ ] Verify exact API paths for each tool against real Kibana instance
- [ ] Verify cookie name (`sid`? `security_authentication`? something else?)
- [ ] Add scenario/investigation tools (like Grafana's `investigate_*`) once basic tools work
- [ ] Add `kbn-version` header requirement if Kibana enforces it
- [ ] Wire up MCP server config in Claude Code (`claude mcp add`)

## How to implement Okta login (once HAR is shared)

Look at `../grafana-mcp-server/grafana_mcp/auth/okta.py` as the reference.
Create `kibana_mcp/auth/okta.py` with `login_with_okta()` and `try_silent_refresh()`.
The HAR will show:
1. Which Okta authorize URL is hit
2. What redirect URI Kibana expects
3. What SAML/OIDC callback sets the session cookie

## Setup instructions

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in env
cp .env.example .env
# Edit .env: set KIBANA_URL, auth method, etc.

# 3. Run in stdio mode (for Claude Code)
python server.py

# 4. Run in HTTP/SSE mode (for remote/web clients)
MCP_MODE=http python server.py
```

## What we need from the user before completing implementation

### REQUIRED
1. **HAR file** — capture from browser: login flow → open a dashboard → run a KQL search.
   - Tells us: auth cookie name, exact API paths, request/response shapes.
   - Before sharing: redact actual cookie values, remove password fields.

2. **Kibana URL** — set in `.env` as `KIBANA_URL`

3. **Auth method** — one of:
   - Cookie injection (paste from DevTools — quickest path to working)
   - API key (Kibana Stack Management → API Keys → Create)
   - Okta SSO (needs HAR to implement login flow)

### HELPFUL (but can discover from HAR / API)
4. **Elasticsearch version** — `8.x` (ES|QL available) or `7.x` (ES|QL unavailable)
5. **Space ID** — if dashboards/data views live in a non-default space
6. **Index patterns in use** — e.g. `logs-*`, `filebeat-*`, `apm-*` — for search_logs default examples
7. **APM enabled?** — determines whether APM tools are worth building out
8. **ML/anomaly detection enabled?** — determines whether ML tools are worth building out

## Adding this server to Claude Code

Once `.env` is filled in and `python server.py` starts without errors:

```bash
# Add to Claude Code (stdio mode)
claude mcp add kibana-mcp -- python /path/to/kibana-mcp-server/server.py

# Or with full path on Windows
claude mcp add kibana-mcp -- python "C:\Users\ppenthoi\Documents\DEV\mcp-servers\kibana-mcp-server\server.py"
```

## Key differences from Grafana MCP server

| Grafana | Kibana |
|---------|--------|
| Prometheus metrics (PromQL) | Elasticsearch logs/metrics (KQL / ES\|QL) |
| `grafana_session` cookie | `sid` or `security_authentication` cookie (TBC from HAR) |
| Loki for logs | Elasticsearch / data streams for logs |
| Grafana alerting | Kibana alerting rules |
| Dashboard panels with PromQL targets | Dashboard panels with ES queries |
| Scenario tools (investigate_*) | TODO: add after basic tools verified |

## Conventions (mirror the Grafana server)

- All env config in `config.py` via `os.environ.get()`
- All HTTP in `kibana/client.py` — no `httpx` calls outside this file
- All API functions in `kibana/api.py` — one `async def` per endpoint
- All tool logic in `tools/index.py` — `register_tools()` registers everything
- `_text(str)` helper returns `[TextContent(...)]`
- Errors caught at the top of `call_tool()` dispatcher — never crash the MCP server
