from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/state/keyword_lifecycle.sqlite3")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PendingState:
    identity_key: str
    keyword_id: str
    campaign_id: str
    adgroup_id: str
    keyword: str
    tier: str
    first_pending_at: str | None
    last_pending_at: str | None
    consecutive_pending: int
    last_decision: str
    last_reason: str


class LifecycleStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS keyword_state (
                identity_key TEXT PRIMARY KEY,
                keyword_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                adgroup_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                tier TEXT NOT NULL,
                first_pending_at TEXT,
                last_pending_at TEXT,
                consecutive_pending INTEGER NOT NULL DEFAULT 0,
                last_decision TEXT NOT NULL,
                last_reason TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS keyword_archive (
                archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_key TEXT NOT NULL,
                keyword_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                adgroup_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                tier TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                delete_reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                restored_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_keyword_archive_identity
                ON keyword_archive(identity_key);
            CREATE INDEX IF NOT EXISTS idx_keyword_archive_deleted_at
                ON keyword_archive(deleted_at);
            """
        )
        self.conn.commit()

    def record_decision(
        self,
        *,
        identity_key: str,
        keyword_id: str,
        campaign_id: str,
        adgroup_id: str,
        keyword: str,
        tier: str,
        decision: str,
        reason: str,
        observed_at: str | None = None,
        commit: bool = True,
    ) -> PendingState:
        observed_at = observed_at or _utc_now_iso()
        existing = self.conn.execute(
            "SELECT * FROM keyword_state WHERE identity_key = ?",
            (identity_key,),
        ).fetchone()

        if decision == "DELETE_PENDING":
            if existing and existing["last_decision"] == "DELETE_PENDING":
                first_pending_at = existing["first_pending_at"] or observed_at
                consecutive_pending = int(existing["consecutive_pending"] or 0) + 1
            else:
                first_pending_at = observed_at
                consecutive_pending = 1
            last_pending_at = observed_at
        else:
            first_pending_at = None
            last_pending_at = None
            consecutive_pending = 0

        self.conn.execute(
            """
            INSERT INTO keyword_state (
                identity_key, keyword_id, campaign_id, adgroup_id, keyword, tier,
                first_pending_at, last_pending_at, consecutive_pending,
                last_decision, last_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                keyword_id = excluded.keyword_id,
                campaign_id = excluded.campaign_id,
                adgroup_id = excluded.adgroup_id,
                keyword = excluded.keyword,
                tier = excluded.tier,
                first_pending_at = excluded.first_pending_at,
                last_pending_at = excluded.last_pending_at,
                consecutive_pending = excluded.consecutive_pending,
                last_decision = excluded.last_decision,
                last_reason = excluded.last_reason,
                updated_at = excluded.updated_at
            """,
            (
                identity_key,
                keyword_id,
                campaign_id,
                adgroup_id,
                keyword,
                tier,
                first_pending_at,
                last_pending_at,
                consecutive_pending,
                decision,
                reason,
                observed_at,
            ),
        )
        if commit:
            self.conn.commit()
        return self.get_state(identity_key)

    def get_state(self, identity_key: str) -> PendingState:
        row = self.conn.execute(
            "SELECT * FROM keyword_state WHERE identity_key = ?",
            (identity_key,),
        ).fetchone()
        if row is None:
            raise KeyError(identity_key)
        return PendingState(
            identity_key=row["identity_key"],
            keyword_id=row["keyword_id"],
            campaign_id=row["campaign_id"],
            adgroup_id=row["adgroup_id"],
            keyword=row["keyword"],
            tier=row["tier"],
            first_pending_at=row["first_pending_at"],
            last_pending_at=row["last_pending_at"],
            consecutive_pending=int(row["consecutive_pending"]),
            last_decision=row["last_decision"],
            last_reason=row["last_reason"],
        )

    def archive_deleted_keyword(
        self,
        *,
        identity_key: str,
        keyword_id: str,
        campaign_id: str,
        adgroup_id: str,
        keyword: str,
        tier: str,
        delete_reason: str,
        payload: dict[str, Any],
        deleted_at: str | None = None,
    ) -> int:
        deleted_at = deleted_at or _utc_now_iso()
        cursor = self.conn.execute(
            """
            INSERT INTO keyword_archive (
                identity_key, keyword_id, campaign_id, adgroup_id, keyword, tier,
                deleted_at, delete_reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity_key,
                keyword_id,
                campaign_id,
                adgroup_id,
                keyword,
                tier,
                deleted_at,
                delete_reason,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def mark_restored(self, archive_id: int, restored_at: str | None = None) -> None:
        restored_at = restored_at or _utc_now_iso()
        self.conn.execute(
            "UPDATE keyword_archive SET restored_at = ? WHERE archive_id = ?",
            (restored_at, archive_id),
        )
        self.conn.commit()

    def export_state(self, identity_key: str) -> dict[str, Any]:
        return asdict(self.get_state(identity_key))
