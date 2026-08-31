from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_tts_model: str = "mimo-v2.5-tts"

    llm_base_url: str = "https://api.xiaomimimo.com/v1"
    llm_api_key: str = ""
    llm_model: str = "mimo-v2.5-pro"

    tts_concurrency: int = 4
    tts_max_chars: int = 300
    tts_max_retry: int = 3
    port: int = 8756

    # 访问密码：留空=不鉴权（本地/局域网直接用）；设值=所有 /api 请求需登录。
    # 对外域名务必同时走 HTTPS，否则密码与凭证在公网明文传输。
    app_password: str = ""

    # 预处理分块：LLM 输出有 token 上限（实测 4096 截断），长稿必须切块送
    llm_chunk_chars: int = 400
    llm_concurrency: int = 4
    llm_disable_thinking: bool = True

    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"


settings = Settings()
