"""
API Key 认证中间件 —— 用于远程 MCP Server (SSE / Streamable HTTP) 模式。

工作原理：
  1. 从 HTTP 请求头 Authorization: Bearer <key> 中提取 token
  2. 与环境变量 MCP_API_KEYS（逗号分隔）中的合法密钥比对
  3. 通过则放行，不通过返回 401

密钥管理：
  - 在 .env 或环境变量中设置 MCP_API_KEYS=key1,key2,key3
  - 每个同事分配一个独立 key，便于审计和吊销
  - 健康检查端点 /health 不需要认证
"""

import os
import hmac
import json
import logging
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def get_valid_api_keys() -> list[str]:
    """从环境变量读取合法 API Key 列表"""
    raw = os.getenv("MCP_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys


# 不需要认证的路径白名单
PUBLIC_PATHS = {"/health", "/health/"}


class APIKeyMiddleware:
    """
    ASGI 中间件：验证 Bearer Token。

    用法：
        app = mcp.streamable_http_app()
        app.add_middleware(APIKeyMiddleware)
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")

        # 健康检查端点免认证
        if path in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        # 从 headers 中提取 Authorization
        headers = dict(scope.get("headers", []))
        auth_header = (
            headers.get(b"authorization", b"").decode("utf-8")
            or headers.get(b"Authorization", b"").decode("utf-8")
        )

        if not auth_header:
            return await self._unauthorized(scope, send, "Missing Authorization header")

        # 解析 Bearer token
        if not auth_header.startswith("Bearer "):
            return await self._unauthorized(scope, send, "Expected 'Bearer <token>' format")

        token = auth_header[7:].strip()

        # 验证 token
        valid_keys = get_valid_api_keys()
        if not valid_keys:
            logger.warning("MCP_API_KEYS not configured — rejecting all requests")
            return await self._unauthorized(scope, send, "Server has no API keys configured")

        # 使用 hmac.compare_digest 防止时序攻击
        is_valid = any(hmac.compare_digest(token, k) for k in valid_keys)
        if not is_valid:
            logger.warning(f"Invalid API key attempted: {token[:8]}...")
            return await self._unauthorized(scope, send, "Invalid API key")

        return await self.app(scope, receive, send)

    async def _unauthorized(self, scope: Scope, send: Send, detail: str):
        """返回 401 JSON 响应"""
        response = JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "detail": detail},
        )
        await response(scope=scope, receive=None, send=send)


async def health_endpoint(scope: Scope, receive: Receive, send: Send):
    """健康检查端点 —— 用于负载均衡器 / 监控探测"""
    from db import check_connection

    db_ok = check_connection()
    body = {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
    }
    response = JSONResponse(status_code=200 if db_ok else 503, content=body)
    await response(scope=scope, receive=receive, send=send)
