from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError


BACKUP_DIR = Path("data/backups")
BATCH_SIZES = (1, 10, 50, 100)


def latest_v2_scan() -> Path:
    files = sorted(BACKUP_DIR.glob("v2_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No v2_scan_*.csv found in data/backups")
    return files[-1]


def load_candidate_ids(path: Path, limit: int = 100) -> list[str]:
    ids: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            if row.get("reason") != "stats_incomplete":
                continue
            kid = str(row.get("keyword_id", "")).strip()
            if kid:
                ids.append(kid)
            if len(ids) >= limit:
                break
    return ids


def main() -> int:
    load_dotenv()
    source = latest_v2_scan()
    ids = load_candidate_ids(source, 100)
    if not ids:
        raise SystemExit("No stats_incomplete keyword IDs found in latest V2 scan")

    client = NaverSearchAdsClient(
        NaverConfig.from_env(),
        min_interval_seconds=0.35,
        max_retries=2,
    )

    fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
    # Reuse the exact date range stored in the latest scan row.
    with source.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))
    first = next(row for row in rows if row.get("reason") == "stats_incomplete")
    since = first.get("inactivity_since", "")
    until = first.get("inactivity_until", "")
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))

    print("=" * 72)
    print("STATS BATCH ERROR DIAGNOSTIC — READ ONLY / NO DELETION")
    print(f"Source : {source}")
    print(f"Range  : {since} ~ {until}")
    print("=" * 72)

    for size in BATCH_SIZES:
        batch = ids[:size]
        params = {
            "ids": json.dumps(batch, separators=(",", ":")),
            "fields": fields,
            "timeRange": time_range,
            "timeIncrement": "allDays",
        }
        print(f"\n[BATCH {size}] requesting {len(batch)} ids...")
        try:
            payload = client._request("GET", "/stats", params=params)
            if isinstance(payload, dict):
                data = payload.get("data", [])
                count = len(data) if isinstance(data, list) else 0
            elif isinstance(payload, list):
                count = len(payload)
            else:
                count = 0
            print(f"  OK — returned rows: {count}")
        except NaverSearchAdsError as exc:
            print("  ERROR")
            print(f"  {exc}")
        time.sleep(1.0)

    print("\nNo API data was modified. No keyword was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
