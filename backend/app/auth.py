"""访问密码鉴权。留空 app_password 则整层关闭，本地/局域网零打扰。

设计要点：
- 凭证是 HMAC(密码)，cookie 里不出现明文密码，也没法反推密码。
- httpOnly cookie，JS 读不到，降低 XSS 窃取风险；HTTPS 下加 Secure。
- 登录失败按 IP 递增延迟 + 次数上限，挡公网暴力破解。
- 只保护 /api/*；静态前端放行（空壳，数据全在 /api 下）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

COOKIE_NAME = "peiyin_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 天


def _secret() -> bytes:
    # 用密码自身派生签名密钥：改密码即让所有旧 cookie 失效，无需另存 secret。
    return hashlib.sha256(("peiyin::" + settings.app_password).encode()).digest()


def expected_token() -> str:
    return hmac.new(_secret(), b"authorized", hashlib.sha256).hexdigest()


def token_ok(token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, expected_token())


def is_secure(request: Request) -> bool:
    # 直连 https，或反代回源 http 但带了 X-Forwarded-Proto=https
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


# ---------- 登录限流：按 IP 记失败次数，递增延迟 ----------

_fails: dict[str, list[float]] = {}
_WINDOW = 300.0  # 5 分钟窗口
_MAX_FAILS = 8   # 窗口内超过则拒绝


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def register_fail(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < _WINDOW]
    hits.append(now)
    _fails[ip] = hits
    # 失败越多睡越久：第 n 次失败延迟 min(n*0.5, 4) 秒
    await asyncio.sleep(min(len(hits) * 0.5, 4.0))


def too_many_fails(request: Request) -> bool:
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < _WINDOW]
    _fails[ip] = hits
    return len(hits) >= _MAX_FAILS


def clear_fails(request: Request) -> None:
    _fails.pop(_client_ip(request), None)


class AuthMiddleware(BaseHTTPMiddleware):
    """未登录访问 /api/* 一律 401。密码为空则直接放行。"""

    # 登录/元信息端点即使没登录也要能访问
    _open_paths = {"/api/login", "/api/auth-status"}

    async def dispatch(self, request: Request, call_next):
        if not settings.app_password:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/") or path in self._open_paths:
            return await call_next(request)

        if not token_ok(request.cookies.get(COOKIE_NAME)):
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
        return await call_next(request)
