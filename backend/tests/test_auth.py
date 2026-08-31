"""访问密码鉴权：中间件拦截、登录流程、限流。"""

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def with_password(monkeypatch):
    monkeypatch.setattr(settings, "app_password", "s3cret")
    auth._fails.clear()
    yield "s3cret"
    auth._fails.clear()


# ---------- 密码为空：整层关闭 ----------

def test_无密码时不拦截(client):
    # 默认 app_password="" —— API 直接可用
    assert client.get("/api/projects").status_code == 200
    st = client.get("/api/auth-status").json()
    assert st == {"auth_required": False, "logged_in": True}


# ---------- 设了密码：未登录被拦 ----------

def test_未登录访问api被拦(client, with_password):
    assert client.get("/api/projects").status_code == 401
    # 音频/导出这类拿 URL 就能下的也要挡住
    assert client.get("/api/projects/1/export").status_code == 401


def test_auth状态反映需登录(client, with_password):
    st = client.get("/api/auth-status").json()
    assert st == {"auth_required": True, "logged_in": False}


def test_静态与登录端点放行(client, with_password):
    # 登录端点本身不能被自己挡住
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/auth-status").status_code == 200


# ---------- 登录流程 ----------

def test_密码正确下发cookie后可访问(client, with_password):
    r = client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 200 and r.json()["logged_in"] is True
    assert auth.COOKIE_NAME in r.cookies
    # TestClient 会带上 cookie，后续请求放行
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth-status").json()["logged_in"] is True


def test_密码错误被拒(client, with_password):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    # 没拿到 cookie，仍被拦
    assert client.get("/api/projects").status_code == 401


def test_登出后失效(client, with_password):
    client.post("/api/login", json={"password": "s3cret"})
    assert client.get("/api/projects").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/projects").status_code == 401


def test_伪造cookie无效(client, with_password):
    client.cookies.set(auth.COOKIE_NAME, "deadbeef")
    assert client.get("/api/projects").status_code == 401


def test_改密码使旧凭证失效(client, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "old")
    auth._fails.clear()
    r = client.post("/api/login", json={"password": "old"})
    token = r.cookies[auth.COOKIE_NAME]
    # 换密码后，旧 token 不再匹配（签名密钥由密码派生）
    monkeypatch.setattr(settings, "app_password", "new")
    client.cookies.set(auth.COOKIE_NAME, token)
    assert client.get("/api/projects").status_code == 401


# ---------- 限流 ----------

def test_多次失败触发限流(client, with_password, monkeypatch):
    # 免掉递增 sleep，加快测试
    import time
    async def no_sleep(_req):
        ip = auth._client_ip(_req)
        auth._fails.setdefault(ip, []).append(time.time())
    monkeypatch.setattr(auth, "register_fail", no_sleep)

    for _ in range(auth._MAX_FAILS):
        client.post("/api/login", json={"password": "bad"})
    # 超过阈值后即使密码对也先被 429 挡
    r = client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 429
