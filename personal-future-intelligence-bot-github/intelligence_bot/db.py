from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Candidate, Source, source_to_dict


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._schema()
        self.conn.execute("UPDATE delivery_queue SET status='pending', due_at=? WHERE status='sending'", (utcnow(),))
        self.conn.execute("UPDATE publication_queue SET status='pending', due_at=? WHERE status='sending'", (utcnow(),))

    def _schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, config_json TEXT NOT NULL, enabled INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'not_checked', last_checked TEXT, last_success TEXT,
            last_error TEXT, etag TEXT, last_modified TEXT, initialized INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,
            categories_json TEXT NOT NULL DEFAULT '["*"]', tracked_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stable_id TEXT NOT NULL UNIQUE,
            canonical_url TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'feed',
            category TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', published TEXT NOT NULL DEFAULT '',
            usefulness_score INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, decision_reason TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}', claims_json TEXT NOT NULL DEFAULT '{}', image_url TEXT,
            original_post_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE TABLE IF NOT EXISTS story_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, version INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL, changed_at TEXT NOT NULL, UNIQUE(story_id, version),
            FOREIGN KEY(story_id) REFERENCES stories(id)
        );
        CREATE TABLE IF NOT EXISTS delivery_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            notice_kind TEXT NOT NULL DEFAULT 'new', status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            due_at TEXT NOT NULL, last_error TEXT, sent_at TEXT, telegram_message_id INTEGER,
            UNIQUE(story_id, chat_id, notice_kind),
            FOREIGN KEY(story_id) REFERENCES stories(id), FOREIGN KEY(chat_id) REFERENCES subscribers(chat_id)
        );
        CREATE INDEX IF NOT EXISTS queue_due ON delivery_queue(status, due_at, id);
        CREATE TABLE IF NOT EXISTS publication_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            notice_kind TEXT NOT NULL DEFAULT 'new', status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            due_at TEXT NOT NULL, last_error TEXT, sent_at TEXT, telegram_message_id INTEGER,
            UNIQUE(story_id, channel_id, notice_kind),
            FOREIGN KEY(story_id) REFERENCES stories(id)
        );
        CREATE INDEX IF NOT EXISTS publication_due ON publication_queue(status, due_at, id);
        CREATE INDEX IF NOT EXISTS story_category ON stories(category, created_at);
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER, chat_id INTEGER NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriber_topics (
            chat_id INTEGER NOT NULL, topic TEXT NOT NULL, PRIMARY KEY(chat_id, topic),
            FOREIGN KEY(chat_id) REFERENCES subscribers(chat_id)
        );
        CREATE TABLE IF NOT EXISTS telegram_channels (
            channel_id INTEGER PRIMARY KEY, username TEXT, title TEXT NOT NULL DEFAULT '',
            access_method TEXT NOT NULL DEFAULT 'bot_api_channel_post', health TEXT NOT NULL DEFAULT 'received',
            last_seen TEXT NOT NULL
        );
        """)
        source_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sources)")}
        if "channel_baselined" not in source_columns:
            self.conn.execute("ALTER TABLE sources ADD COLUMN channel_baselined INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def sync_sources(self, sources: list[Source]) -> None:
        with self.transaction():
            for source in sources:
                self.conn.execute(
                    "INSERT INTO sources(id,config_json,enabled) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,enabled=excluded.enabled",
                    (source.id, json.dumps(source_to_dict(source)), int(source.enabled)),
                )

    def source_state(self, source_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(source_id)
        return row

    def story_was_baselined(self, url: str) -> bool:
        row = self.conn.execute("SELECT decision_reason FROM stories WHERE canonical_url=?", (url,)).fetchone()
        return bool(row and row[0] == "Existing entry captured during initial baseline")

    def source_checked(self, source_id: str) -> None:
        self.conn.execute("UPDATE sources SET last_checked=? WHERE id=?", (utcnow(), source_id))

    def source_failed(self, source_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE sources SET status='error',last_checked=?,last_error=? WHERE id=?",
            (utcnow(), error[:1000], source_id),
        )

    def source_succeeded(self, source_id: str, etag: str | None, last_modified: str | None) -> None:
        self.conn.execute(
            "UPDATE sources SET status='healthy',last_checked=?,last_success=?,last_error=NULL,etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified),initialized=1 WHERE id=?",
            (utcnow(), utcnow(), etag, last_modified, source_id),
        )

    def source_channel_baseline_completed(self, source_id: str) -> None:
        self.conn.execute("UPDATE sources SET channel_baselined=1 WHERE id=?", (source_id,))

    def source_diagnostic(self, source_id: str, status: str, error: str | None = None) -> None:
        """Record CLI diagnostics without advancing the runtime feed baseline or polling clock."""
        self.conn.execute("UPDATE sources SET status=?,last_error=? WHERE id=?", (status, error[:1000] if error else None, source_id))

    def add_subscriber(self, chat_id: int) -> None:
        self.conn.execute(
            "INSERT INTO subscribers(chat_id,active,created_at) VALUES(?,1,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET active=1",
            (chat_id, utcnow()),
        )

    def stop_subscriber(self, chat_id: int) -> None:
        with self.transaction():
            self.conn.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (chat_id,))
            self.conn.execute("UPDATE delivery_queue SET status='cancelled' WHERE chat_id=? AND status='pending'", (chat_id,))

    def subscriber(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()

    def set_category(self, chat_id: int, category: str, enabled: bool) -> list[str]:
        row = self.subscriber(chat_id)
        categories = set(json.loads(row["categories_json"])) if row else {"*"}
        if category == "*":
            categories = {"*"} if enabled else set()
        else:
            if "*" in categories:
                categories = {"money", "ai", "students", "science_technology", "government", "tracked"}
            if enabled:
                categories.add(category)
            else:
                categories.discard(category)
        self.conn.execute("UPDATE subscribers SET categories_json=? WHERE chat_id=?", (json.dumps(sorted(categories)), chat_id))
        return sorted(categories)

    def add_topic(self, chat_id: int, topic: str) -> None:
        topic = " ".join(topic.split())[:100]
        if topic:
            self.conn.execute("INSERT OR IGNORE INTO subscriber_topics(chat_id,topic) VALUES(?,?)", (chat_id, topic))

    def topics(self, chat_id: int) -> tuple[str, ...]:
        rows = self.conn.execute("SELECT topic FROM subscriber_topics WHERE chat_id=? ORDER BY topic", (chat_id,)).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    def stable_id(url: str) -> str:
        import hashlib
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def save_candidate(self, candidate: Candidate, baseline: bool = False) -> tuple[int, bool, bool]:
        item, verification, classification = candidate.item, candidate.verification, candidate.classification
        evidence = {
            "status": verification.status,
            "url": verification.evidence_url,
            "excerpt": verification.evidence_excerpt[:900],
            "page_title": verification.page_title,
            "reason": verification.reason,
        }
        values = (
            self.stable_id(item.url), item.url, item.source_id, item.source_type,
            classification.category, item.title[:500], item.summary[:1200], item.published[:100], classification.score,
            "eligible" if classification.eligible else "rejected", "; ".join(classification.reasons)[:1000],
            json.dumps(evidence, ensure_ascii=False), json.dumps(verification.claims, ensure_ascii=False),
            verification.image_url or item.image_url, item.original_post_url, utcnow(), utcnow(),
        )
        with self.transaction():
            row = self.conn.execute("SELECT id,title,summary,claims_json,status,evidence_json FROM stories WHERE canonical_url=?", (item.url,)).fetchone()
            if row is None:
                cursor = self.conn.execute(
                    "INSERT INTO stories(stable_id,canonical_url,source_id,source_type,category,title,summary,published,usefulness_score,status,decision_reason,evidence_json,claims_json,image_url,original_post_url,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                story_id = int(cursor.lastrowid)
                self.conn.execute("INSERT INTO story_versions(story_id,version,snapshot_json,changed_at) VALUES(?,?,?,?)", (story_id, 1, json.dumps({"title": item.title, "summary": item.summary, "claims": verification.claims}, ensure_ascii=False), utcnow()))
                return story_id, True, False
            story_id = int(row["id"])
            changed = row["title"] != item.title[:500] or row["summary"] != item.summary[:1200] or row["claims_json"] != values[12] or row["status"] != values[9]
            if changed:
                count = self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM story_versions WHERE story_id=?", (story_id,)).fetchone()[0]
                self.conn.execute(
                    "UPDATE stories SET title=?,summary=?,published=?,usefulness_score=?,status=?,decision_reason=?,evidence_json=?,claims_json=?,image_url=?,updated_at=? WHERE id=?",
                    (values[5], values[6], values[7], values[8], values[9], values[10], values[11], values[12], values[13], utcnow(), story_id),
                )
                self.conn.execute("INSERT INTO story_versions(story_id,version,snapshot_json,changed_at) VALUES(?,?,?,?)", (story_id, count, json.dumps({"title": item.title, "summary": item.summary, "claims": verification.claims}, ensure_ascii=False), utcnow()))
                return story_id, False, True
            return story_id, False, False

    def queue_story(self, story_id: int, category: str, notice_kind: str = "new") -> int:
        queued = 0
        with self.transaction():
            rows = self.conn.execute("SELECT chat_id,categories_json FROM subscribers WHERE active=1").fetchall()
            story = self.conn.execute("SELECT title,summary,evidence_json FROM stories WHERE id=?", (story_id,)).fetchone()
            story_text = " ".join((story["title"], story["summary"], json.loads(story["evidence_json"]).get("excerpt", ""))).casefold() if story else ""
            for row in rows:
                categories = set(json.loads(row["categories_json"]))
                topics = self.conn.execute("SELECT topic FROM subscriber_topics WHERE chat_id=?", (row["chat_id"],)).fetchall()
                matches_tracked_topic = any(topic[0].casefold() in story_text for topic in topics)
                if category == "tracked" and not matches_tracked_topic:
                    continue
                if "*" not in categories and category not in categories and not matches_tracked_topic:
                    continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO delivery_queue(story_id,chat_id,notice_kind,status,due_at) VALUES(?,?,?,'pending',?)",
                    (story_id, row["chat_id"], notice_kind, utcnow()),
                )
                queued += cur.rowcount
        return queued

    def monitoring_enabled(self) -> bool:
        return self.get_metadata("monitoring_enabled", "0") == "1"

    def set_monitoring_enabled(self, enabled: bool) -> None:
        was_enabled = self.monitoring_enabled()
        self.set_metadata("monitoring_enabled", "1" if enabled else "0")
        if enabled and not was_enabled:
            self.set_metadata("monitoring_enabled_at", utcnow())

    def queue_publication(self, story_id: int, channel_id: int, notice_kind: str = "new") -> int:
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO publication_queue(story_id,channel_id,notice_kind,status,due_at) VALUES(?,?,?,'pending',?)",
            (story_id, channel_id, notice_kind, utcnow()),
        )
        return cursor.rowcount

    def due_publications(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT q.*,s.category,s.title,s.summary,s.published,s.image_url,s.original_post_url,s.evidence_json,s.claims_json,s.canonical_url,s.source_id,s.usefulness_score,src.config_json "
            "FROM publication_queue q JOIN stories s ON s.id=q.story_id JOIN sources src ON src.id=s.source_id "
            "WHERE q.status='pending' AND q.due_at<=? ORDER BY q.id",
            (utcnow(),),
        ).fetchall()

    def claim_publication(self, queue_id: int) -> bool:
        cursor = self.conn.execute(
            "UPDATE publication_queue SET status='sending',attempts=attempts+1 WHERE id=? AND status='pending' AND due_at<=?",
            (queue_id, utcnow()),
        )
        return cursor.rowcount == 1

    def previously_published(self, story_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM publication_queue WHERE story_id=? AND status='sent' LIMIT 1", (story_id,)).fetchone() is not None

    def publication_sent(self, queue_id: int, message_id: int | None) -> None:
        self.conn.execute("UPDATE publication_queue SET status='sent',sent_at=?,telegram_message_id=?,last_error=NULL WHERE id=?", (utcnow(), message_id, queue_id))

    def publication_failed(self, queue_id: int, error: str, delay_seconds: int, permanent: bool = False) -> None:
        self.conn.execute(
            "UPDATE publication_queue SET status=?,due_at=?,last_error=? WHERE id=?",
            ("failed" if permanent else "pending", datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + delay_seconds, timezone.utc).isoformat(timespec="seconds"), error[:1000], queue_id),
        )

    def publication_counts(self) -> dict[str, int]:
        return {row["status"]: row["n"] for row in self.conn.execute("SELECT status,COUNT(*) n FROM publication_queue GROUP BY status")}

    def latest_published(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*,MAX(q.sent_at) AS publication_sent_at FROM stories s JOIN publication_queue q ON q.story_id=s.id "
            "WHERE q.status='sent' GROUP BY s.id ORDER BY publication_sent_at DESC LIMIT ?",
            (max(1, min(50, limit)),),
        ).fetchall()

    def last_successful_check(self) -> str | None:
        row = self.conn.execute("SELECT MAX(last_success) FROM sources WHERE enabled=1").fetchone()
        return row[0] if row and row[0] else None

    def recent_source_errors(self, limit: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT id,last_error FROM sources WHERE enabled=1 AND last_error IS NOT NULL ORDER BY last_checked DESC LIMIT ?",
            (max(1, min(10, limit)),),
        ).fetchall()
        return [f"{row['id']}: {row['last_error']}" for row in rows]

    def story_id_for_url(self, url: str) -> int | None:
        row = self.conn.execute("SELECT id FROM stories WHERE canonical_url=?", (url,)).fetchone()
        return int(row[0]) if row else None

    def story_version(self, story_id: int) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(version),1) FROM story_versions WHERE story_id=?", (story_id,)).fetchone()
        return int(row[0])

    def due_deliveries(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT q.*,s.category,s.title,s.summary,s.published,s.image_url,s.original_post_url,s.evidence_json,s.claims_json,s.source_id,s.usefulness_score,src.config_json "
            "FROM delivery_queue q JOIN stories s ON s.id=q.story_id JOIN sources src ON src.id=s.source_id "
            "WHERE q.status='pending' AND q.due_at<=? ORDER BY q.id",
            (utcnow(),),
        ).fetchall()

    def claim_delivery(self, queue_id: int) -> bool:
        cursor = self.conn.execute(
            "UPDATE delivery_queue SET status='sending',attempts=attempts+1 WHERE id=? AND status='pending' AND due_at<=?",
            (queue_id, utcnow()),
        )
        return cursor.rowcount == 1

    def previously_delivered(self, story_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM delivery_queue WHERE story_id=? AND status='sent' LIMIT 1", (story_id,)).fetchone() is not None

    def delivery_sent(self, queue_id: int, message_id: int | None) -> None:
        self.conn.execute("UPDATE delivery_queue SET status='sent',sent_at=?,telegram_message_id=?,last_error=NULL WHERE id=?", (utcnow(), message_id, queue_id))

    def delivery_failed(self, queue_id: int, error: str, delay_seconds: int, permanent: bool = False) -> None:
        self.conn.execute(
            "UPDATE delivery_queue SET status=?,due_at=?,last_error=? WHERE id=?",
            ("failed" if permanent else "pending", datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + delay_seconds, timezone.utc).isoformat(timespec="seconds"), error[:1000], queue_id),
        )

    def recent_stories(self, category: str | None = None, query: str = "", limit: int = 10) -> list[sqlite3.Row]:
        sql = "SELECT * FROM stories WHERE status='eligible'"
        args: list[Any] = []
        if category:
            if category == "opportunities":
                sql += " AND category IN ('money','students','government','tracked')"
            else:
                sql += " AND category=?"
                args.append(category)
        if query:
            sql += " AND (title LIKE ? OR summary LIKE ?)"
            pattern = "%" + query[:100] + "%"
            args.extend((pattern, pattern))
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(50, limit)))
        return self.conn.execute(sql, args).fetchall()

    def report(self, chat_id: int, story_id: int | None, reason: str) -> None:
        self.conn.execute("INSERT INTO reports(story_id,chat_id,reason,created_at) VALUES(?,?,?,?)", (story_id, chat_id, reason[:500], utcnow()))

    def queue_counts(self) -> dict[str, int]:
        return {row["status"]: row["n"] for row in self.conn.execute("SELECT status,COUNT(*) n FROM delivery_queue GROUP BY status")}

    def source_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sources ORDER BY id").fetchall()

    def record_telegram_channel(self, channel_id: int, username: str | None, title: str) -> None:
        self.conn.execute(
            "INSERT INTO telegram_channels(channel_id,username,title,last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET username=excluded.username,title=excluded.title,last_seen=excluded.last_seen,health='received'",
            (channel_id, username, title[:200], utcnow()),
        )

    def telegram_channels(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM telegram_channels ORDER BY title,username").fetchall()

    def get_metadata(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_metadata(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def deadline_stories(self) -> list[sqlite3.Row]:
        from datetime import date, datetime
        formats = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")
        rows = self.recent_stories("money", limit=50)
        upcoming = []
        for row in rows:
            claims = json.loads(row["claims_json"])
            if not claims.get("deadline_excerpt") or claims.get("application_status_language") in ("closed", "cancelled_or_withdrawn", "date_conflict_check_required", "past_date_found_status_unresolved"):
                continue
            for raw in claims.get("date_mentions", []):
                parsed_date = None
                for fmt in formats:
                    try:
                        parsed_date = datetime.strptime(raw, fmt).date()
                        break
                    except ValueError:
                        continue
                if parsed_date and parsed_date >= date.today():
                    upcoming.append(row)
                    break
        return upcoming

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(destination))
        try:
            self.conn.backup(target)
        finally:
            target.close()
