"""
Kibana & Elasticsearch API calls — paths verified from HAR (Kibana 8.19.13).

All paths use config.kibana.api_base() for space-awareness (/s/<space>/api vs /api).
Search goes through /internal/bsearch (Kibana's internal ES proxy).
Data views/dashboards use the content_management/rpc API (Kibana 8.7+).
"""
from typing import Any, Dict, List, Optional

from kibana_mcp.config import config
from kibana_mcp.kibana.client import kibana_get, kibana_post


def _api(path: str) -> str:
    return f"{config.kibana.api_base()}{path}"


def _space_prefix() -> str:
    space = config.kibana.space_id
    return f"/s/{space}" if space else ""


# ── Spaces ────────────────────────────────────────────────────────────────────

async def list_spaces() -> List[Dict]:
    return await kibana_get("/api/spaces/space")


# ── Saved Objects / Dashboards ────────────────────────────────────────────────

async def list_dashboards(space_id: Optional[str] = None) -> List[Dict]:
    sid = space_id or config.kibana.space_id
    base = f"/s/{sid}/api" if sid else "/api"
    result = await kibana_post(f"{base}/content_management/rpc/search", {
        "contentTypeId": "dashboard",
        "query": {"limit": 200},
        "options": {"fields": ["title", "description"]},
        "version": 1,
    })
    return result.get("result", {}).get("result", {}).get("hits", [])


async def get_dashboard(dashboard_id: str) -> Dict:
    return await kibana_get(_api(f"/saved_objects/dashboard/{dashboard_id}"))


# ── Data Views (Index Patterns) ───────────────────────────────────────────────

async def list_data_views() -> List[Dict]:
    result = await kibana_post(_api("/content_management/rpc/search"), {
        "contentTypeId": "index-pattern",
        "query": {"limit": 10000},
        "options": {"fields": ["title", "type", "typeMeta", "name"]},
        "version": 1,
    })
    return result.get("result", {}).get("result", {}).get("hits", [])


async def get_data_view(data_view_id: str) -> Dict:
    result = await kibana_post(_api("/content_management/rpc/get"), {
        "contentTypeId": "index-pattern",
        "id": data_view_id,
        "version": 1,
    })
    return result.get("result", {}).get("result", {}).get("item", {})


# ── Search (via Kibana bsearch internal proxy) ────────────────────────────────

async def _bsearch_poll(space: str, search_id: str) -> Dict:
    """Poll a running bsearch async search until complete."""
    import asyncio
    path = f"/s/{space}/internal/bsearch?compress=false" if space else "/internal/bsearch?compress=false"
    for _ in range(60):  # max 30s of polling
        await asyncio.sleep(0.5)
        poll_body = {"batch": [{"request": {"id": search_id}}]}
        result = await kibana_post(path, poll_body)
        item = result[0] if isinstance(result, list) and result else result
        inner = item.get("result", item)
        if not inner.get("isRunning", False):
            return inner.get("rawResponse", inner)
    # Return whatever we have after timeout
    return {}


async def search_logs(
    index_pattern: str,
    kql: str,
    from_ms: int,
    to_ms: int,
    size: int = 100,
    sort_order: str = "desc",
) -> Dict:
    """
    Search logs via Kibana's internal bsearch endpoint.
    Handles async search: polls until isRunning=false.
    """
    space = config.kibana.space_id
    path = f"/s/{space}/internal/bsearch?compress=false" if space else "/internal/bsearch?compress=false"

    body = {
        "batch": [{
            "request": {
                "params": {
                    "index": index_pattern,
                    "body": {
                        "query": {
                            "bool": {
                                "must": [
                                    {"query_string": {"query": kql, "analyze_wildcard": True}},
                                    {"range": {"@timestamp": {
                                        "gte": from_ms, "lte": to_ms, "format": "epoch_millis"
                                    }}},
                                ]
                            }
                        },
                        "sort": [
                            {"@timestamp": {"order": sort_order, "unmapped_type": "boolean"}},
                        ],
                        "size": size,
                    }
                }
            }
        }]
    }
    result = await kibana_post(path, body)
    item = result[0] if isinstance(result, list) and result else result
    inner = item.get("result", item)

    # If async search is still running, poll for completion
    if inner.get("isRunning", False):
        search_id = inner.get("id")
        if search_id:
            return await _bsearch_poll(space, search_id)
        # No id — fall through and return whatever partial result we have

    return inner.get("rawResponse", inner)


async def run_esql(query: str) -> Dict:
    """Run an ES|QL query (Elasticsearch 8.11+)."""
    space = config.kibana.space_id
    path = f"/s/{space}/internal/bsearch?compress=false" if space else "/internal/bsearch?compress=false"
    body = {
        "batch": [{
            "request": {
                "params": {
                    "body": {"query": query},
                    "strategy": "esql",
                }
            }
        }]
    }
    result = await kibana_post(path, body)
    item = result[0] if isinstance(result, list) and result else result
    inner = item.get("result", item)
    if inner.get("isRunning", False):
        search_id = inner.get("id")
        if search_id:
            return await _bsearch_poll(space, search_id)
    return inner.get("rawResponse", inner)


# ── Alerts ────────────────────────────────────────────────────────────────────

async def get_alert_rules(page: int = 1, per_page: int = 50) -> Dict:
    return await kibana_get(_api("/alerting/rules/_find"), params={
        "page": page,
        "per_page": per_page,
    })


async def get_active_alerts(rule_id: str) -> Dict:
    return await kibana_get(_api(f"/alerting/rule/{rule_id}/state"))


# ── APM ───────────────────────────────────────────────────────────────────────

async def get_apm_services(environment: str = "ENVIRONMENT_ALL", from_ms: int = None, to_ms: int = None) -> Dict:
    params = {"environment": environment}
    if from_ms:
        params["start"] = from_ms
    if to_ms:
        params["end"] = to_ms
    # /internal/apm is the confirmed path in Kibana 8.x (not /api/apm)
    space = config.kibana.space_id
    prefix = f"/s/{space}" if space else ""
    return await kibana_get(f"{prefix}/internal/apm/services", params=params)


async def get_apm_service_stats(service_name: str, environment: str = "ENVIRONMENT_ALL") -> Dict:
    space = config.kibana.space_id
    prefix = f"/s/{space}" if space else ""
    return await kibana_get(f"{prefix}/internal/apm/services/{service_name}/stats", params={
        "environment": environment,
    })


# ── ML / Anomaly Detection ────────────────────────────────────────────────────

async def list_ml_jobs() -> Dict:
    return await kibana_get(_api("/ml/anomaly_detectors"))


async def get_ml_job_results(job_id: str, from_ms: int, to_ms: int) -> Dict:
    body = {
        "query": {
            "bool": {
                "filter": [{"range": {"timestamp": {"gte": from_ms, "lte": to_ms}}}]
            }
        }
    }
    # ML results are in system indices — use bsearch
    space = config.kibana.space_id
    path = f"/s/{space}/internal/bsearch?compress=false" if space else "/internal/bsearch?compress=false"
    result = await kibana_post(path, {
        "batch": [{"request": {"params": {"index": f".ml-anomalies-{job_id}", "body": body}}}]
    })
    item = result[0] if isinstance(result, list) and result else result
    inner = item.get("result", item)
    if inner.get("isRunning", False):
        search_id = inner.get("id")
        if search_id:
            return await _bsearch_poll(space, search_id)
    return inner.get("rawResponse", inner)
