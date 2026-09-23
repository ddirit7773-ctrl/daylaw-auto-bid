from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path


BACKUP_DIR = Path("data/backups")


def latest_scan() -> Path:
    files = sorted(BACKUP_DIR.glob("v2_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No v2_scan_*.csv found in data/backups")
    return files[-1]


def main() -> int:
    path = latest_scan()
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))

    status = Counter(row.get("status", "") for row in rows)
    tier = Counter(row.get("tier", "") for row in rows)
    exposure = Counter(row.get("exposure_eligible", "") for row in rows)
    reasons = Counter(row.get("reason", "") for row in rows)
    keyword_status = Counter(row.get("keyword_status", "") for row in rows)
    inspect_status = Counter(row.get("inspect_status", "") for row in rows)
    stats_source = Counter(row.get("inactivity_stats_source", "") for row in rows)

    print("=" * 76)
    print("V2 SCAN SUMMARY — LOCAL CSV ONLY / NO API / NO DELETION")
    print(f"Source: {path}")
    print(f"Rows  : {len(rows):,}")
    print("")

    print("STATUS")
    for key, value in status.most_common():
        print(f"  {key or '(blank)':22s}: {value:,}")

    print("\nTIER")
    for key, value in tier.most_common():
        print(f"  {key or '(blank)':22s}: {value:,}")

    print("\nEXPOSURE ELIGIBLE")
    for key, value in exposure.most_common():
        print(f"  {key or '(blank)':22s}: {value:,}")

    print("\nKEYWORD STATUS")
    for key, value in keyword_status.most_common(10):
        print(f"  {key or '(blank)':22s}: {value:,}")

    print("\nINSPECT STATUS")
    for key, value in inspect_status.most_common(10):
        print(f"  {key or '(blank)':22s}: {value:,}")

    print("\nINACTIVITY STATS SOURCE")
    for key, value in stats_source.most_common(10):
        print(f"  {key or '(blank)':22s}: {value:,}")

    print("\nTOP DECISION REASONS")
    for key, value in reasons.most_common(20):
        print(f"  {value:7,d}  {key}")

    print("\nNo API request was made. Nothing was deleted or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
