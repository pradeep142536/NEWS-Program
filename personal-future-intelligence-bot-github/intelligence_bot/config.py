from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load simple KEY=value entries without overriding real environment values."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Config:
    token: str
    database: Path
    registry: Path
    scan_interval: int = 60
    telegram_poll_timeout: int = 30
    max_article_bytes: int = 2_000_000
    max_image_bytes: int = 5_000_000
    log_level: str = "INFO"
    destination_channel_id: str = ""
    authorized_source_channel_ids: frozenset[int] = frozenset()
    max_story_age_seconds: int = 72 * 60 * 60


def _parse_id_set(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "").strip()
    try:
        return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated integer Telegram IDs") from exc


def from_environment(project_root: Path) -> Config:
    load_dotenv(project_root / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return Config(
        token=token,
        database=Path(os.environ.get("BOT_DATABASE", "data/intelligence.sqlite3")),
        registry=Path(os.environ.get("SOURCE_REGISTRY", "sources.json")),
        scan_interval=max(10, int(os.environ.get("SCAN_INTERVAL_SECONDS", "60"))),
        telegram_poll_timeout=min(50, max(1, int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30")))),
        max_article_bytes=max(64_000, int(os.environ.get("MAX_ARTICLE_BYTES", "2000000"))),
        max_image_bytes=max(64_000, int(os.environ.get("MAX_IMAGE_BYTES", "5000000"))),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        destination_channel_id=os.environ.get("TELEGRAM_DESTINATION_CHANNEL_ID", "").strip(),
        authorized_source_channel_ids=_parse_id_set("AUTHORIZED_TELEGRAM_SOURCE_CHANNEL_IDS"),
        max_story_age_seconds=max(3600, int(os.environ.get("MAX_STORY_AGE_HOURS", "72")) * 3600),
    )
