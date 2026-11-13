"""Database URL scheme rewrite for SQLAlchemy and LangGraph checkpoints."""

from urllib.parse import urlsplit, urlunsplit

POSTGRES_SCHEMES: tuple[str, ...] = ("postgresql", "postgresql+psycopg")
SQLALCHEMY_SCHEME = "postgresql+psycopg"
CHECKPOINT_SCHEME = "postgresql"


def _rewrite_scheme(url: str, scheme: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in POSTGRES_SCHEMES:
        raise ValueError("数据库 URL 必须是 postgresql:// 或 postgresql+psycopg://")
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def to_sqlalchemy_url(url: str) -> str:
    """转换为 SQLAlchemy 异步引擎使用的 URL。"""

    return _rewrite_scheme(url, SQLALCHEMY_SCHEME)


def to_checkpoint_url(url: str) -> str:
    """转换为 psycopg / LangGraph checkpoint 使用的 URL。"""

    return _rewrite_scheme(url, CHECKPOINT_SCHEME)
