from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
import re
import signal
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .collector import discover_official_feeds, load_sources, retrieve_feed
from .config import Config
from .db import Database
from .dates import publication_date_is_fresh, publication_date_status
from .models import Candidate, Classification, FeedItem, Source, Verification
from .net import FetchError, UnsafeURL, fetch_url, host_matches, robots_allowed, validate_url
from .telegram_api import TelegramClient, TelegramError
from .verify import candidate_for, classify, format_alert

log = logging.getLogger(__name__)
SCAN_DIAGNOSTIC_KEYS = (
    "parsed", "baseline", "not_modified", "already_seen", "too_old", "invalid_date",
    "future_date", "missing_publication_date", "low_relevance", "verification_failed",
    "unsupported_url", "duplicate", "eligible_new", "queued",
)


def _log_scan_diagnostics(source_id: str, counts: Counter[str]) -> None:
    details = ", ".join(f"{key}={counts[key]}" for key in SCAN_DIAGNOSTIC_KEYS)
    log.info("Source %s scan diagnostics: %s", source_id, details)


class BotService:
    def __init__(self, config: Config, project_root: Path):
        self.config = config
        self.project_root = project_root
        db_path = config.database if config.database.is_absolute() or str(config.database) == ":memory:" else project_root / config.database
        registry = config.registry if config.registry.is_absolute() else project_root / config.registry
        self.db = Database(db_path)
        self.sources = load_sources(registry)
        self.db.sync_sources(self.sources)
        if self.db.get_metadata("monitoring_enabled") is None:
            self.db.set_monitoring_enabled(False)
        self.telegram = TelegramClient(config.token)
        self.stop_event = asyncio.Event()
        self.scan_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.scan_wakeup = asyncio.Event()
        self.delivery_wakeup = asyncio.Event()
        self.telegram_wakeup = asyncio.Event()
        self.destination_channel_id = int(config.destination_channel_id) if config.destination_channel_id.lstrip("-").isdigit() else None
        self.stop_signal_path = project_root / "data" / "stop.signal"
        self.pid_path = project_root / "data" / "publisher.pid"

    def is_fresh(self, published: str) -> bool:
        return publication_date_is_fresh(published, self.config.max_story_age_seconds)

    async def scan_now(self, force: bool = False) -> str:
        async with self.scan_lock:
            if not self.db.monitoring_enabled():
                return "Monitoring is OFF."
            if self.destination_channel_id is None:
                log.error("Monitoring enabled without a verified channel destination")
                return "No verified destination channel is configured."
            due = []
            now = datetime.now(timezone.utc)
            for source in self.sources:
                if not source.enabled or not source.feed_url:
                    continue
                state = self.db.source_state(source.id)
                last_checked = state["last_checked"]
                if last_checked and not force:
                    try:
                        age = (now - datetime.fromisoformat(last_checked)).total_seconds()
                    except ValueError:
                        age = source.interval_seconds
                    if age < source.interval_seconds:
                        continue
                due.append(source)
            if not due:
                return "No feeds were due for polling; all recent feed checks are within their configured interval."
            results = await asyncio.gather(*(self._scan_source(source) for source in due), return_exceptions=True)
            new_items = queued = baseline = errors = 0
            for source, result in zip(due, results):
                if isinstance(result, Exception):
                    errors += 1
                    log.error("Source scan failed (%s): %s", source.id, result)
                else:
                    new_items += result[0]
                    queued += result[1]
                    baseline += result[2]
            if queued:
                self.delivery_wakeup.set()
            return f"Checked {len(due)} source(s): {new_items} new eligible item(s), {queued} channel publication(s) queued, {baseline} baseline item(s), {errors} source error(s)."

    async def _scan_source(self, source: Source) -> tuple[int, int, int]:
        if not self.db.monitoring_enabled():
            return (0, 0, 0)
        state = self.db.source_state(source.id)
        # The channel has a separate first-run baseline. Legacy subscriber-era
        # feed initialization must never cause existing items to be published.
        baseline = not bool(state["channel_baselined"])
        self.db.source_checked(source.id)
        last_error = None
        feed = None
        for attempt in range(3):
            try:
                feed = await asyncio.to_thread(
                    retrieve_feed,
                    source,
                    None if baseline else state["etag"],
                    None if baseline else state["last_modified"],
                    self.config.max_article_bytes,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        if feed is None:
            self.db.source_failed(source.id, f"{type(last_error).__name__}: {last_error}")
            raise last_error or RuntimeError("feed retrieval failed")
        if not self.db.monitoring_enabled():
            return (0, 0, 0)
        if feed.not_modified:
            self.db.source_succeeded(source.id, feed.etag, feed.last_modified)
            counts: Counter[str] = Counter({"not_modified": 1})
            _log_scan_diagnostics(source.id, counts)
            return (0, 0, 0)

        counts: Counter[str] = Counter({"parsed": len(feed.items)})
        if baseline:
            baseline_count = 0
            seen_urls: set[str] = set()
            for item in feed.items:
                if not self.db.monitoring_enabled():
                    return (0, 0, baseline_count)
                if item.url in seen_urls:
                    counts["duplicate"] += 1
                    continue
                seen_urls.add(item.url)
                if not item.published:
                    counts["missing_publication_date"] += 1
                classification = classify(item, source, "")
                candidate = Candidate(
                    item,
                    Verification("baseline_only", item.url, "", reason="Existing feed entry stored as history; it was not published"),
                    Classification(False, classification.category, 0, ("Existing entry captured during initial baseline",)),
                )
                self.db.save_candidate(candidate, baseline=True)
                baseline_count += 1
            counts["baseline"] = baseline_count
            self.db.source_succeeded(source.id, feed.etag, feed.last_modified)
            self.db.source_channel_baseline_completed(source.id)
            _log_scan_diagnostics(source.id, counts)
            log.info("Source %s: baseline stored %d feed item(s); none published", source.id, baseline_count)
            return (0, 0, baseline_count)

        new_items = queued = 0
        seen_urls: set[str] = set()
        for item in feed.items:
            if not self.db.monitoring_enabled():
                break
            if item.url in seen_urls:
                counts["duplicate"] += 1
                continue
            seen_urls.add(item.url)
            date_status, _ = publication_date_status(item.published, self.config.max_story_age_seconds)
            if date_status == "missing_date":
                counts["missing_publication_date"] += 1
                # The feed item may still be new. Its date is unknown, so only
                # current source observation plus publisher verification can admit it.
            if date_status not in ("fresh", "missing_date"):
                counts[date_status] += 1
                verification = Verification("stale", item.url, "", reason=f"Publication date rejected by the {date_status} filter")
                classification = Classification(False, "excluded", 0, (f"Story rejected by publication-date filter: {date_status}",))
            else:
                try:
                    validate_url(item.url, source.trusted_domains)
                except (UnsafeURL, ValueError) as exc:
                    counts["unsupported_url"] += 1
                    verification = Verification("unsupported_url", item.url, "", reason=str(exc))
                    classification = Classification(False, "excluded", 0, ("Article URL is outside the configured HTTPS source allowlist",))
                except FetchError as exc:
                    verification = Verification("insufficient_evidence", item.url, "", reason=str(exc))
                    classification = Classification(False, "excluded", 0, ("Article URL host could not be checked safely",))
                else:
                    if source.trust_level in ("discovery_only", "reputable_reporting"):
                        verification, classification = await asyncio.to_thread(
                            candidate_for, item, source, (), self.config.max_article_bytes, self.sources
                        )
                    else:
                        verification, classification = await asyncio.to_thread(
                            candidate_for, item, source, (), self.config.max_article_bytes
                        )
            if not self.db.monitoring_enabled():
                break
            was_baselined = self.db.story_was_baselined(item.url)
            candidate = Candidate(item, verification, classification)
            story_id, is_new, changed = self.db.save_candidate(candidate)
            if not is_new:
                counts["baseline" if was_baselined else "already_seen"] += 1
            if is_new and classification.eligible:
                new_items += 1
                counts["eligible_new"] += 1
                if self.destination_channel_id is not None:
                    just_queued = self.db.queue_publication(story_id, self.destination_channel_id)
                    queued += just_queued
                    counts["queued"] += just_queued
            elif changed and self.db.previously_published(story_id) and self.destination_channel_id is not None:
                if classification.eligible or verification.status in ("expired_or_cancelled", "official_announcement_details_unresolved", "contradicted_by_primary_page"):
                    version = self.db.story_version(story_id)
                    just_queued = self.db.queue_publication(story_id, self.destination_channel_id, f"correction_v{version}")
                    queued += just_queued
                    counts["queued"] += just_queued
            if not classification.eligible and verification.status != "stale" and verification.status != "unsupported_url":
                if verification.status == "not_fetched_irrelevant" or classification.category in ("not_relevant", "excluded"):
                    counts["low_relevance"] += 1
                elif verification.status in ("rejected_untrusted_domain",):
                    counts["unsupported_url"] += 1
                else:
                    counts["verification_failed"] += 1

        self.db.source_succeeded(source.id, feed.etag, feed.last_modified)
        _log_scan_diagnostics(source.id, counts)
        log.info("Source %s: parsed %d item(s); %d eligible new item(s); %d queued", source.id, len(feed.items), new_items, queued)
        return new_items, queued, 0

    async def _source_for_channel_link(self, url: str) -> Source | None:
        host = (urlsplit(url).hostname or "").lower()
        return next((source for source in self.sources if host_matches(host, source.trusted_domains)), None)

    async def _handle_channel_post(self, post: dict) -> None:
        if not self.db.monitoring_enabled() or not self.config.authorized_source_channel_ids:
            return
        body = str(post.get("text") or post.get("caption") or "").strip()
        chat = post.get("chat") or {}
        message_id = post.get("message_id")
        if chat.get("id") is None:
            return
        channel_id = int(chat["id"])
        if channel_id not in self.config.authorized_source_channel_ids or channel_id == self.destination_channel_id:
            return
        self.db.record_telegram_channel(channel_id, chat.get("username"), str(chat.get("title", "")))
        posted_at = post.get("date")
        if posted_at:
            try:
                post_age = datetime.now(timezone.utc).timestamp() - int(posted_at)
                if post_age > self.config.max_story_age_seconds or post_age < -300:
                    return
            except (TypeError, ValueError):
                return
        if not body or not message_id:
            return
        links: list[str] = re.findall(r"https://[^\s<>\]\[()]+", body)
        for entity in (post.get("entities") or []) + (post.get("caption_entities") or []):
            if entity.get("type") == "text_link" and entity.get("url"):
                links.append(entity["url"])
        if not chat.get("username"):
            # Telegram's t.me/c link is not a permission bypass; it only preserves attribution for members.
            raw_id = str(chat.get("id", ""))
            channel_part = raw_id[4:] if raw_id.startswith("-100") else raw_id.lstrip("-")
            post_url = f"https://t.me/c/{channel_part}/{message_id}"
        else:
            post_url = f"https://t.me/{chat['username']}/{message_id}"
        seen: set[str] = set()
        for url in links:
            url = url.rstrip(".,;!?)\"' ")
            if url in seen:
                continue
            seen.add(url)
            source = await self._source_for_channel_link(url)
            if not source:
                continue
            item = FeedItem(source.id, source.publisher, body[:300], url, body[:800], str(post.get("date", "")), original_post_url=post_url, source_type="telegram_channel")
            try:
                if source.trust_level in ("discovery_only", "reputable_reporting"):
                    verification, classification = await asyncio.to_thread(candidate_for, item, source, (), self.config.max_article_bytes, self.sources)
                else:
                    verification, classification = await asyncio.to_thread(candidate_for, item, source, (), self.config.max_article_bytes)
            except Exception as exc:
                log.warning("Could not verify channel post at %s: %s", source.domain, exc)
                return
            if not classification.eligible:
                log.info("Channel post did not pass verification/relevance (%s): %s", source.domain, classification.reasons)
                return
            if source.trust_level == "discovery_only" and verification.claims.get("discovery_url"):
                item = replace(
                    item,
                    title=verification.page_title or item.title,
                    url=verification.evidence_url,
                    summary=verification.evidence_excerpt,
                    original_post_url=url,
                    source_type="cross_verified_discovery",
                )
            if self.db.story_id_for_url(item.url) is not None:
                log.info("Channel post duplicates a previously collected primary-source URL")
                return
            story_id, is_new, changed = self.db.save_candidate(Candidate(item, verification, classification))
            if self.destination_channel_id is not None and (is_new or changed and self.db.previously_published(story_id)):
                notice = f"correction_v{self.db.story_version(story_id)}" if changed else "new"
                queued = self.db.queue_publication(story_id, self.destination_channel_id, notice)
                if queued:
                    self.delivery_wakeup.set()
                log.info("Accepted verified authorized-channel post; queued %d publication(s)", queued)
            return
        log.info("Ignored channel post: no configured trusted primary-source link")

    async def _handle_update(self, update: dict) -> None:
        post = update.get("channel_post")
        if post:
            await self._handle_channel_post(post)

    async def poll_telegram(self) -> None:
        offset_text = self.db.get_metadata("telegram_update_offset")
        offset = int(offset_text) if offset_text else None
        backoff = 1
        while not self.stop_event.is_set():
            if not self.db.monitoring_enabled():
                self.telegram_wakeup.clear()
                try:
                    await asyncio.wait_for(self.telegram_wakeup.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                updates = await asyncio.to_thread(self.telegram.get_updates, offset, self.config.telegram_poll_timeout)
                backoff = 1
                for update in updates:
                    if not self.db.monitoring_enabled():
                        break
                    await self._handle_update(update)
                    update_id = int(update.get("update_id", 0))
                    offset = update_id + 1
                    self.db.set_metadata("telegram_update_offset", str(offset))
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                log.error("Telegram polling error: %s", exc)
                if exc.error_code == 409:
                    log.error("A second getUpdates worker uses this token; stopping to prevent a retry loop")
                    self.stop_event.set()
                    return
                await asyncio.sleep(min(60, exc.retry_after or backoff))
                backoff = min(60, backoff * 2)
            except Exception:
                log.exception("Update processing failed")
                await asyncio.sleep(2)

    async def _download_image(self, image_url: str, source: Source) -> tuple[bytes, str] | None:
        try:
            allowed, reason = await asyncio.to_thread(robots_allowed, image_url, source.trusted_domains)
            if not allowed:
                log.info("Skipping image: %s", reason)
                return None
            response = await asyncio.to_thread(
                fetch_url,
                image_url,
                source.trusted_domains,
                max_bytes=self.config.max_image_bytes,
                headers={"Accept": "image/jpeg,image/png,image/webp,image/gif"},
            )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not content_type.startswith("image/"):
                return None
            signatures = {
                "image/jpeg": (b"\xff\xd8\xff", "image.jpg"),
                "image/png": (b"\x89PNG\r\n\x1a\n", "image.png"),
                "image/gif": (b"GIF8", "image.gif"),
                "image/webp": (b"RIFF", "image.webp"),
            }
            expected = signatures.get(content_type)
            if not expected or not response.body.startswith(expected[0]):
                return None
            if content_type == "image/webp" and response.body[8:12] != b"WEBP":
                return None
            return response.body, expected[1]
        except (FetchError, UnsafeURL, ValueError) as exc:
            log.info("Skipping unavailable image: %s", exc)
            return None

    async def deliver_pending(self) -> int:
        async with self.delivery_lock:
            if not self.db.monitoring_enabled() or self.destination_channel_id is None:
                return 0
            return await self._deliver_publications()

    async def _deliver_publications(self) -> int:
        rows = self.db.due_publications()
        sent = 0
        for row in rows:
            if self.stop_event.is_set() or not self.db.monitoring_enabled():
                break
            if not self.db.claim_publication(row["id"]):
                continue
            if not self.db.monitoring_enabled():
                self.db.publication_failed(row["id"], "Monitoring was disabled before publication", 0)
                break
            source = Source.from_dict(json.loads(row["config_json"]))
            evidence = json.loads(row["evidence_json"])
            verification = Verification(
                status=evidence.get("status", "insufficient_evidence"),
                evidence_url=evidence.get("url", row["canonical_url"] if "canonical_url" in row.keys() else ""),
                evidence_excerpt=evidence.get("excerpt", ""),
                page_title=evidence.get("page_title", ""),
                image_url=row["image_url"],
                claims=json.loads(row["claims_json"]),
                reason=evidence.get("reason", ""),
            )
            item = FeedItem(
                source_id=row["source_id"], publisher=source.publisher, title=row["title"],
                url=row["canonical_url"], summary=row["summary"], published=row["published"],
                image_url=row["image_url"], original_post_url=row["original_post_url"],
            )
            body = format_alert(item, source, verification, row["category"], row["usefulness_score"], row["notice_kind"])
            try:
                result = None
                if row["image_url"] and source.images_allowed and len(body) <= 1000:
                    image = await self._download_image(row["image_url"], source)
                    if image:
                        result = await asyncio.to_thread(self.telegram.send_photo, row["channel_id"], image[0], image[1], body)
                if result is None:
                    result = await asyncio.to_thread(self.telegram.send_message, row["channel_id"], body)
                self.db.publication_sent(row["id"], result.get("message_id"))
                sent += 1
                await asyncio.sleep(3.1)
            except TelegramError as exc:
                attempts = int(row["attempts"]) + 1
                retry_delay = exc.retry_after or min(3600, 2 ** min(attempts, 11))
                self.db.publication_failed(row["id"], str(exc), retry_delay, permanent=exc.permanent)
                log.error("Publication failed for queue row %s (attempt %s): %s", row["id"], attempts, exc)
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                self.db.publication_failed(row["id"], f"{type(exc).__name__}: {exc}", min(3600, 2 ** min(attempts, 11)))
                log.exception("Publication failed for queue row %s", row["id"])
        return sent

    async def delivery_loop(self) -> None:
        while not self.stop_event.is_set():
            self.delivery_wakeup.clear()
            try:
                if self.db.monitoring_enabled():
                    await self.deliver_pending()
            except Exception:
                log.exception("Queue worker error")
            try:
                await asyncio.wait_for(self.delivery_wakeup.wait(), timeout=2 if self.db.monitoring_enabled() else 30)
            except asyncio.TimeoutError:
                pass

    async def scan_loop(self) -> None:
        first_scan = True
        while not self.stop_event.is_set():
            self.scan_wakeup.clear()
            try:
                if self.db.monitoring_enabled():
                    await self.scan_now(force=first_scan)
                    first_scan = False
                await asyncio.wait_for(self.scan_wakeup.wait(), timeout=self.config.scan_interval)
            except asyncio.TimeoutError:
                first_scan = False

    async def set_monitoring(self, enabled: bool) -> None:
        self.db.set_monitoring_enabled(enabled)
        self.scan_wakeup.set()
        self.delivery_wakeup.set()
        self.telegram_wakeup.set()
        if not enabled:
            async with self.scan_lock:
                pass
            async with self.delivery_lock:
                pass

    async def external_control_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.stop_signal_path.exists():
                await self.set_monitoring(False)
                self.stop_event.set()
                self.scan_wakeup.set()
                self.delivery_wakeup.set()
                self.telegram_wakeup.set()
                return
            await asyncio.sleep(0.5)

    async def verify_destination(self) -> None:
        if not self.config.destination_channel_id:
            raise RuntimeError("Set TELEGRAM_DESTINATION_CHANNEL_ID to your Telegram channel ID or @username.")
        me = await asyncio.to_thread(self.telegram.get_me)
        channel = await asyncio.to_thread(self.telegram.get_chat, self.config.destination_channel_id)
        if channel.get("type") != "channel":
            raise RuntimeError("TELEGRAM_DESTINATION_CHANNEL_ID must point to a Telegram channel.")
        channel_id = int(channel["id"])
        membership = await asyncio.to_thread(self.telegram.get_chat_member, channel_id, int(me["id"]))
        status = membership.get("status")
        can_post = status == "creator" or status == "administrator" and membership.get("can_post_messages") is True
        if not can_post:
            raise RuntimeError("Add this bot as a channel administrator with permission to post messages.")
        self.destination_channel_id = channel_id
        self.db.set_metadata("destination_channel_id", str(channel_id))
        log.info("Destination channel verified; the bot can publish there")

    async def run(self) -> None:
        webhook = await asyncio.to_thread(self.telegram.get_webhook_info)
        if webhook.get("url"):
            raise RuntimeError("This bot token has a webhook configured. Stop the webhook worker and deliberately switch this dedicated token to polling before starting this service; no webhook settings were changed.")
        await self.verify_destination()
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(str(__import__("os").getpid()), encoding="ascii")
        log.info("Channel publisher ready; monitoring=%s", "ON" if self.db.monitoring_enabled() else "OFF")
        tasks = [
            asyncio.create_task(self.poll_telegram(), name="telegram-poll"),
            asyncio.create_task(self.scan_loop(), name="source-scan"),
            asyncio.create_task(self.delivery_loop(), name="publication-queue"),
            asyncio.create_task(self.external_control_loop(), name="external-control"),
        ]
        try:
            await self.stop_event.wait()
        finally:
            self.stop_event.set()
            self.scan_wakeup.set()
            self.delivery_wakeup.set()
            self.telegram_wakeup.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.db.close()
            self.pid_path.unlink(missing_ok=True)
            self.stop_signal_path.unlink(missing_ok=True)


async def check_sources(config: Config, project_root: Path) -> int:
    registry = config.registry if config.registry.is_absolute() else project_root / config.registry
    sources = load_sources(registry)
    db_path = config.database if config.database.is_absolute() or str(config.database) == ":memory:" else project_root / config.database
    db = Database(db_path)
    db.sync_sources(sources)
    failed = 0
    for source in sources:
        if not source.enabled or not source.feed_url:
            print(f"SKIP  {source.id}: no enabled verified feed")
            continue
        try:
            result = await asyncio.to_thread(retrieve_feed, source, max_bytes=config.max_article_bytes)
            print(f"OK    {source.id}: HTTP feed parsed; {len(result.items)} item(s)")
            db.source_diagnostic(source.id, "healthy")
            representative = next((item for item in result.items if classify(item, source, "").eligible), None)
            if representative:
                if source.trust_level in ("discovery_only", "reputable_reporting"):
                    verification, _ = await asyncio.to_thread(candidate_for, representative, source, (), config.max_article_bytes, sources)
                else:
                    verification, _ = await asyncio.to_thread(candidate_for, representative, source, (), config.max_article_bytes)
                print(f"PAGE  {source.id}: {verification.status}" + (f" — {verification.reason}" if verification.reason else ""))
            else:
                print(f"PAGE  {source.id}: no feed item matched the relevance rules before page retrieval")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {source.id}: {type(exc).__name__}: {exc}")
            db.source_diagnostic(source.id, "error", f"{type(exc).__name__}: {exc}")
    db.close()
    return 1 if failed else 0


async def discover_sources(config: Config, project_root: Path) -> int:
    registry = config.registry if config.registry.is_absolute() else project_root / config.registry
    sources = load_sources(registry)
    failed = 0
    for source in sources:
        if source.enabled or source.feed_url:
            continue
        try:
            candidates = await asyncio.to_thread(discover_official_feeds, source, config.max_article_bytes)
            if not candidates:
                print(f"NONE  {source.id}: official homepage exposed no RSS/Atom alternate link")
            for url, count, detail in candidates:
                state = "VALID" if count is not None else "FAIL"
                print(f"{state}  {source.id}: {url} ({detail})" + (f"; {count} item(s)" if count is not None else ""))
                failed += count is None
        except Exception as exc:
            failed += 1
            print(f"FAIL  {source.id}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


async def check_telegram(config: Config) -> int:
    if not config.token:
        print("TELEGRAM_BOT_TOKEN is not configured; no request made.")
        return 2
    try:
        me = await asyncio.to_thread(TelegramClient(config.token).get_me)
        print(f"OK Telegram token valid for @{me.get('username', 'unknown')} (no message sent)")
        return 0
    except TelegramError as exc:
        print(f"FAIL Telegram getMe: {exc}")
        return 1


async def check_destination(config: Config) -> int:
    if not config.token:
        print("TELEGRAM_BOT_TOKEN is not configured; no request made.")
        return 2
    if not config.destination_channel_id:
        print("TELEGRAM_DESTINATION_CHANNEL_ID is not configured; no request made.")
        return 2
    client = TelegramClient(config.token)
    try:
        me = await asyncio.to_thread(client.get_me)
        channel = await asyncio.to_thread(client.get_chat, config.destination_channel_id)
        if channel.get("type") != "channel":
            print("FAIL destination: configured chat is not a Telegram channel")
            return 1
        member = await asyncio.to_thread(client.get_chat_member, int(channel["id"]), int(me["id"]))
        status = member.get("status")
        can_post = status == "creator" or status == "administrator" and member.get("can_post_messages") is True
        if not can_post:
            print("FAIL destination: add the bot as a channel administrator with permission to post")
            return 1
        title = str(channel.get("title") or channel.get("username") or channel["id"])
        print(f"OK destination channel verified for @{me.get('username', 'unknown')}: {title} ({channel['id']}); no message sent")
        return 0
    except TelegramError as exc:
        print(f"FAIL destination verification: {exc}")
        return 1


def set_monitoring_state(config: Config, project_root: Path, enabled: bool) -> int:
    db_path = config.database if config.database.is_absolute() or str(config.database) == ":memory:" else project_root / config.database
    db = Database(db_path)
    try:
        db.set_monitoring_enabled(enabled)
        state = "ON" if enabled else "OFF"
        print(f"Monitoring set to {state}; no news was fetched or published.")
        return 0
    finally:
        db.close()


def run_service(config: Config, project_root: Path) -> None:
    if not config.token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and configure the token.")
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = BotService(config, project_root)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        loop.run_until_complete(service.run())
    except KeyboardInterrupt:
        service.stop_event.set()
    finally:
        loop.close()
