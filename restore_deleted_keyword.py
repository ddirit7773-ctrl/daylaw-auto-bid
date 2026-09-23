from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.lifecycle_store import DEFAULT_DB_PATH, LifecycleStore
from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from src.keyword_cleaner.policy_v2 import normalize


KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore one keyword from the V2 deletion archive. Dry-run by default."
    )
    parser.add_argument("--archive-id", type=int, required=True)
    parser.add_argument("--restore", action="store_true", help="Actually restore the keyword.")
    parser.add_argument("--confirm", default="", help="Live restore requires exactly: --confirm RESTORE")
    return parser.parse_args()


def load_archive(archive_id: int) -> dict:
    if not DEFAULT_DB_PATH.exists():
        raise SystemExit(f"Archive database does not exist: {DEFAULT_DB_PATH}")
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM keyword_archive WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"Archive id {archive_id} was not found.")
    return dict(row)


def main() -> int:
    args = parse_args()
    live = bool(args.restore)
    if live and args.confirm != "RESTORE":
        raise SystemExit("Live restore requires both --restore and --confirm RESTORE")

    archive = load_archive(args.archive_id)
    try:
        payload = json.loads(archive.get("payload_json") or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Archive payload is invalid JSON: {exc}") from exc

    plan = payload.get("plan") or {}
    backup = payload.get("keyword_before_delete") or {}
    keyword = str(archive.get("keyword") or backup.get("keyword") or "").strip()
    adgroup_id = str(archive.get("adgroup_id") or "").strip()

    print("=" * 72)
    print("V2 KEYWORD RESTORE")
    print("=" * 72)
    print(f"Mode          : {'LIVE RESTORE' if live else 'DRY RUN'}")
    print(f"Archive id    : {archive['archive_id']}")
    print(f"Deleted at    : {archive.get('deleted_at', '')}")
    print(f"Campaign      : {plan.get('campaign_name', archive.get('campaign_id', ''))}")
    print(f"Ad group      : {plan.get('adgroup_name', adgroup_id)}")
    print(f"Keyword       : {keyword}")
    print(f"Restored at   : {archive.get('restored_at') or '-'}")

    if archive.get("restored_at"):
        print("[SAFE STOP] This archive has already been marked restored.")
        return 0
    if not keyword or not adgroup_id:
        print("[SAFE STOP] Archive is missing keyword/ad-group identity.")
        return 2
    if not isinstance(backup, dict) or not backup:
        print("[SAFE STOP] Archive does not contain the original keyword payload.")
        return 2

    if not live:
        print("[DRY RUN] Archive is readable. Nothing was restored or modified.")
        return 0

    load_dotenv()
    client = NaverSearchAdsClient(NaverConfig.from_env(), min_interval_seconds=0.20)

    try:
        current_keywords = client.get_keywords(adgroup_id)
    except NaverSearchAdsError as exc:
        print(f"[SAFE STOP] Could not read target ad group: {exc}")
        return 3

    target_normalized = normalize(keyword)
    for row in current_keywords:
        if normalize(str(row.get("keyword", ""))) == target_normalized:
            print("[SAFE STOP] The same keyword already exists in the target ad group.")
            return 0

    backup = dict(backup)
    backup["keyword"] = keyword
    try:
        created = client.create_keyword_from_backup(adgroup_id, backup)
    except NaverSearchAdsError as exc:
        print(f"[RESTORE ERROR] {exc}")
        return 4

    try:
        verify_rows = client.get_keywords(adgroup_id)
    except NaverSearchAdsError as exc:
        print(f"[VERIFY ERROR] Restore API returned success but verification failed: {exc}")
        return 5

    restored = any(
        normalize(str(row.get("keyword", ""))) == target_normalized
        for row in verify_rows
    )
    if not restored:
        print("[VERIFY ERROR] Restored keyword was not found during post-restore verification.")
        return 5

    store = LifecycleStore()
    try:
        store.mark_restored(int(archive["archive_id"]), datetime.now(KST).isoformat())
    finally:
        store.close()

    print(f"[OK] Keyword restored and verified: {created.get('keyword', keyword)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
