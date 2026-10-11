import os
from pathlib import Path
from typing import Optional, Dict

from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url


_ENV_LOADED = False


def load_dotenv(project_root: Optional[Path] = None) -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    root = project_root or Path(__file__).resolve().parents[1]
    dotenv_path = root / ".env"
    if dotenv_path.exists():
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

    _ENV_LOADED = True


def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"缺少环境变量: {key}")
    return value


def get_target_dsn() -> str:
    load_dotenv()
    return get_required_env("SR_TARGET_DSN")


def get_source_dsn() -> str:
    load_dotenv()
    return get_required_env("SR_SOURCE_DSN")


def get_target_engine(**kwargs):
    dsn = get_target_dsn()
    default_kwargs = {"pool_pre_ping": True}
    default_kwargs.update(kwargs)
    return create_engine(dsn, **default_kwargs)


def get_source_engine(**kwargs):
    dsn = get_source_dsn()
    default_kwargs = {"pool_pre_ping": True}
    default_kwargs.update(kwargs)
    return create_engine(dsn, **default_kwargs)


def get_stream_load_config() -> Dict[str, str]:
    load_dotenv()

    target_dsn = get_target_dsn()
    url = make_url(target_dsn)

    database = url.database or os.getenv("SR_TARGET_DB", "fintech_stat")
    host = os.getenv("SR_HTTP_HOST") or (url.host or "127.0.0.1")

    # StarRocks MySQL 协议常用 9030，对应 HTTP stream load 常用 8030
    default_http_port = "8030" if str(url.port or "") == "9030" else str(url.port or "8030")
    http_port = os.getenv("SR_HTTP_PORT", default_http_port)

    username = os.getenv("SR_HTTP_USER") or (url.username or "")
    password = os.getenv("SR_HTTP_PASSWORD") or (url.password or "")

    if not username or not password:
        raise RuntimeError("缺少 Stream Load 账号信息，请设置 SR_HTTP_USER/SR_HTTP_PASSWORD 或在 SR_TARGET_DSN 中提供")

    return {
        "database": database,
        "host": host,
        "port": str(http_port),
        "username": username,
        "password": password,
    }
