from typing import Any, Optional

import httpx

from kibana_mcp.auth.manager import get_session, SessionExpiredError
from kibana_mcp.config import config

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(verify=config.kibana.tls_verify, timeout=60.0)
    return _client


async def _headers() -> dict:
    base = {
        "Content-Type": "application/json",
        "kbn-xsrf": "true",
        "kbn-version": config.kibana.kbn_version,
    }
    if config.kibana.auth_method == "api_key":
        base["Authorization"] = f"ApiKey {config.kibana.api_key}"
        return base

    session = await get_session()
    base["Cookie"] = f"{session.cookie_name}={session.cookie_value}"
    base.update(session.extra_headers)
    return base


async def kibana_get(path: str, params: Optional[dict] = None) -> Any:
    headers = await _headers()
    url = f"{config.kibana.base_url}{path}"
    resp = await _get_client().get(url, headers=headers, params=params)
    _check_response(resp, path)
    return resp.json()


async def kibana_post(path: str, body: Any) -> Any:
    headers = await _headers()
    url = f"{config.kibana.base_url}{path}"
    resp = await _get_client().post(url, headers=headers, json=body)
    _check_response(resp, path)
    return resp.json()


async def es_post(path: str, body: Any) -> Any:
    """Direct Elasticsearch API call proxied through Kibana."""
    headers = await _headers()
    url = f"{config.kibana.base_url}{path}"
    resp = await _get_client().post(url, headers=headers, json=body)
    _check_response(resp, path)
    return resp.json()


def _check_response(resp: httpx.Response, path: str) -> None:
    if resp.status_code in (401, 302, 403):
        raise SessionExpiredError()
    if not resp.is_success:
        raise RuntimeError(f"Kibana API error {resp.status_code} on {path}: {resp.text[:300]}")
