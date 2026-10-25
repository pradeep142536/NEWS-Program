from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .dates import parse_publication_datetime
from .models import FeedItem, Source
from .net import FetchError, fetch_url, host_matches, robots_allowed

log = logging.getLogger(__name__)


def load_sources(path: Path) -> list[Source]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = [Source.from_dict(item) for item in raw.get("sources", [])]
    ids: set[str] = set()
    for source in sources:
        if source.id in ids:
            raise ValueError(f"Duplicate source id: {source.id}")
        ids.add(source.id)
        if source.trust_level not in ("primary", "discovery_only", "reputable_reporting"):
            raise ValueError(f"Source {source.id} has unsupported trust_level {source.trust_level!r}")
        if source.enabled:
            if not source.feed_url:
                raise ValueError(f"Enabled source {source.id} needs a feed_url")
            if not host_matches(urlsplit(source.feed_url).hostname or "", source.trusted_domains):
                raise ValueError(f"Feed URL for {source.id} is outside its trusted domain list")
    primary_domains = tuple(domain for source in sources if source.trust_level == "primary" for domain in source.trusted_domains)
    for source in sources:
        if source.trust_level == "discovery_only":
            if not source.primary_evidence_domains:
                raise ValueError(f"Discovery-only source {source.id} needs primary_evidence_domains")
            for domain in source.primary_evidence_domains:
                if not host_matches(domain, primary_domains):
                    raise ValueError(f"Evidence domain {domain} for {source.id} is not in any primary source allowlist")
    return sources


def _clean_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return child.text.strip()
    return ""


def _link(node: ET.Element) -> str:
    for child in node:
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local != "link":
            continue
        href = child.attrib.get("href")
        if href and child.attrib.get("rel", "alternate") in ("alternate", "" ):
            return href
        if child.text:
            return child.text.strip()
    return ""


def parse_feed(body: bytes, source: Source) -> list[FeedItem]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed XML feed: {exc}") from exc
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]
    results: list[FeedItem] = []
    for node in nodes:
        title = _clean_text(_child_text(node, ("title",)))
        link = _link(node)
        if not title or not link:
            continue
        summary = _clean_text(_child_text(node, ("description", "summary", "encoded", "content")))
        published = _child_text(node, ("pubdate", "published", "updated", "date"))
        image_url = None
        for child in node.iter():
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name in ("content", "thumbnail", "enclosure") and child.attrib.get("url"):
                mime = child.attrib.get("type", "").lower()
                if name != "enclosure" or not mime or mime.startswith("image/"):
                    image_url = child.attrib["url"]
                    break
        results.append(FeedItem(source.id, source.publisher, title, link, summary, published, image_url))
    return results


@dataclass(frozen=True)
class FeedResult:
    items: list[FeedItem]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


def retrieve_feed(source: Source, etag: str | None = None, last_modified: str | None = None, max_bytes: int = 2_000_000) -> FeedResult:
    allowed, reason = robots_allowed(source.feed_url or "", source.trusted_domains)
    if not allowed:
        raise FetchError(reason)
    headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    response = fetch_url(source.feed_url or "", source.trusted_domains, max_bytes=max_bytes, headers=headers)
    if response.status == 304:
        return FeedResult([], etag, last_modified, True)
    if response.status < 200 or response.status >= 300:
        raise FetchError(f"Unexpected HTTP status {response.status}")
    return FeedResult(
        parse_feed(response.body, source),
        response.headers.get("etag"),
        response.headers.get("last-modified"),
    )


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.metadata: dict[str, str] = {}
        self.links: list[str] = []
        self.in_title = False
        self.title_parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "meta":
            key = (data.get("property") or data.get("name") or "").lower()
            if key and data.get("content"):
                self.metadata[key] = data["content"]
        if tag.lower() in ("a", "area") and data.get("href"):
            self.links.append(data["href"])
        if tag.lower() in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() in ("p", "br", "li", "h1", "h2", "h3", "section", "article"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript", "svg") and self.skip_depth:
            self.skip_depth -= 1
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if not self.skip_depth:
            self.parts.append(data)


@dataclass(frozen=True)
class ArticlePage:
    url: str
    title: str
    text: str
    image_url: str | None
    links: tuple[str, ...] = ()


def retrieve_article(url: str, source: Source, max_bytes: int = 2_000_000) -> ArticlePage:
    allowed, reason = robots_allowed(url, source.trusted_domains)
    if not allowed:
        raise FetchError(reason)
    response = fetch_url(url, source.trusted_domains, max_bytes=max_bytes, headers={"Accept": "text/html,application/xhtml+xml"})
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not any(token in content_type for token in ("text/html", "application/xhtml+xml")):
        raise FetchError(f"Original page is not HTML ({content_type.split(';', 1)[0]})")
    parser = _PageParser()
    try:
        parser.feed(response.body.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise FetchError(f"Could not parse original page: {exc}") from exc
    image = parser.metadata.get("og:image") or parser.metadata.get("twitter:image")
    image_url = None
    if image:
        image_host = urlsplit(image).hostname or ""
        if host_matches(image_host, source.trusted_domains):
            image_url = image
        elif host_matches(image_host, ("images-assets.nasa.gov",)) and "nasa.gov" in source.domain:
            image_url = image
    title = _clean_text(" ".join(parser.title_parts)) or parser.metadata.get("og:title", "")
    page_text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    links = tuple(dict.fromkeys(
        urljoin(response.url, link.strip())
        for link in parser.links
        if link.strip() and urlsplit(urljoin(response.url, link.strip())).scheme in ("http", "https")
    ))
    return ArticlePage(response.url, title[:300], page_text[:100_000], image_url, links[:500])


def parse_published_time(value: str):
    return parse_publication_datetime(value)


class _AlternateFeedParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        rel = set(values.get("rel", "").lower().split())
        mime = values.get("type", "").lower()
        if "alternate" in rel and ("rss" in mime or "atom" in mime):
            self.links.append((values.get("href", ""), mime))


def discover_official_feeds(source: Source, max_bytes: int = 1_000_000) -> list[tuple[str, int | None, str]]:
    """Find only publisher-declared RSS/Atom links; never auto-enable them."""
    root_url = f"https://{source.domain}/"
    allowed, reason = robots_allowed(root_url, source.trusted_domains)
    if not allowed:
        raise FetchError(f"Official site root {reason.lower()}")
    response = fetch_url(root_url, source.trusted_domains, max_bytes=max_bytes, headers={"Accept": "text/html,application/xhtml+xml"})
    parser = _AlternateFeedParser()
    parser.feed(response.body.decode("utf-8", errors="replace"))
    results: list[tuple[str, int | None, str]] = []
    seen: set[str] = set()
    for href, mime in parser.links:
        candidate_url = urljoin(response.url, href)
        host = urlsplit(candidate_url).hostname or ""
        if not host_matches(host, source.trusted_domains) or candidate_url in seen:
            continue
        seen.add(candidate_url)
        candidate_source = replace(source, feed_url=candidate_url, enabled=True)
        try:
            feed = retrieve_feed(candidate_source, max_bytes=max_bytes)
            results.append((candidate_url, len(feed.items), f"declared {mime}; RSS/Atom parsed"))
        except Exception as exc:
            results.append((candidate_url, None, f"declared {mime}; validation failed: {type(exc).__name__}: {exc}"))
    return results
