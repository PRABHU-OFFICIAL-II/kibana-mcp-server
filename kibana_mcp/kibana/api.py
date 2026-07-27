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


# ── Console proxy helpers (used by utility tools) ────────────────────────────

async def _console_proxy_get(es_path: str, space: Optional[str] = None) -> Dict:
    """Route an ES GET through Kibana's Dev Tools console proxy."""
    s = space if space is not None else config.kibana.space_id
    prefix = f"/s/{s}" if s else ""
    return await kibana_get(f"{prefix}/api/console/proxy", params={"path": es_path, "method": "GET"})


async def _console_proxy_post(es_path: str, body: Any, space: Optional[str] = None) -> Dict:
    """Route an ES POST through Kibana's Dev Tools console proxy."""
    s = space if space is not None else config.kibana.space_id
    prefix = f"/s/{s}" if s else ""
    return await kibana_post(f"{prefix}/api/console/proxy?path={es_path}&method=POST", body)


async def count_by_field(
    index_pattern: str,
    field: str,
    from_ms: int,
    to_ms: int,
    size: int = 20,
    service: Optional[str] = None,
    level: Optional[str] = None,
    space: Optional[str] = None,
) -> Dict:
    filters: List[Dict] = [{"range": {"@timestamp": {"gte": from_ms, "lte": to_ms, "format": "epoch_millis"}}}]
    if service:
        filters.append({"term": {"kubernetes.labels.app.keyword": service}})
    if level:
        filters.append({"term": {"dissect.catalina_out.level.keyword": level.upper()}})
    kw = field if field.endswith(".keyword") else f"{field}.keyword"
    body = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {"by_field": {"terms": {"field": kw, "size": size, "order": {"_count": "desc"}}}},
    }
    return await _console_proxy_post(f"/{index_pattern}/_search", body, space)


async def get_log_histogram(
    index_pattern: str,
    from_ms: int,
    to_ms: int,
    interval: str = "30m",
    service: Optional[str] = None,
    level: Optional[str] = None,
    kql: Optional[str] = None,
    space: Optional[str] = None,
) -> Dict:
    filters: List[Dict] = [{"range": {"@timestamp": {"gte": from_ms, "lte": to_ms, "format": "epoch_millis"}}}]
    must: List[Dict] = []
    if service:
        filters.append({"term": {"kubernetes.labels.app.keyword": service}})
    if level:
        filters.append({"term": {"dissect.catalina_out.level.keyword": level.upper()}})
    if kql:
        must.append({"query_string": {"query": kql, "default_field": "message"}})
    query: Dict = {"bool": {"filter": filters}}
    if must:
        query["bool"]["must"] = must
    body = {
        "size": 0,
        "query": query,
        "aggs": {"over_time": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval, "min_doc_count": 0}}},
    }
    return await _console_proxy_post(f"/{index_pattern}/_search", body, space)


async def get_context_around(
    index_pattern: str,
    timestamp: str,
    before: int = 20,
    after: int = 20,
    service: Optional[str] = None,
    space: Optional[str] = None,
) -> Dict:
    """Return up to `before` docs before and `after` docs after a pivot timestamp."""
    src_filters: List[Dict] = []
    if service:
        src_filters.append({"term": {"kubernetes.labels.app.keyword": service}})

    def _q(order: str, op: str, count: int) -> Dict:
        return {
            "size": count,
            "sort": [{"@timestamp": {"order": order}}],
            "query": {"bool": {"filter": [{"range": {"@timestamp": {op: timestamp}}}, *src_filters]}},
            "_source": True,
        }

    import asyncio
    before_r, after_r = await asyncio.gather(
        _console_proxy_post(f"/{index_pattern}/_search", _q("desc", "lt", before), space),
        _console_proxy_post(f"/{index_pattern}/_search", _q("asc", "gt", after), space),
    )
    before_hits = list(reversed(before_r.get("hits", {}).get("hits", [])))
    after_hits = after_r.get("hits", {}).get("hits", [])
    return {"before": before_hits, "after": after_hits, "pivot": timestamp}


async def list_fields(
    index_pattern: str,
    filter_pattern: Optional[str] = None,
    popular_only: bool = True,
    space: Optional[str] = None,
) -> Dict:
    if popular_only:
        s = space if space is not None else config.kibana.space_id
        prefix = f"/s/{s}" if s else ""
        result = await kibana_post(f"{prefix}/api/console/proxy?path=/{index_pattern}/_search&method=POST", {
            "size": 50, "sort": [{"@timestamp": {"order": "desc"}}], "_source": True,
        })
        hits = result.get("hits", {}).get("hits", [])
        fields: set = set()

        def _walk(obj: dict, pfx: str = "") -> None:
            for k, v in obj.items():
                full = f"{pfx}.{k}" if pfx else k
                fields.add(full)
                if isinstance(v, dict):
                    _walk(v, full)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            _walk(item, full)

        for hit in hits:
            _walk(hit.get("_source", {}))
        if filter_pattern:
            fields = {f for f in fields if filter_pattern.lower() in f.lower()}
        return {"fields": sorted(fields), "source": "sample"}
    else:
        result = await _console_proxy_get(f"/{index_pattern}/_field_caps?fields=*", space)
        raw = result.get("fields", {})
        if filter_pattern:
            raw = {k: v for k, v in raw.items() if filter_pattern.lower() in k.lower()}
        return {"fields": {k: list(v.keys()) for k, v in sorted(raw.items()) if not k.startswith("_")}, "source": "field_caps"}


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
