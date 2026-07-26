"""
Investigation / scenario tools for Kibana MCP server.

These are higher-level tools that compose multiple API calls to answer
common debugging questions about microservices running on IDMC/Kubernetes.

Log field reference (from Filebeat + Logstash dissect of catalina/Java logs):
  kubernetes.labels.app          — service name (vcs, migration, cai-run, etc.)
  kubernetes.namespace           — K8s namespace (iics-prod-nause6, etc.)
  kubernetes.pod.name            — pod name
  dissect.catalina_out.level     — log level (INFO, DEBUG, ERROR, WARN)
  dissect.catalina_out.message   — parsed log message body
  dissect.catalina_out.reqid     — request/correlation ID for tracing
  dissect.catalina_out.app       — app path (/vcs, /migration, /cai-run)
  dissect.catalina_out.org       — org/tenant ID
  dissect.catalina_out.sn        — service name short form
  CT_PODNAME                     — pod group (AWS-PROD-USE1-POD6)
  POD_cluster_name               — cluster name (use6, use1, etc.)

Space conventions:
  gcs  — CDI (Cloud Data Integration) workloads
  cai  — CAI (Cloud Application Integration) workloads
"""
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


def _range_ms(range_minutes: int) -> Tuple[int, int]:
    to_ms = _now_ms()
    return to_ms - range_minutes * 60 * 1000, to_ms


def _format_hit(doc: dict) -> str:
    src = doc.get("_source", {})
    fields = doc.get("fields", {})

    ts = (fields.get("@timestamp", [None])[0] if fields else None) or src.get("@timestamp", "")
    level = src.get("dissect.catalina_out.level") or src.get("log.level", "")
    app = src.get("dissect.catalina_out.app") or src.get("kubernetes.labels.app", "")
    msg = src.get("dissect.catalina_out.message") or src.get("message", str(src)[:200])
    pod = src.get("kubernetes.pod.name", "")

    parts = [f"[{ts}]"]
    if level:
        parts.append(f"[{level}]")
    if app:
        parts.append(f"[{app}]")
    if pod:
        parts.append(f"[{pod}]")
    parts.append(str(msg).strip())
    return " ".join(parts)


def _hits_from_bsearch(result: dict) -> Tuple[int, List[dict]]:
    hits_obj = result.get("hits", {})
    total = hits_obj.get("total", {})
    total_count = total.get("value", 0) if isinstance(total, dict) else total
    docs = hits_obj.get("hits", [])
    return total_count, docs


async def _search_space(
    space: str,
    index_pattern: str,
    kql: str,
    from_ms: int,
    to_ms: int,
    size: int = 200,
    sort_order: str = "desc",
) -> dict:
    from kibana_mcp.kibana.api import search_logs
    from kibana_mcp.config import config

    orig_space = config.kibana.space_id
    config.kibana.space_id = space
    try:
        return await search_logs(index_pattern, kql, from_ms, to_ms, size, sort_order)
    finally:
        config.kibana.space_id = orig_space


async def _search_both_spaces(
    index_pattern: str,
    kql: str,
    from_ms: int,
    to_ms: int,
    size: int = 100,
) -> Dict[str, dict]:
    """Search GCS and CAI spaces in parallel, return deduplicated {space: result}.
    GCS and CAI often point to overlapping indices — deduplicate by _id, keeping GCS copy.
    """
    gcs_task = asyncio.create_task(_search_space("gcs", index_pattern, kql, from_ms, to_ms, size))
    cai_task = asyncio.create_task(_search_space("cai", index_pattern, kql, from_ms, to_ms, size))
    gcs_result, cai_result = await asyncio.gather(gcs_task, cai_task, return_exceptions=True)

    empty = {"hits": {"hits": [], "total": {"value": 0}}}
    gcs = gcs_result if not isinstance(gcs_result, Exception) else empty
    cai = cai_result if not isinstance(cai_result, Exception) else empty

    # Remove from CAI any doc already returned by GCS
    gcs_ids = {d.get("_id") for d in gcs.get("hits", {}).get("hits", []) if d.get("_id")}
    cai_hits = cai.get("hits", {}).get("hits", [])
    cai_deduped = [d for d in cai_hits if d.get("_id") not in gcs_ids]
    if len(cai_deduped) < len(cai_hits):
        cai = dict(cai)
        cai["hits"] = dict(cai.get("hits", {}))
        cai["hits"]["hits"] = cai_deduped
        # Adjust total if it was exact
        orig_total = cai["hits"].get("total", {})
        if isinstance(orig_total, dict):
            cai["hits"]["total"] = {"value": orig_total.get("value", 0) - (len(cai_hits) - len(cai_deduped)), "relation": "eq"}

    return {"gcs": gcs, "cai": cai}


def _pick_space(service: Optional[str], explicit_space: Optional[str]) -> Optional[str]:
    """Heuristic: CAI services go to cai space, CDI/other go to gcs."""
    if explicit_space:
        return explicit_space
    if not service:
        return None  # caller will search both
    cai_prefixes = ("cai-", "cai_", "iics-cai", "process-server", "active-vcs", "active-bpel")
    if any(service.lower().startswith(p) for p in cai_prefixes):
        return "cai"
    return "gcs"


# ── Investigation Functions ────────────────────────────────────────────────────

async def investigate_service_errors(
    service: str,
    range_minutes: int = 60,
    size: int = 100,
    space: Optional[str] = None,
    org: Optional[str] = None,
) -> str:
    """Find recent ERROR/EXCEPTION log lines for a specific microservice."""
    from_ms, to_ms = _range_ms(range_minutes)
    chosen_space = _pick_space(service, space)

    kql_parts = [
        f'kubernetes.labels.app: "{service}"',
        '(dissect.catalina_out.level: "ERROR" OR dissect.catalina_out.level: "WARN" OR message: "*Exception*" OR message: "*ERROR*")',
    ]
    if org:
        kql_parts.append(f'dissect.catalina_out.org: "{org}"')
    kql = " AND ".join(kql_parts)

    if chosen_space:
        result = await _search_space(chosen_space, "filebeat-*-intcloud-*", kql, from_ms, to_ms, size)
        spaces_searched = [chosen_space]
        results_by_space = {chosen_space: result}
    else:
        results_by_space = await _search_both_spaces("filebeat-*-intcloud-*", kql, from_ms, to_ms, size // 2)
        spaces_searched = list(results_by_space.keys())

    lines = [
        f"Service error investigation: {service}",
        f"Time range: last {range_minutes} minutes",
        f"Spaces: {', '.join(spaces_searched)}",
        "",
    ]

    for sp, result in results_by_space.items():
        total, docs = _hits_from_bsearch(result)
        lines.append(f"[{sp.upper()}] {total} matching events (showing {len(docs)})")
        for doc in docs:
            lines.append(f"  {_format_hit(doc)}")
        lines.append("")

    return "\n".join(lines)


async def trace_request(
    reqid: str,
    range_minutes: int = 60,
    size: int = 200,
    space: Optional[str] = None,
) -> str:
    """Trace a request ID across all services to reconstruct the full call chain."""
    from_ms, to_ms = _range_ms(range_minutes)

    # reqid appears in the structured context block in the message field
    kql = f'dissect.catalina_out.reqid: "{reqid}" OR message: "{reqid}"'

    if space:
        result = await _search_space(space, "filebeat-*-intcloud-*", kql, from_ms, to_ms, size, "asc")
        results_by_space = {space: result}
    else:
        gcs = asyncio.create_task(_search_space("gcs", "filebeat-*-intcloud-*", kql, from_ms, to_ms, size // 2, "asc"))
        cai = asyncio.create_task(_search_space("cai", "filebeat-*-intcloud-*", kql, from_ms, to_ms, size // 2, "asc"))
        gcs_r, cai_r = await asyncio.gather(gcs, cai, return_exceptions=True)
        results_by_space = {
            "gcs": gcs_r if not isinstance(gcs_r, Exception) else {},
            "cai": cai_r if not isinstance(cai_r, Exception) else {},
        }

    all_docs = []
    seen_ids = set()
    for sp, result in results_by_space.items():
        _, docs = _hits_from_bsearch(result)
        for doc in docs:
            doc_id = doc.get("_id", "")
            if doc_id and doc_id in seen_ids:
                continue  # same ES document returned by both spaces
            if doc_id:
                seen_ids.add(doc_id)
            doc["_space"] = sp
            all_docs.append(doc)

    # Sort by timestamp ascending for chronological trace
    def ts_key(doc):
        src = doc.get("_source", {})
        fields = doc.get("fields", {})
        return (fields.get("@timestamp", [None])[0] if fields else None) or src.get("@timestamp", "")

    all_docs.sort(key=ts_key)

    lines = [
        f"Request trace: {reqid}",
        f"Time range: last {range_minutes} minutes",
        f"Total events: {len(all_docs)}",
        "",
    ]

    services_seen = set()
    for doc in all_docs:
        src = doc.get("_source", {})
        svc = src.get("kubernetes.labels.app", "") or src.get("dissect.catalina_out.app", "")
        if svc:
            services_seen.add(svc)
        sp = doc.get("_space", "")
        line = _format_hit(doc)
        lines.append(f"  [{sp}] {line}")

    lines.append("")
    lines.append(f"Services involved: {', '.join(sorted(services_seen))}")
    return "\n".join(lines)


async def search_by_org(
    org_id: str,
    kql_extra: Optional[str] = None,
    range_minutes: int = 60,
    size: int = 100,
    space: Optional[str] = None,
    service: Optional[str] = None,
) -> str:
    """Search all logs for a specific tenant/org ID, optionally narrowed by service or KQL."""
    from_ms, to_ms = _range_ms(range_minutes)

    kql_parts = [f'dissect.catalina_out.org: "{org_id}"']
    if service:
        kql_parts.append(f'kubernetes.labels.app: "{service}"')
    if kql_extra:
        kql_parts.append(f"({kql_extra})")
    kql = " AND ".join(kql_parts)

    if space:
        result = await _search_space(space, "filebeat-*-intcloud-*", kql, from_ms, to_ms, size)
        results_by_space = {space: result}
    else:
        results_by_space = await _search_both_spaces("filebeat-*-intcloud-*", kql, from_ms, to_ms, size // 2)

    lines = [
        f"Org search: {org_id}",
        f"Time range: last {range_minutes} minutes",
        "",
    ]
    for sp, result in results_by_space.items():
        total, docs = _hits_from_bsearch(result)
        lines.append(f"[{sp.upper()}] {total} events (showing {len(docs)})")
        for doc in docs:
            lines.append(f"  {_format_hit(doc)}")
        lines.append("")

    return "\n".join(lines)


async def investigate_pod_health(
    service: Optional[str] = None,
    namespace: Optional[str] = None,
    range_minutes: int = 60,
    space: Optional[str] = None,
) -> str:
    """Check K8s pod events: OOMKills, crashes, restarts from k8s_controlplane-*."""
    from_ms, to_ms = _range_ms(range_minutes)
    chosen_space = space or _pick_space(service, None) or "gcs"

    kql_parts = ['(message: "OOMKill*" OR message: "Back-off*" OR message: "*CrashLoopBackOff*" OR message: "*Killing*" OR message: "*Failed*" OR message: "*Evicted*")']
    if service:
        kql_parts.append(f'kubernetes.labels.app: "{service}" OR kubernetes.pod.name: "{service}*"')
    if namespace:
        kql_parts.append(f'kubernetes.namespace: "{namespace}"')
    kql = " AND ".join(kql_parts)

    result = await _search_space(chosen_space, "k8s_controlplane-*", kql, from_ms, to_ms, 100)
    total, docs = _hits_from_bsearch(result)

    lines = [
        f"Pod health investigation" + (f": {service}" if service else ""),
        f"Namespace filter: {namespace or '(all)'}",
        f"Space: {chosen_space}",
        f"Time range: last {range_minutes} minutes",
        f"Events found: {total}",
        "",
    ]
    for doc in docs:
        src = doc.get("_source", {})
        fields = doc.get("fields", {})
        ts = (fields.get("@timestamp", [None])[0] if fields else None) or src.get("@timestamp", "")
        pod = src.get("kubernetes.pod.name", "")
        ns = src.get("kubernetes.namespace", "")
        msg = src.get("message", "")
        lines.append(f"  [{ts}] [{ns}/{pod}] {str(msg).strip()[:200]}")

    return "\n".join(lines)


async def compare_log_volume(
    service: str,
    baseline_minutes: int = 60,
    comparison_minutes: int = 60,
    space: Optional[str] = None,
) -> str:
    """
    Compare error rates between two time windows — useful for checking impact of a deployment.
    Baseline = the period ending (baseline_minutes + comparison_minutes) ago.
    Comparison = the most recent comparison_minutes.
    """
    chosen_space = _pick_space(service, space) or "gcs"
    now = _now_ms()
    comp_to = now
    comp_from = now - comparison_minutes * 60 * 1000
    base_to = comp_from
    base_from = base_to - baseline_minutes * 60 * 1000

    kql = f'kubernetes.labels.app: "{service}"'
    error_kql = f'{kql} AND (dissect.catalina_out.level: "ERROR" OR message: "*Exception*")'

    async def search(kql_q, from_ms, to_ms):
        r = await _search_space(chosen_space, "filebeat-*-intcloud-*", kql_q, from_ms, to_ms, 0)
        total, _ = _hits_from_bsearch(r)
        return total

    (baseline_total, comp_total, baseline_errors, comp_errors) = await asyncio.gather(
        search(kql, base_from, base_to),
        search(kql, comp_from, comp_to),
        search(error_kql, base_from, base_to),
        search(error_kql, comp_from, comp_to),
    )

    def pct(errors, total):
        return f"{100 * errors / total:.1f}%" if total > 0 else "n/a"

    def delta(a, b):
        if b == 0:
            return "∞" if a > 0 else "0%"
        return f"{(a - b) / b * 100:+.1f}%"

    lines = [
        f"Log volume comparison: {service}",
        f"Space: {chosen_space}",
        "",
        f"{'':30} {'Baseline':>12} {'Recent':>12} {'Change':>10}",
        "─" * 66,
        f"{'Total log lines':30} {baseline_total:>12,} {comp_total:>12,} {delta(comp_total, baseline_total):>10}",
        f"{'Error/exception events':30} {baseline_errors:>12,} {comp_errors:>12,} {delta(comp_errors, baseline_errors):>10}",
        f"{'Error rate':30} {pct(baseline_errors, baseline_total):>12} {pct(comp_errors, comp_total):>12}",
        "",
        f"Baseline window:   last {baseline_minutes + comparison_minutes}m → {baseline_minutes + comparison_minutes - comparison_minutes}m ago",
        f"Comparison window: last {comparison_minutes}m → now",
    ]
    return "\n".join(lines)


async def search_service_logs(
    service: str,
    kql: Optional[str] = None,
    level: Optional[str] = None,
    range_minutes: int = 60,
    size: int = 100,
    space: Optional[str] = None,
    org: Optional[str] = None,
    namespace: Optional[str] = None,
    cluster: Optional[str] = None,
) -> str:
    """
    General-purpose service log search — the primary debugging entry point.
    Filters by service (kubernetes.labels.app), optionally by level, org, namespace, cluster.
    """
    from_ms, to_ms = _range_ms(range_minutes)
    chosen_space = _pick_space(service, space)

    kql_parts = [f'kubernetes.labels.app: "{service}"']
    if level:
        kql_parts.append(f'dissect.catalina_out.level: "{level.upper()}"')
    if org:
        kql_parts.append(f'dissect.catalina_out.org: "{org}"')
    if namespace:
        kql_parts.append(f'kubernetes.namespace: "{namespace}"')
    if cluster:
        kql_parts.append(f'(POD_cluster_name: "{cluster}" OR POD.cluster_name: "{cluster}")')
    if kql:
        kql_parts.append(f"({kql})")
    final_kql = " AND ".join(kql_parts)

    if chosen_space:
        result = await _search_space(chosen_space, "filebeat-*-intcloud-*", final_kql, from_ms, to_ms, size)
        results_by_space = {chosen_space: result}
    else:
        results_by_space = await _search_both_spaces("filebeat-*-intcloud-*", final_kql, from_ms, to_ms, size // 2)

    lines = [
        f"Service logs: {service}" + (f" [{level.upper()}]" if level else ""),
        f"Time range: last {range_minutes} minutes",
        f"Spaces: {', '.join(results_by_space.keys())}",
        "",
    ]

    for sp, result in results_by_space.items():
        total, docs = _hits_from_bsearch(result)
        lines.append(f"[{sp.upper()}] {total} events (showing {len(docs)})")
        for doc in docs:
            lines.append(f"  {_format_hit(doc)}")
        lines.append("")

    return "\n".join(lines)
