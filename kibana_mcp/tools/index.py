import time
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.types import TextContent, Tool

from kibana_mcp.auth.manager import get_session, inject_session, init_session, SessionExpiredError
from kibana_mcp.auth.session import load_session
from kibana_mcp.config import config
from kibana_mcp.kibana.api import (
    list_spaces, list_dashboards, get_dashboard, list_data_views, get_data_view,
    search_logs, run_esql, get_alert_rules, get_active_alerts,
    get_apm_services, list_ml_jobs,
)
from kibana_mcp.tools.scenarios import (
    investigate_service_errors,
    trace_request,
    search_by_org,
    investigate_pod_health,
    compare_log_volume,
    search_service_logs,
)


def _text(content: str) -> list:
    return [TextContent(type="text", text=content)]


def register_tools(server: Server) -> None:

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> list:
        try:
            return await _dispatch(name, arguments)
        except SessionExpiredError:
            return _text("Session expired. Use inject_session or login tool to re-authenticate.")
        except NotImplementedError as e:
            return _text(f"Not yet implemented: {e}")
        except RuntimeError as e:
            return _text(f"Kibana API error: {e}")
        except Exception as e:
            return _text(f"Error executing {name}: {type(e).__name__}: {e}")

    async def _dispatch(name: str, arguments: Dict[str, Any]) -> list:

        # ── Auth ──────────────────────────────────────────────────────────────

        if name == "login":
            username = arguments.get("username") or config.kibana.username
            password = arguments.get("password") or config.kibana.password
            if not username or not password:
                return _text("Username and password required. Pass as arguments or set KIBANA_USERNAME / KIBANA_PASSWORD env vars.")
            s = await init_session(username, password)
            from datetime import datetime, timezone
            exp = datetime.fromtimestamp(s.expires_at / 1000, tz=timezone.utc).isoformat()
            return _text(f"Logged in via Okta SAML. Cookie: {s.cookie_name}. Expires at {exp}")

        elif name == "inject_session":
            s = inject_session(
                cookie_name=arguments["cookie_name"],
                cookie_value=arguments["cookie_value"],
                expires_at_ms=arguments["expires_at_unix_seconds"] * 1000,
                extra_headers=arguments.get("extra_headers", {}),
            )
            from datetime import datetime, timezone
            exp = datetime.fromtimestamp(s.expires_at / 1000, tz=timezone.utc).isoformat()
            return _text(f"Session injected. Cookie: {s.cookie_name}. Expires at {exp}")

        elif name == "auth_status":
            session = load_session()
            if not session:
                return _text("No active session. Use inject_session or login tool first.")
            expires_in = round((session.expires_at - int(time.time() * 1000)) / 60000)
            if expires_in > 0:
                return _text(f"Session active. Cookie: {session.cookie_name}. Expires in {expires_in} minutes.")
            return _text(f"Session expired {abs(expires_in)} minutes ago. Use inject_session or login.")

        # ── Spaces ────────────────────────────────────────────────────────────

        elif name == "list_spaces":
            spaces = await list_spaces()
            lines = [f"  {s.get('id','').ljust(22)} {s.get('name','')}" for s in spaces]
            header = f"Found {len(spaces)} spaces:\n\nID                     Name\n{'─' * 60}"
            return _text(f"{header}\n" + "\n".join(lines))

        # ── Dashboards ────────────────────────────────────────────────────────

        elif name == "list_dashboards":
            dashboards = await list_dashboards(arguments.get("space_id"))
            lines = [
                f"  {d.get('id','').ljust(38)} {d.get('attributes',{}).get('title','')}"
                for d in dashboards
            ]
            header = f"Found {len(dashboards)} dashboards:\n\nID                                     Title\n{'─' * 70}"
            return _text(f"{header}\n" + "\n".join(lines))

        elif name == "get_dashboard_info":
            dashboard = await get_dashboard(arguments["dashboard_id"])
            attrs = dashboard.get("attributes", {})
            panels = attrs.get("panelsJSON", "[]")
            import json
            try:
                panels_list = json.loads(panels) if isinstance(panels, str) else panels
            except Exception:
                panels_list = []
            lines = [
                f"Dashboard: {attrs.get('title', '(unknown)')}",
                f"Description: {attrs.get('description', '')}",
                f"Panels: {len(panels_list)}",
                "",
            ]
            for p in panels_list:
                ptype = p.get("type", "")
                pid = p.get("panelIndex", p.get("id", ""))
                lines.append(f"  [{pid}] type={ptype}")
            return _text("\n".join(lines))

        # ── Data Views ────────────────────────────────────────────────────────

        elif name == "list_data_views":
            views = await list_data_views()
            lines = [
                f"  {v.get('id','').ljust(38)} {v.get('attributes',{}).get('title', v.get('title',''))}"
                for v in views
            ]
            header = f"Found {len(views)} data views:\n\nID                                     Title/Pattern\n{'─' * 70}"
            return _text(f"{header}\n" + "\n".join(lines))

        elif name == "get_data_view":
            item = await get_data_view(arguments["data_view_id"])
            attrs = item.get("attributes", item)
            lines = [
                f"Data View: {attrs.get('title', '(unknown)')}",
                f"ID: {item.get('id', '')}",
                f"Time field: {attrs.get('timeFieldName', '@timestamp')}",
            ]
            return _text("\n".join(lines))

        # ── Search / Logs ─────────────────────────────────────────────────────

        elif name == "search_logs":
            range_min = arguments.get("range_minutes", 60)
            to_ms = int(time.time() * 1000)
            from_ms = to_ms - range_min * 60 * 1000
            result = await search_logs(
                index_pattern=arguments["index_pattern"],
                kql=arguments.get("kql", "*"),
                from_ms=from_ms,
                to_ms=to_ms,
                size=arguments.get("size", 100),
                sort_order=arguments.get("sort_order", "desc"),
            )
            hits = result.get("hits", {})
            total = hits.get("total", {})
            total_count = total.get("value", 0) if isinstance(total, dict) else total
            docs = hits.get("hits", [])
            lines = [
                f"Search: {arguments.get('kql', '*')}",
                f"Index: {arguments['index_pattern']}",
                f"Range: last {range_min} minutes",
                f"Total hits: {total_count} (showing {len(docs)})",
                "",
            ]
            for doc in docs:
                src = doc.get("_source", {})
                # Prefer fields array if present (bsearch returns fields)
                fields = doc.get("fields", {})
                ts = (fields.get("@timestamp", [None])[0] if fields else None) or src.get("@timestamp", "")
                msg = src.get("message", str(src)[:120])
                lines.append(f"  [{ts}] {msg}")
            return _text("\n".join(lines))

        elif name == "run_esql":
            result = await run_esql(arguments["query"])
            columns = result.get("columns", [])
            rows = result.get("values", [])
            col_names = [c.get("name", "") for c in columns]
            lines = [
                f"ES|QL: {arguments['query']}",
                f"Rows: {len(rows)}",
                "",
                " | ".join(col_names),
                "─" * 60,
            ]
            for row in rows[:100]:
                lines.append(" | ".join(str(v) for v in row))
            if len(rows) > 100:
                lines.append(f"... {len(rows) - 100} more rows")
            return _text("\n".join(lines))

        # ── Alerts ────────────────────────────────────────────────────────────

        elif name == "get_alert_rules":
            result = await get_alert_rules()
            rules = result.get("data", [])
            lines = [f"Found {len(rules)} alert rules:", ""]
            for r in rules:
                enabled = "enabled" if r.get("enabled") else "disabled"
                lines.append(f"  [{enabled}] {r.get('name','')} (type: {r.get('rule_type_id','')}) — id: {r.get('id','')}")
            return _text("\n".join(lines))

        elif name == "get_active_alerts":
            result = await get_active_alerts(arguments["rule_id"])
            alerts = result.get("alerts", {})
            lines = [f"Active alerts for rule {arguments['rule_id']}:", ""]
            for alert_id, state in alerts.items():
                lines.append(f"  {alert_id}: {state.get('status','')}")
            return _text("\n".join(lines))

        # ── APM ───────────────────────────────────────────────────────────────

        elif name == "get_apm_services":
            result = await get_apm_services(
                environment=arguments.get("environment", "ENVIRONMENT_ALL"),
            )
            services = result.get("items", result.get("services", []))
            lines = [f"Found {len(services)} APM services:", ""]
            for s in services:
                lines.append(f"  {s.get('serviceName', s.get('name', ''))} — env: {s.get('environments', '')}")
            return _text("\n".join(lines))

        # ── ML ────────────────────────────────────────────────────────────────

        elif name == "list_ml_jobs":
            result = await list_ml_jobs()
            jobs = result.get("jobs", result.get("anomaly_detectors", []))
            lines = [f"Found {len(jobs)} ML anomaly detection jobs:", ""]
            for j in jobs:
                lines.append(f"  {j.get('job_id','')} — state: {j.get('state','')}")
            return _text("\n".join(lines))

        # ── Investigation / Scenario tools ────────────────────────────────────

        elif name == "search_service_logs":
            output = await search_service_logs(
                service=arguments["service"],
                kql=arguments.get("kql"),
                level=arguments.get("level"),
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 100),
                space=arguments.get("space"),
                org=arguments.get("org"),
                namespace=arguments.get("namespace"),
                cluster=arguments.get("cluster"),
            )
            return _text(output)

        elif name == "investigate_service_errors":
            output = await investigate_service_errors(
                service=arguments["service"],
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 100),
                space=arguments.get("space"),
                org=arguments.get("org"),
            )
            return _text(output)

        elif name == "trace_request":
            output = await trace_request(
                reqid=arguments["reqid"],
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 200),
                space=arguments.get("space"),
            )
            return _text(output)

        elif name == "search_by_org":
            output = await search_by_org(
                org_id=arguments["org_id"],
                kql_extra=arguments.get("kql"),
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 100),
                space=arguments.get("space"),
                service=arguments.get("service"),
            )
            return _text(output)

        elif name == "investigate_pod_health":
            output = await investigate_pod_health(
                service=arguments.get("service"),
                namespace=arguments.get("namespace"),
                range_minutes=arguments.get("range_minutes", 60),
                space=arguments.get("space"),
            )
            return _text(output)

        elif name == "compare_log_volume":
            output = await compare_log_volume(
                service=arguments["service"],
                baseline_minutes=arguments.get("baseline_minutes", 60),
                comparison_minutes=arguments.get("comparison_minutes", 60),
                space=arguments.get("space"),
            )
            return _text(output)

        else:
            return _text(f"Unknown tool: {name}")

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return [
            # ── Auth ──────────────────────────────────────────────────────────
            Tool(name="login",
                 description="Log in to Kibana via Okta SSO (SAML). Launches a headless Playwright browser — approve the Okta Verify push on your phone.",
                 inputSchema={"type": "object", "properties": {
                     "username": {"type": "string", "description": "Okta username (or set KIBANA_USERNAME env var)"},
                     "password": {"type": "string", "description": "Okta password (or set KIBANA_PASSWORD env var)"},
                 }}),

            Tool(name="inject_session",
                 description="Inject a Kibana session cookie obtained from browser DevTools or a HAR file.",
                 inputSchema={"type": "object", "properties": {
                     "cookie_name": {"type": "string", "description": "Cookie name (typically 'sid' for Kibana 8.x SAML)"},
                     "cookie_value": {"type": "string", "description": "Cookie value from browser DevTools"},
                     "expires_at_unix_seconds": {"type": "number", "description": "Expiry as Unix timestamp in seconds"},
                     "extra_headers": {"type": "object", "description": "Any extra headers needed", "additionalProperties": {"type": "string"}},
                 }, "required": ["cookie_name", "cookie_value", "expires_at_unix_seconds"]}),

            Tool(name="auth_status",
                 description="Check current Kibana session status and expiry.",
                 inputSchema={"type": "object", "properties": {}}),

            # ── Spaces ────────────────────────────────────────────────────────
            Tool(name="list_spaces",
                 description="List all Kibana spaces.",
                 inputSchema={"type": "object", "properties": {}}),

            # ── Dashboards ────────────────────────────────────────────────────
            Tool(name="list_dashboards",
                 description="List all dashboards in a Kibana space.",
                 inputSchema={"type": "object", "properties": {
                     "space_id": {"type": "string", "description": "Kibana space ID (omit to use configured default space)"},
                 }}),
            Tool(name="get_dashboard_info",
                 description="Get panel layout and metadata for a specific Kibana dashboard.",
                 inputSchema={"type": "object", "properties": {
                     "dashboard_id": {"type": "string", "description": "Saved object ID from list_dashboards"},
                 }, "required": ["dashboard_id"]}),

            # ── Data Views ────────────────────────────────────────────────────
            Tool(name="list_data_views",
                 description="List all Kibana data views (index patterns).",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="get_data_view",
                 description="Get fields and config for a specific data view.",
                 inputSchema={"type": "object", "properties": {
                     "data_view_id": {"type": "string"},
                 }, "required": ["data_view_id"]}),

            # ── Search ────────────────────────────────────────────────────────
            Tool(name="search_logs",
                 description="Search logs in an Elasticsearch index using KQL. Returns matching log lines.",
                 inputSchema={"type": "object", "properties": {
                     "index_pattern": {"type": "string", "description": "Index or data stream to search (e.g. 'filebeat-*-intcloud-*')"},
                     "kql": {"type": "string", "description": "KQL query (e.g. 'service.name: my-app AND log.level: error')", "default": "*"},
                     "range_minutes": {"type": "number", "default": 60},
                     "size": {"type": "number", "default": 100, "description": "Max number of results"},
                     "sort_order": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
                 }, "required": ["index_pattern"]}),

            Tool(name="run_esql",
                 description="Run an ES|QL query against Elasticsearch (requires ES 8.11+).",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string", "description": "ES|QL query string"},
                 }, "required": ["query"]}),

            # ── Alerts ────────────────────────────────────────────────────────
            Tool(name="get_alert_rules",
                 description="List all Kibana alerting rules.",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="get_active_alerts",
                 description="Get active alert instances for a specific rule.",
                 inputSchema={"type": "object", "properties": {
                     "rule_id": {"type": "string", "description": "Rule ID from get_alert_rules"},
                 }, "required": ["rule_id"]}),

            # ── APM ───────────────────────────────────────────────────────────
            Tool(name="get_apm_services",
                 description="List all services tracked in Kibana APM.",
                 inputSchema={"type": "object", "properties": {
                     "environment": {"type": "string", "description": "APM environment filter (default: all)", "default": "ENVIRONMENT_ALL"},
                 }}),

            # ── ML ────────────────────────────────────────────────────────────
            Tool(name="list_ml_jobs",
                 description="List all Elasticsearch ML anomaly detection jobs and their state.",
                 inputSchema={"type": "object", "properties": {}}),

            # ── Investigation / Scenario tools ────────────────────────────────
            Tool(name="search_service_logs",
                 description=(
                     "Primary debugging tool: search logs for a specific microservice. "
                     "Filters by kubernetes.labels.app. Optionally narrow by log level, org/tenant, "
                     "K8s namespace, cluster, or a custom KQL expression. "
                     "Auto-routes to the right Kibana space (gcs for CDI, cai for CAI) or searches both."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "service": {"type": "string", "description": "Microservice name (kubernetes.labels.app), e.g. 'vcs', 'migration', 'cai-run'"},
                     "kql": {"type": "string", "description": "Additional KQL filter, e.g. 'message: \"import job\"'"},
                     "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"], "description": "Log level filter"},
                     "range_minutes": {"type": "number", "default": 60, "description": "How far back to search"},
                     "size": {"type": "number", "default": 100},
                     "space": {"type": "string", "enum": ["gcs", "cai"], "description": "Force a specific Kibana space (omit to auto-detect)"},
                     "org": {"type": "string", "description": "Filter by tenant/org ID (dissect.catalina_out.org)"},
                     "namespace": {"type": "string", "description": "Filter by K8s namespace, e.g. 'iics-prod-nause6'"},
                     "cluster": {"type": "string", "description": "Filter by cluster, e.g. 'use6', 'use1'"},
                 }, "required": ["service"]}),

            Tool(name="investigate_service_errors",
                 description=(
                     "Find recent ERROR and WARN log lines and exceptions for a microservice. "
                     "Use this as a first step when debugging a service issue or alert."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "service": {"type": "string", "description": "Microservice name (kubernetes.labels.app)"},
                     "range_minutes": {"type": "number", "default": 60},
                     "size": {"type": "number", "default": 100},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                     "org": {"type": "string", "description": "Filter by tenant/org ID"},
                 }, "required": ["service"]}),

            Tool(name="trace_request",
                 description=(
                     "Trace a request ID (reqid) across all services to reconstruct the full call chain. "
                     "Searches dissect.catalina_out.reqid and message fields. "
                     "Results are returned in chronological order showing how the request flowed between services."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "reqid": {"type": "string", "description": "Request/correlation ID from the log context block, e.g. '8WX4wCZJyMMbqWYaAWvGWU'"},
                     "range_minutes": {"type": "number", "default": 60},
                     "size": {"type": "number", "default": 200},
                     "space": {"type": "string", "enum": ["gcs", "cai"], "description": "Omit to search both spaces"},
                 }, "required": ["reqid"]}),

            Tool(name="search_by_org",
                 description=(
                     "Search all logs for a specific tenant/org ID. Useful for debugging customer-reported issues. "
                     "Optionally narrow by service name or additional KQL."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "org_id": {"type": "string", "description": "Org/tenant ID from dissect.catalina_out.org, e.g. '0Cr2nEhbUQ5gNM3R5EpLoj'"},
                     "service": {"type": "string", "description": "Optionally narrow to a specific service"},
                     "kql": {"type": "string", "description": "Additional KQL filter"},
                     "range_minutes": {"type": "number", "default": 60},
                     "size": {"type": "number", "default": 100},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }, "required": ["org_id"]}),

            Tool(name="investigate_pod_health",
                 description=(
                     "Check Kubernetes pod events: OOMKills, CrashLoopBackOff, evictions, and restarts. "
                     "Queries k8s_controlplane-* index. Use when a service is unstable or crashing."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "service": {"type": "string", "description": "Service/pod name prefix to filter (optional)"},
                     "namespace": {"type": "string", "description": "K8s namespace filter, e.g. 'iics-prod-nause6'"},
                     "range_minutes": {"type": "number", "default": 60},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }}),

            Tool(name="compare_log_volume",
                 description=(
                     "Compare log volume and error rate between two time windows for a service. "
                     "Use after a deployment to check if error rates changed. "
                     "Baseline = earlier window, Comparison = most recent window."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "service": {"type": "string", "description": "Service name (kubernetes.labels.app)"},
                     "baseline_minutes": {"type": "number", "default": 60, "description": "Duration of baseline window in minutes"},
                     "comparison_minutes": {"type": "number", "default": 60, "description": "Duration of comparison (recent) window in minutes"},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }, "required": ["service"]}),
        ]
