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
    count_by_field, get_log_histogram, get_context_around, list_fields,
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


def _parse_time_window(arguments: Dict[str, Any]) -> tuple:
    """
    Parse time_from / time_to ISO strings (always IST from the caller) into epoch ms.
    Returns (from_ms, to_ms) or (None, None) if not provided.
    IST = UTC+5:30, so subtract 5h30m to get UTC epoch ms.
    """
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    from_str = arguments.get("time_from")
    to_str = arguments.get("time_to")
    if not from_str or not to_str:
        return None, None
    try:
        def _to_ms(s: str) -> int:
            s = s.strip()
            # Try parsing with timezone info first
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                        "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z"):
                try:
                    dt = datetime.strptime(s, fmt)
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    pass
            # No timezone info — assume IST
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    dt = datetime.strptime(s, fmt).replace(tzinfo=IST)
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    pass
            raise ValueError(f"Cannot parse time: {s}")
        return _to_ms(from_str), _to_ms(to_str)
    except Exception:
        return None, None


def register_tools(server: Server) -> None:

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> list:
        try:
            return await _dispatch(name, arguments)
        except SessionExpiredError:
            return _text("⚠️ Could not establish a Kibana session — browser login may have been closed or timed out.")
        except NotImplementedError as e:
            return _text(f"Not yet implemented: {e}")
        except RuntimeError as e:
            return _text(f"Kibana API error: {e}")
        except Exception as e:
            return _text(f"Error executing {name}: {type(e).__name__}: {e}")

    async def _dispatch(name: str, arguments: Dict[str, Any]) -> list:

        # ── Auth ──────────────────────────────────────────────────────────────

        if name == "login":
            s = await init_session()
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
            kql_str = arguments.get("kql", "*")
            now_ms = int(time.time() * 1000)
            # If the KQL contains explicit @timestamp filters, don't let range_minutes
            # create a conflicting ES range clause — set the range fence wide open so
            # only the KQL timestamps control what documents are returned.
            if "@timestamp" in kql_str:
                from_ms = 0
                to_ms = now_ms + 86_400_000  # +1 day buffer for any timezone edge
            else:
                from_ms = now_ms - range_min * 60 * 1000
                to_ms = now_ms
            sl_space = arguments.get("space")
            if sl_space:
                from kibana_mcp.config import config as _cfg
                _orig_sl = _cfg.kibana.space_id
                _cfg.kibana.space_id = sl_space
                try:
                    result = await search_logs(
                        index_pattern=arguments["index_pattern"],
                        kql=kql_str,
                        from_ms=from_ms,
                        to_ms=to_ms,
                        size=arguments.get("size", 100),
                        sort_order=arguments.get("sort_order", "desc"),
                    )
                finally:
                    _cfg.kibana.space_id = _orig_sl
            else:
                result = await search_logs(
                    index_pattern=arguments["index_pattern"],
                    kql=kql_str,
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
            esql_space = arguments.get("space")
            if esql_space:
                from kibana_mcp.config import config as _cfg
                _orig = _cfg.kibana.space_id
                _cfg.kibana.space_id = esql_space
                try:
                    result = await run_esql(arguments["query"])
                finally:
                    _cfg.kibana.space_id = _orig
            else:
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
            f_ms, t_ms = _parse_time_window(arguments)
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
                from_ms=f_ms,
                to_ms=t_ms,
            )
            return _text(output)

        elif name == "investigate_service_errors":
            f_ms, t_ms = _parse_time_window(arguments)
            output = await investigate_service_errors(
                service=arguments["service"],
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 100),
                space=arguments.get("space"),
                org=arguments.get("org"),
                from_ms=f_ms,
                to_ms=t_ms,
            )
            return _text(output)

        elif name == "trace_request":
            f_ms, t_ms = _parse_time_window(arguments)
            output = await trace_request(
                reqid=arguments["reqid"],
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 200),
                space=arguments.get("space"),
                from_ms=f_ms,
                to_ms=t_ms,
            )
            return _text(output)

        elif name == "search_by_org":
            f_ms, t_ms = _parse_time_window(arguments)
            output = await search_by_org(
                org_id=arguments["org_id"],
                kql_extra=arguments.get("kql"),
                range_minutes=arguments.get("range_minutes", 60),
                size=arguments.get("size", 100),
                space=arguments.get("space"),
                service=arguments.get("service"),
                from_ms=f_ms,
                to_ms=t_ms,
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

        # ── Utility tools (borrowed from community impl) ──────────────────────

        elif name == "get_log_context":
            range_min = arguments.get("range_minutes", 60)
            to_ms = int(time.time() * 1000)
            from_ms = to_ms - range_min * 60 * 1000
            result = await get_context_around(
                index_pattern=arguments.get("index_pattern", "filebeat-*-intcloud-*"),
                timestamp=arguments["timestamp"],
                before=arguments.get("before", 20),
                after=arguments.get("after", 20),
                service=arguments.get("service"),
                space=arguments.get("space"),
            )
            before_hits = result["before"]
            after_hits = result["after"]
            pivot = result["pivot"]
            lines = []
            if before_hits:
                lines.append(f"=== {len(before_hits)} entries BEFORE {pivot} ===")
                for doc in before_hits:
                    src = doc.get("_source", {})
                    ts = src.get("@timestamp", "")
                    msg = src.get("dissect.catalina_out.message") or src.get("message", "")
                    svc = src.get("kubernetes.labels.app", "")
                    lvl = src.get("dissect.catalina_out.level", "")
                    lines.append(f"  [{ts}] [{lvl}] [{svc}] {str(msg).strip()[:300]}")
            lines.append(f"\n=== PIVOT: {pivot} ===\n")
            if after_hits:
                lines.append(f"=== {len(after_hits)} entries AFTER {pivot} ===")
                for doc in after_hits:
                    src = doc.get("_source", {})
                    ts = src.get("@timestamp", "")
                    msg = src.get("dissect.catalina_out.message") or src.get("message", "")
                    svc = src.get("kubernetes.labels.app", "")
                    lvl = src.get("dissect.catalina_out.level", "")
                    lines.append(f"  [{ts}] [{lvl}] [{svc}] {str(msg).strip()[:300]}")
            return _text("\n".join(lines) if lines else "No log entries found around that timestamp.")

        elif name == "count_by_field":
            range_min = arguments.get("range_minutes", 60)
            to_ms = int(time.time() * 1000)
            from_ms = to_ms - range_min * 60 * 1000
            result = await count_by_field(
                index_pattern=arguments.get("index_pattern", "filebeat-*-intcloud-*"),
                field=arguments["field"],
                from_ms=from_ms,
                to_ms=to_ms,
                size=min(int(arguments.get("max_buckets", 20)), 100),
                service=arguments.get("service"),
                level=arguments.get("level"),
                space=arguments.get("space"),
            )
            buckets = result.get("aggregations", {}).get("by_field", {}).get("buckets", [])
            if not buckets:
                return _text(f"No values found for field '{arguments['field']}'.")
            lines = [f"{'Value':<60} {'Count':>10}", "─" * 72]
            for b in buckets:
                key = str(b["key"])[:57] + "..." if len(str(b["key"])) > 60 else str(b["key"])
                lines.append(f"{key:<60} {b['doc_count']:>10,}")
            lines += ["─" * 72, f"{'Total (shown)':<60} {sum(b['doc_count'] for b in buckets):>10,}"]
            return _text("\n".join(lines))

        elif name == "log_histogram":
            range_min = arguments.get("range_minutes", 360)
            to_ms = int(time.time() * 1000)
            from_ms = to_ms - range_min * 60 * 1000
            interval = arguments.get("interval", "30m")
            result = await get_log_histogram(
                index_pattern=arguments.get("index_pattern", "filebeat-*-intcloud-*"),
                from_ms=from_ms,
                to_ms=to_ms,
                interval=interval,
                service=arguments.get("service"),
                level=arguments.get("level"),
                kql=arguments.get("kql"),
                space=arguments.get("space"),
            )
            buckets = result.get("aggregations", {}).get("over_time", {}).get("buckets", [])
            if not buckets:
                return _text("No data found for the requested time range.")
            max_count = max(b["doc_count"] for b in buckets) or 1
            bar_width = 40
            lines = [f"Log volume over time (interval: {interval})", "=" * 70]
            for b in buckets:
                ts = str(b.get("key_as_string", b.get("key", "?")))[:19]
                count = b["doc_count"]
                bar = "█" * int(count / max_count * bar_width)
                lines.append(f"{ts}  {bar:<{bar_width}} {count:>6,}")
            total = sum(b["doc_count"] for b in buckets)
            lines.append(f"\nTotal: {total:,} entries across {len(buckets)} buckets")
            return _text("\n".join(lines))

        elif name == "list_fields":
            result = await list_fields(
                index_pattern=arguments.get("index_pattern", "filebeat-*-intcloud-*"),
                filter_pattern=arguments.get("filter_pattern"),
                popular_only=arguments.get("popular_only", True),
                space=arguments.get("space"),
            )
            source = result.get("source", "")
            if source == "sample":
                fields_list = result.get("fields", [])
                lines = [f"Fields from 50 recent docs ({len(fields_list)} total):"]
                lines += [f"  {f}" for f in fields_list]
                lines.append("\n_Run with popular_only=false for full field_caps scan._")
                return _text("\n".join(lines))
            else:
                fields_dict = result.get("fields", {})
                lines = [f"{'Field':<60} {'Types'}", "─" * 80]
                for fname, types in fields_dict.items():
                    lines.append(f"{fname:<60} {', '.join(types)}")
                lines.append(f"\n{len(fields_dict)} fields")
                return _text("\n".join(lines))

        else:
            return _text(f"Unknown tool: {name}")

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return [
            # ── Auth ──────────────────────────────────────────────────────────
            Tool(name="login",
                 description="Opens a browser window — sign in with Okta and approve the push. No credentials needed.",
                 inputSchema={"type": "object", "properties": {}}),

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
                 description=(
                     "Search logs in an Elasticsearch index using KQL. Returns matching log lines. "
                     "If your KQL contains explicit @timestamp filters (e.g. @timestamp >= '2026-07-23T08:36:00Z'), "
                     "the range_minutes parameter is ignored and the KQL timestamps control the window. "
                     "Always use Z-suffix UTC timestamps in KQL (e.g. '2026-07-23T08:36:00Z')."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "index_pattern": {"type": "string", "description": "Index or data stream to search (e.g. 'filebeat-*-intcloud-*')"},
                     "kql": {"type": "string", "description": "KQL query (e.g. 'service.name: my-app AND log.level: error')", "default": "*"},
                     "range_minutes": {"type": "number", "default": 60, "description": "How far back to search. Ignored if KQL contains @timestamp filters."},
                     "size": {"type": "number", "default": 100, "description": "Max number of results"},
                     "sort_order": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
                     "space": {"type": "string", "enum": ["gcs", "cai"], "description": "Kibana space to search in (omit to use configured default)"},
                 }, "required": ["index_pattern"]}),

            Tool(name="run_esql",
                 description="Run an ES|QL query against Elasticsearch (requires ES 8.11+).",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string", "description": "ES|QL query string"},
                     "space": {"type": "string", "enum": ["gcs", "cai"], "description": "Kibana space to run the query in (omit for default)"},
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
                     "Auto-routes to the right Kibana space (gcs for CDI, cai for CAI) or searches both. "
                     "Use time_from/time_to (IST) for a precise historical window; otherwise range_minutes from now."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "service": {"type": "string", "description": "Microservice name (kubernetes.labels.app), e.g. 'vcs', 'migration', 'cai-run'"},
                     "kql": {"type": "string", "description": "Additional KQL filter"},
                     "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"]},
                     "range_minutes": {"type": "number", "default": 60, "description": "How far back from now (ignored if time_from/time_to set)"},
                     "time_from": {"type": "string", "description": "Window start in IST, e.g. '2026-07-23 14:06:00'. Takes priority over range_minutes."},
                     "time_to": {"type": "string", "description": "Window end in IST, e.g. '2026-07-23 14:06:30'."},
                     "size": {"type": "number", "default": 100},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                     "org": {"type": "string", "description": "Filter by tenant/org ID"},
                     "namespace": {"type": "string", "description": "Filter by K8s namespace"},
                     "cluster": {"type": "string", "description": "Filter by cluster, e.g. 'use6'"},
                 }, "required": ["service"]}),

            Tool(name="investigate_service_errors",
                 description=(
                     "Find ERROR and WARN log lines and exceptions for a microservice. "
                     "Use time_from/time_to (IST) for a precise historical window."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "service": {"type": "string", "description": "Microservice name (kubernetes.labels.app)"},
                     "range_minutes": {"type": "number", "default": 60, "description": "Ignored if time_from/time_to set"},
                     "time_from": {"type": "string", "description": "Window start in IST, e.g. '2026-07-23 14:06:00'"},
                     "time_to": {"type": "string", "description": "Window end in IST, e.g. '2026-07-23 14:06:30'"},
                     "size": {"type": "number", "default": 100},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                     "org": {"type": "string", "description": "Filter by tenant/org ID"},
                 }, "required": ["service"]}),

            Tool(name="trace_request",
                 description=(
                     "Trace a request ID (reqid) across all services to reconstruct the full call chain. "
                     "Searches dissect.catalina_out.reqid and message fields. "
                     "Results are returned in chronological order. "
                     "Use time_from/time_to (IST) when the request happened at a known time in the past."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "reqid": {"type": "string", "description": "Request/correlation ID, e.g. '8WX4wCZJyMMbqWYaAWvGWU'"},
                     "range_minutes": {"type": "number", "default": 60, "description": "Ignored if time_from/time_to set"},
                     "time_from": {"type": "string", "description": "Window start in IST"},
                     "time_to": {"type": "string", "description": "Window end in IST"},
                     "size": {"type": "number", "default": 200},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }, "required": ["reqid"]}),

            Tool(name="search_by_org",
                 description=(
                     "Search all logs for a specific tenant/org ID. Searches the full message text so it "
                     "finds all log formats: CDI catalina logs (dissect.catalina_out.org), "
                     "CAI/Azure logs (TenantId=...), and any other format that embeds the org ID. "
                     "Use time_from/time_to (IST) for a precise historical window."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "org_id": {"type": "string", "description": "Org/tenant ID, e.g. '30gkAqkWMvmjJOwn2w5IkT'"},
                     "service": {"type": "string", "description": "Optionally narrow to a specific service"},
                     "kql": {"type": "string", "description": "Additional KQL filter, e.g. 'error'"},
                     "range_minutes": {"type": "number", "default": 60, "description": "Ignored if time_from/time_to set"},
                     "time_from": {"type": "string", "description": "Window start in IST, e.g. '2026-07-23 14:06:00'"},
                     "time_to": {"type": "string", "description": "Window end in IST, e.g. '2026-07-23 14:06:30'"},
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

            # ── Utility tools ─────────────────────────────────────────────────
            Tool(name="get_log_context",
                 description=(
                     "Fetch N log entries immediately before and after a specific timestamp. "
                     "Use this to understand what led up to an error and what happened afterwards. "
                     "Provide the exact ISO 8601 timestamp from a log entry (e.g. from investigate_service_errors output)."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "timestamp": {"type": "string", "description": "ISO 8601 pivot timestamp, e.g. '2024-01-15T10:45:30.123Z'"},
                     "index_pattern": {"type": "string", "default": "filebeat-*-intcloud-*"},
                     "service": {"type": "string", "description": "Restrict context to a specific service (optional)"},
                     "before": {"type": "number", "default": 20, "description": "Entries to fetch before the timestamp"},
                     "after": {"type": "number", "default": 20, "description": "Entries to fetch after the timestamp"},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }, "required": ["timestamp"]}),

            Tool(name="count_by_field",
                 description=(
                     "Aggregate log counts grouped by any field value. "
                     "Examples: error count per service, log level distribution across a namespace, "
                     "most active org IDs in the last hour. Returns a table sorted by count descending."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "field": {"type": "string", "description": "Field to group by, e.g. 'kubernetes.labels.app', 'dissect.catalina_out.level', 'kubernetes.namespace'"},
                     "index_pattern": {"type": "string", "default": "filebeat-*-intcloud-*"},
                     "range_minutes": {"type": "number", "default": 60},
                     "max_buckets": {"type": "number", "default": 20, "description": "Max distinct values to return (max 100)"},
                     "service": {"type": "string", "description": "Pre-filter to a specific service"},
                     "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"], "description": "Pre-filter to a log level"},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }, "required": ["field"]}),

            Tool(name="log_histogram",
                 description=(
                     "Count log entries over time in fixed-width buckets and render as a text bar chart. "
                     "Useful for spotting error spikes, sustained failures, or confirming a problem is ongoing vs historical. "
                     "E.g. 'show me error volume for vcs over the last 6 hours in 30-minute buckets'."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "index_pattern": {"type": "string", "default": "filebeat-*-intcloud-*"},
                     "range_minutes": {"type": "number", "default": 360, "description": "How far back to show (default 6 hours)"},
                     "interval": {"type": "string", "default": "30m", "description": "Bucket size: 1m, 5m, 15m, 30m, 1h, 6h, 1d"},
                     "service": {"type": "string", "description": "Filter to a specific service"},
                     "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"]},
                     "kql": {"type": "string", "description": "Additional KQL filter"},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }}),

            Tool(name="list_fields",
                 description=(
                     "Discover fields available in a log index. "
                     "popular_only=true (default) samples 50 recent docs — fast, good for most queries. "
                     "popular_only=false queries _field_caps for all indexed fields with their types — "
                     "use when you need to find an exact field name. "
                     "Use filter_pattern to narrow results, e.g. 'dissect' or 'kubernetes'."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "index_pattern": {"type": "string", "default": "filebeat-*-intcloud-*"},
                     "filter_pattern": {"type": "string", "description": "Case-insensitive substring to filter field names"},
                     "popular_only": {"type": "boolean", "default": True, "description": "True = fast sample scan; False = full field_caps (slow)"},
                     "space": {"type": "string", "enum": ["gcs", "cai"]},
                 }}),
        ]
