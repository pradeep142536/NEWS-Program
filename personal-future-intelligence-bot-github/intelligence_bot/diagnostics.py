from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from .collector import load_sources, retrieve_feed
from .config import Config
from .dates import publication_date_status
from .models import FeedItem
from .net import FetchError, UnsafeURL, validate_url
from .verify import candidate_for, classify

COUNT_KEYS = (
    "parsed",
    "baseline",
    "already_seen",
    "too_old",
    "invalid_date",
    "future_date",
    "missing_publication_date",
    "low_relevance",
    "failed_verification",
    "unsupported_url",
    "duplicate",
    "would_publish",
    "would_qualify_but_baseline_withholds",
    "verification_not_sampled",
)


def _database_path(config: Config, project_root: Path) -> Path:
    return config.database if config.database.is_absolute() else project_root / config.database


def _read_history(database_path: Path) -> tuple[dict[str, tuple[str, str]], dict[str, bool]]:
    """Read story URLs and baseline flags without initializing or writing SQLite."""
    if not database_path.is_file():
        return {}, {}
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        history = {row[0]: (row[1], row[2]) for row in connection.execute("SELECT canonical_url,status,decision_reason FROM stories")}
        baselines = {row[0]: bool(row[1]) for row in connection.execute("SELECT id,channel_baselined FROM sources")}
        return history, baselines
    finally:
        connection.close()


def _verification_rejection(status: str, category: str, reasons: tuple[str, ...]) -> str:
    if status in ("unsupported_url", "rejected_untrusted_domain"):
        return "unsupported_url"
    if status == "not_fetched_irrelevant" or category == "not_relevant":
        return "low_relevance"
    if any("URL" in reason or "domain" in reason.casefold() for reason in reasons):
        return "unsupported_url"
    return "failed_verification"


async def run_dry_run(
    config: Config,
    project_root: Path,
    *,
    source_id: str | None = None,
    sample_size: int = 10,
) -> int:
    """Evaluate recent feed items without touching Telegram or writing to SQLite."""
    if sample_size < 1 or sample_size > 50:
        raise ValueError("sample_size must be between 1 and 50")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
    registry = config.registry if config.registry.is_absolute() else project_root / config.registry
    sources = load_sources(registry)
    if source_id and not any(source.id == source_id for source in sources):
        print(f"Unknown source id: {source_id}")
        return 2

    try:
        history, baselines = _read_history(_database_path(config, project_root))
    except sqlite3.Error as exc:
        print(f"Could not read the existing database safely: {exc}")
        return 2

    selected = [source for source in sources if source.enabled and source.feed_url and (not source_id or source.id == source_id)]
    if not selected:
        print("No enabled feed sources matched the request.")
        return 1

    print("DRY RUN — read-only; no Telegram calls, messages, queue changes, checkpoints, or database writes.")
    print(f"Sources: {len(selected)} | article verification sample limit per source: {sample_size}")
    failures = 0
    for source in selected:
        counts: Counter[str] = Counter()
        try:
            feed = await asyncio.to_thread(retrieve_feed, source, None, None, config.max_article_bytes)
        except Exception as exc:
            failures += 1
            print(f"\n[{source.id}] {source.publisher}: SOURCE ERROR — {type(exc).__name__}: {exc}")
            continue

        counts["parsed"] = len(feed.items)
        is_baseline = not baselines.get(source.id, False)
        print(f"\n[{source.id}] {source.publisher}: parsed {len(feed.items)} item(s); baseline={'yes' if is_baseline else 'complete'}")
        if is_baseline:
            counts["baseline"] = len(feed.items)
            print("  This source has no completed channel baseline. Existing entries will be stored as history and withheld by the first live scan.")

        seen_in_feed: set[str] = set()
        candidates: list[tuple[FeedItem, object]] = []
        samples: list[tuple[FeedItem, str]] = []
        for item in feed.items:
            if item.url in seen_in_feed:
                counts["duplicate"] += 1
                if len(samples) < sample_size:
                    samples.append((item, "duplicate URL repeated inside this feed"))
                continue
            seen_in_feed.add(item.url)
            date_status, parsed_date = publication_date_status(item.published, config.max_story_age_seconds)
            if date_status == "missing_date":
                counts["missing_publication_date"] += 1
            elif date_status != "fresh":
                counts[date_status] += 1
                if len(samples) < sample_size:
                    samples.append((item, f"rejected by date filter: {date_status}"))
                if item.url in history and not is_baseline:
                    prior_reason = history[item.url][1]
                    counts["baseline" if prior_reason == "Existing entry captured during initial baseline" else "already_seen"] += 1
                continue
            if item.url in history and not is_baseline:
                prior_reason = history[item.url][1]
                if prior_reason == "Existing entry captured during initial baseline":
                    counts["baseline"] += 1
                    label = "stored during initial baseline; already-seen historical entry"
                else:
                    counts["already_seen"] += 1
                    label = "already seen in the local story database"
                if len(samples) < sample_size:
                    samples.append((item, label))
                continue
            try:
                validate_url(item.url, source.trusted_domains)
            except (UnsafeURL, ValueError):
                counts["unsupported_url"] += 1
                if len(samples) < sample_size:
                    samples.append((item, "rejected: unsupported or untrusted article URL"))
                continue
            except FetchError as exc:
                counts["failed_verification"] += 1
                if len(samples) < sample_size:
                    samples.append((item, f"verification failed during safe host check: {exc}"))
                continue
            preliminary = classify(item, source, "")
            if not preliminary.eligible:
                counts["low_relevance"] += 1
                reason = "; ".join(preliminary.reasons) or "no relevance category matched"
                if len(samples) < sample_size:
                    samples.append((item, f"rejected by relevance filter: {reason}"))
                continue
            candidates.append((item, parsed_date))
            if len(samples) < sample_size:
                samples.append((item, f"relevant; awaiting publisher/evidence verification ({preliminary.category})"))

        candidates.sort(key=lambda pair: pair[1] or __import__("datetime").datetime.min.replace(tzinfo=__import__("datetime").timezone.utc), reverse=True)
        to_verify = candidates[:sample_size]
        counts["verification_not_sampled"] = max(0, len(candidates) - len(to_verify))
        for item, _ in to_verify:
            evidence_sources = sources if source.trust_level in ("discovery_only", "reputable_reporting") else None
            verification, classification = await asyncio.to_thread(
                candidate_for,
                item,
                source,
                (),
                config.max_article_bytes,
                evidence_sources,
            )
            if classification.eligible and is_baseline:
                counts["would_qualify_but_baseline_withholds"] += 1
                print(
                    f"  BASELINE WITHHOLD [otherwise qualifies: {classification.category}; score={classification.score}; {verification.status}] "
                    f"{item.title[:180]} | {item.published or 'no publication date'} | {verification.evidence_url}"
                )
            elif classification.eligible:
                counts["would_publish"] += 1
                print(
                    f"  WOULD PUBLISH [{classification.category}; score={classification.score}; {verification.status}] "
                    f"{item.title[:180]} | {item.published or 'no publication date'} | {verification.evidence_url}"
                )
            else:
                rejection = _verification_rejection(verification.status, classification.category, classification.reasons)
                counts[rejection] += 1
                print(
                    f"  REJECT [{rejection}; {verification.status}] {item.title[:180]} | "
                    f"{verification.reason or '; '.join(classification.reasons)}"
                )

        for item, reason in samples:
            print(f"  SAMPLE [{reason}] {item.title[:180]} | {item.published or 'no publication date'} | {item.url}")
        print("  Counts: " + ", ".join(f"{key}={counts[key]}" for key in COUNT_KEYS if counts[key] or key in ("parsed", "would_publish", "would_qualify_but_baseline_withholds")))

    return 1 if failures else 0
