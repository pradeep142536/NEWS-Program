from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Source:
    id: str
    publisher: str
    domain: str
    trusted_domains: tuple[str, ...]
    feed_url: str | None
    access_method: str
    topics: tuple[str, ...]
    categories: tuple[str, ...]
    interval_seconds: int
    enabled: bool
    images_allowed: bool = False
    notes: str = ""
    trust_level: str = "primary"
    primary_evidence_domains: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "Source":
        return cls(
            id=str(item["id"]),
            publisher=str(item["publisher"]),
            domain=str(item["domain"]).lower(),
            trusted_domains=tuple(str(x).lower() for x in item.get("trusted_domains", [])),
            feed_url=item.get("feed_url"),
            access_method=str(item["access_method"]),
            topics=tuple(str(x) for x in item.get("topics", [])),
            categories=tuple(str(x) for x in item.get("categories", [])),
            interval_seconds=max(60, int(item.get("interval_seconds", 900))),
            enabled=bool(item.get("enabled", False)),
            images_allowed=bool(item.get("images_allowed", False)),
            notes=str(item.get("notes", "")),
            trust_level=str(item.get("trust_level", "primary")).lower(),
            primary_evidence_domains=tuple(str(x).lower() for x in item.get("primary_evidence_domains", [])),
        )


@dataclass(frozen=True)
class FeedItem:
    source_id: str
    publisher: str
    title: str
    url: str
    summary: str = ""
    published: str = ""
    image_url: str | None = None
    original_post_url: str | None = None
    source_type: str = "feed"


@dataclass(frozen=True)
class Verification:
    status: str
    evidence_url: str
    evidence_excerpt: str
    page_title: str = ""
    image_url: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class Classification:
    eligible: bool
    category: str
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    item: FeedItem
    verification: Verification
    classification: Classification


def source_to_dict(source: Source) -> dict[str, Any]:
    return asdict(source)
