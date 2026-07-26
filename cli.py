#!/usr/bin/env python3
"""
Quick CLI for testing kibana-mcp tools from the command line.

Usage:
    python cli.py <tool_name> [args...]

Examples:
    python cli.py auth_status
    python cli.py login
    python cli.py list_spaces
    python cli.py list_data_views
    python cli.py list_dashboards
    python cli.py search_service_logs vcs
    python cli.py search_service_logs vcs --level ERROR --range 30
    python cli.py investigate_service_errors migration --range 60
    python cli.py trace_request 8WX4wCZJyMMbqWYaAWvGWU
    python cli.py search_by_org 0Cr2nEhbUQ5gNM3R5EpLoj
    python cli.py investigate_pod_health --service vcs --namespace iics-prod-nause6
    python cli.py compare_log_volume vcs
    python cli.py search_logs filebeat-*-intcloud-* --kql "kubernetes.labels.app: vcs" --range 30
    python cli.py get_alert_rules
    python cli.py get_apm_services
    python cli.py list_ml_jobs
    python cli.py inject_session sid <cookie_value> <expires_unix_seconds>
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)


async def run(args):
    tool = args.tool

    # ── Auth ──────────────────────────────────────────────────────────────────

    if tool == "auth_status":
        from kibana_mcp.auth.session import load_session
        import time
        session = load_session()
        if not session:
            print("No active session. Run: python cli.py login")
            return
        expires_in = round((session.expires_at - int(time.time() * 1000)) / 60000)
        if expires_in > 0:
            print(f"Session active. Cookie: {session.cookie_name}. Expires in {expires_in} minutes.")
        else:
            print(f"Session EXPIRED {abs(expires_in)} minutes ago. Run: python cli.py login")

    elif tool == "login":
        from kibana_mcp.auth.manager import init_session
        from kibana_mcp.config import config
        username = args.username or config.kibana.username
        password = args.password or config.kibana.password
        if not username or not password:
            print("ERROR: set KIBANA_USERNAME and KIBANA_PASSWORD in .env or pass --username/--password")
            return
        print(f"Logging in as {username} via Okta SAML...")
        print("A headless browser will open. Approve the Okta Verify push on your phone.")
        session = await init_session(username, password)
        from datetime import datetime, timezone
        exp = datetime.fromtimestamp(session.expires_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"Login successful. Cookie: {session.cookie_name}. Expires: {exp}")

    elif tool == "inject_session":
        from kibana_mcp.auth.manager import inject_session
        if len(args.extra) < 3:
            print("Usage: python cli.py inject_session <cookie_name> <cookie_value> <expires_unix_seconds>")
            print("Example: python cli.py inject_session sid Fe26.2** 1753500000")
            return
        cookie_name, cookie_value, expires_str = args.extra[0], args.extra[1], args.extra[2]
        session = inject_session(cookie_name, cookie_value, int(float(expires_str)) * 1000)
        from datetime import datetime, timezone
        exp = datetime.fromtimestamp(session.expires_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"Session injected. Cookie: {session.cookie_name}. Expires: {exp}")

    # ── Spaces / Discovery ────────────────────────────────────────────────────

    elif tool == "list_spaces":
        from kibana_mcp.kibana.api import list_spaces
        spaces = await list_spaces()
        print(f"Found {len(spaces)} spaces:")
        for s in spaces:
            print(f"  {s.get('id','').ljust(22)} {s.get('name','')}")

    elif tool == "list_dashboards":
        from kibana_mcp.kibana.api import list_dashboards
        space = args.space or None
        dashboards = await list_dashboards(space)
        print(f"Found {len(dashboards)} dashboards:")
        for d in dashboards:
            print(f"  {d.get('id','').ljust(38)} {d.get('attributes',{}).get('title','')}")

    elif tool == "list_data_views":
        from kibana_mcp.kibana.api import list_data_views
        views = await list_data_views()
        print(f"Found {len(views)} data views:")
        for v in views:
            title = v.get('attributes', {}).get('title', v.get('title', ''))
            name  = v.get('attributes', {}).get('name', '')
            label = f"{title}" + (f" ({name})" if name and name != title else "")
            print(f"  {v.get('id','').ljust(38)} {label}")

    # ── Search ────────────────────────────────────────────────────────────────

    elif tool == "search_logs":
        from kibana_mcp.kibana.api import search_logs
        import time
        if not args.index:
            print("Usage: python cli.py search_logs <index_pattern> [--kql '...'] [--range 60] [--size 50]")
            return
        to_ms = int(time.time() * 1000)
        from_ms = to_ms - args.range * 60 * 1000
        result = await search_logs(args.index, args.kql or "*", from_ms, to_ms, args.size)
        hits = result.get("hits", {})
        total = hits.get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else total
        docs = hits.get("hits", [])
        print(f"Total hits: {total_count} (showing {len(docs)})")
        print(f"Index: {args.index}  KQL: {args.kql or '*'}  Range: last {args.range}m")
        print()
        for doc in docs:
            src = doc.get("_source", {})
            ts  = src.get("@timestamp", "")
            msg = src.get("dissect.catalina_out.message") or src.get("message", str(src)[:120])
            print(f"  [{ts}] {str(msg).strip()[:200]}")

    # ── Alerts ────────────────────────────────────────────────────────────────

    elif tool == "get_alert_rules":
        from kibana_mcp.kibana.api import get_alert_rules
        result = await get_alert_rules()
        rules = result.get("data", [])
        print(f"Found {len(rules)} alert rules:")
        for r in rules:
            enabled = "ON " if r.get("enabled") else "OFF"
            print(f"  [{enabled}] {r.get('name','').ljust(50)} type={r.get('rule_type_id','')}  id={r.get('id','')}")

    elif tool == "get_active_alerts":
        from kibana_mcp.kibana.api import get_active_alerts
        if not args.rule_id:
            print("Usage: python cli.py get_active_alerts --rule-id <id>")
            return
        result = await get_active_alerts(args.rule_id)
        alerts = result.get("alerts", {})
        print(f"Active alerts for rule {args.rule_id}: {len(alerts)}")
        for aid, state in alerts.items():
            print(f"  {aid}: {state.get('status','')}")

    # ── APM / ML ──────────────────────────────────────────────────────────────

    elif tool == "get_apm_services":
        from kibana_mcp.kibana.api import get_apm_services
        result = await get_apm_services()
        services = result.get("items", result.get("services", []))
        print(f"Found {len(services)} APM services:")
        for s in services:
            print(f"  {s.get('serviceName', s.get('name',''))}")

    elif tool == "list_ml_jobs":
        from kibana_mcp.kibana.api import list_ml_jobs
        result = await list_ml_jobs()
        jobs = result.get("jobs", result.get("anomaly_detectors", []))
        print(f"Found {len(jobs)} ML jobs:")
        for j in jobs:
            print(f"  {j.get('job_id','').ljust(40)} state={j.get('state','')}")

    # ── Investigation tools ───────────────────────────────────────────────────

    elif tool == "search_service_logs":
        from kibana_mcp.tools.scenarios import search_service_logs
        if not args.service:
            print("Usage: python cli.py search_service_logs <service> [--level ERROR] [--range 60] [--org <id>] [--namespace <ns>] [--kql '...'] [--space gcs|cai]")
            return
        print(await search_service_logs(
            service=args.service,
            kql=args.kql,
            level=args.level,
            range_minutes=args.range,
            size=args.size,
            space=args.space,
            org=args.org,
            namespace=args.namespace,
            cluster=args.cluster,
        ))

    elif tool == "investigate_service_errors":
        from kibana_mcp.tools.scenarios import investigate_service_errors
        if not args.service:
            print("Usage: python cli.py investigate_service_errors <service> [--range 60] [--org <id>] [--space gcs|cai]")
            return
        print(await investigate_service_errors(
            service=args.service,
            range_minutes=args.range,
            size=args.size,
            space=args.space,
            org=args.org,
        ))

    elif tool == "trace_request":
        from kibana_mcp.tools.scenarios import trace_request
        if not args.reqid:
            print("Usage: python cli.py trace_request <reqid> [--range 60] [--space gcs|cai]")
            return
        print(await trace_request(
            reqid=args.reqid,
            range_minutes=args.range,
            size=args.size,
            space=args.space,
        ))

    elif tool == "search_by_org":
        from kibana_mcp.tools.scenarios import search_by_org
        if not args.org_id:
            print("Usage: python cli.py search_by_org <org_id> [--service <svc>] [--kql '...'] [--range 60] [--space gcs|cai]")
            return
        print(await search_by_org(
            org_id=args.org_id,
            kql_extra=args.kql,
            range_minutes=args.range,
            size=args.size,
            space=args.space,
            service=args.service,
        ))

    elif tool == "investigate_pod_health":
        from kibana_mcp.tools.scenarios import investigate_pod_health
        print(await investigate_pod_health(
            service=args.service,
            namespace=args.namespace,
            range_minutes=args.range,
            space=args.space,
        ))

    elif tool == "compare_log_volume":
        from kibana_mcp.tools.scenarios import compare_log_volume
        if not args.service:
            print("Usage: python cli.py compare_log_volume <service> [--baseline 60] [--range 60] [--space gcs|cai]")
            return
        print(await compare_log_volume(
            service=args.service,
            baseline_minutes=args.baseline,
            comparison_minutes=args.range,
            space=args.space,
        ))

    elif tool == "debug_search":
        # Raw bsearch response dump — used to diagnose zero-hit issues
        from kibana_mcp.kibana.client import kibana_post
        from kibana_mcp.config import config
        import time, json
        service = args.service or "vcs"
        to_ms = int(time.time() * 1000)
        from_ms = to_ms - args.range * 60 * 1000
        space = args.space or config.kibana.space_id or "gcs"
        path = f"/s/{space}/internal/bsearch?compress=false"
        body = {
            "batch": [{
                "request": {
                    "params": {
                        "index": "filebeat-*-intcloud-*",
                        "body": {
                            "query": {
                                "bool": {
                                    "must": [
                                        {"query_string": {"query": f'kubernetes.labels.app: "{service}"', "analyze_wildcard": True}},
                                        {"range": {"@timestamp": {"gte": from_ms, "lte": to_ms, "format": "epoch_millis"}}},
                                    ]
                                }
                            },
                            "size": 2,
                            "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "boolean"}}],
                        }
                    }
                }
            }]
        }
        print(f"POST {config.kibana.base_url}{path}")
        print(f"Range: {from_ms} - {to_ms}  ({args.range} minutes)")
        raw = await kibana_post(path, body)
        print("--- RAW RESPONSE ---")
        print(json.dumps(raw, indent=2)[:3000])

    else:
        print(f"Unknown tool: {tool}")
        print("Run: python cli.py --help")


def main():
    parser = argparse.ArgumentParser(
        description="Kibana MCP CLI — test tools from the command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("tool", help="Tool name to run")

    # Positional extras (for inject_session)
    parser.add_argument("extra", nargs="*", help="Extra positional args (e.g. cookie values)")

    # Common optional args
    parser.add_argument("--range",     type=int,   default=60,   metavar="MINUTES", help="Time range in minutes (default: 60)")
    parser.add_argument("--size",      type=int,   default=100,  help="Max results (default: 100)")
    parser.add_argument("--space",     type=str,   default=None, choices=["gcs", "cai"], help="Kibana space")
    parser.add_argument("--kql",       type=str,   default=None, help="KQL filter expression")
    parser.add_argument("--level",     type=str,   default=None, choices=["ERROR", "WARN", "INFO", "DEBUG"], help="Log level filter")
    parser.add_argument("--service",   type=str,   default=None, help="Microservice name (kubernetes.labels.app)")
    parser.add_argument("--org",       type=str,   default=None, help="Org/tenant ID")
    parser.add_argument("--namespace", type=str,   default=None, help="K8s namespace")
    parser.add_argument("--cluster",   type=str,   default=None, help="Cluster name (e.g. use6)")
    parser.add_argument("--index",     type=str,   default=None, help="Index pattern for search_logs")
    parser.add_argument("--rule-id",   type=str,   dest="rule_id", default=None, help="Alert rule ID")
    parser.add_argument("--baseline",  type=int,   default=60,   help="Baseline window in minutes for compare_log_volume")
    parser.add_argument("--username",  type=str,   default=None, help="Okta username (overrides .env)")
    parser.add_argument("--password",  type=str,   default=None, help="Okta password (overrides .env)")

    # For trace_request, service positional becomes reqid
    args = parser.parse_args()

    # Resolve positional "service" and "reqid" and "org_id" from the extra list
    if args.tool in ("search_service_logs", "investigate_service_errors", "compare_log_volume") and args.extra and not args.service:
        args.service = args.extra[0]
    if args.tool == "trace_request" and args.extra:
        args.reqid = args.extra[0]
    else:
        args.reqid = getattr(args, "reqid", None) or (args.extra[0] if args.extra and args.tool == "trace_request" else None)
    if args.tool == "search_by_org" and args.extra and not hasattr(args, "org_id"):
        args.org_id = args.extra[0]
    else:
        args.org_id = getattr(args, "org_id", None) or (args.extra[0] if args.extra and args.tool == "search_by_org" else None)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
