import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    """清掉环境里的代理变量。

    本机全局注入了 socks5 代理，httpx 会读 *_proxy 并要求 socksio；测试
    全部走 respx 拦截，不该经过任何代理。
    """
    for k in (
        "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY",
        "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """每个测试用独立的真实 SQLite 文件和音频目录，不 mock 数据库。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "mimo_api_key", "sk-test")
    monkeypatch.setattr(settings, "mimo_tts_model", "mimo-v2.5-tts")
    monkeypatch.setattr(settings, "tts_max_retry", 2)
    (tmp_path / "audio").mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield tmp_path


@pytest.fixture
def no_sleep(monkeypatch):
    """让退避重试不真的等。"""
    import asyncio

    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
