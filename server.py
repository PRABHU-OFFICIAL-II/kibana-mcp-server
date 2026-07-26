#!/usr/bin/env python3
"""Kibana MCP Server — stdio and HTTP/SSE modes."""

import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)

from mcp.server import Server
from mcp.server.stdio import stdio_server

from kibana_mcp.auth.manager import get_session, SessionExpiredError
from kibana_mcp.config import config
from kibana_mcp.tools.index import register_tools

MODE = os.environ.get("MCP_MODE", "stdio")
PORT = int(os.environ.get("MCP_PORT", 3002))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")


async def warm_up_session() -> None:
    try:
        session = await get_session()
        expires_in = round((session.expires_at - __import__("time").time() * 1000) / 60000)
        print(f"[kibana-mcp] Session loaded, expires in {expires_in} minutes", file=sys.stderr)
    except SessionExpiredError:
        print("[kibana-mcp] No active session — call inject_session tool to authenticate", file=sys.stderr)


async def start_stdio() -> None:
    server = Server("kibana-mcp-server")
    register_tools(server)
    await warm_up_session()
    print("[kibana-mcp] Running on stdio", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def start_http() -> None:
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    import uvicorn

    server = Server("kibana-mcp-server")
    register_tools(server)
    await warm_up_session()

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    async def handle_messages(request: Request):
        await sse.handle_post_message(request.scope, request.receive, request._send)

    async def health(request: Request):
        return JSONResponse({"status": "ok", "mode": "http", "kibana": config.kibana.base_url})

    app = Starlette(routes=[
        Route("/health", health),
        Route("/sse", handle_sse),
        Mount("/messages/", app=handle_messages),
    ])

    print(f"[kibana-mcp] HTTP/SSE server on http://{HOST}:{PORT}", file=sys.stderr)
    config_uv = uvicorn.Config(app, host=HOST, port=PORT, log_level="error")
    server_uv = uvicorn.Server(config_uv)
    await server_uv.serve()


async def main() -> None:
    print(f"[kibana-mcp] Starting in {MODE} mode — targeting {config.kibana.base_url}", file=sys.stderr)
    if MODE == "http":
        await start_http()
    else:
        await start_stdio()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
