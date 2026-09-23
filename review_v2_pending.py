from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


BACKUP_DIR = Path("data/backups")
SAMPLE_SIZE = 120


def latest_v2_scan() -> Path:
    files = sorted(BACKUP_DIR.glob("v2_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No v2_scan_*.csv found in data/backups")
    return files[-1]


def age_bucket(value: str) -> str:
    try:
        age = int(float(value or 0))
    except (TypeError, ValueError):
        return "UNKNOWN"
    if age < 30:
        return "21-29d"
    if age < 45:
        return "30-44d"
    if age < 60:
        return "45-59d"
    if age < 90:
        return "60-89d"
    return "90d+"


def main() -> int:
    source = latest_v2_scan()
    with source.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))

    pending = [row for row in rows if row.get("status") == "DELETE_PENDING"]
    if not pending:
        raise SystemExit("No DELETE_PENDING rows found in latest V2 scan")

    tier_counter = Counter(row.get("tier", "") for row in pending)
    campaign_counter = Counter(row.get("campaign_name", "") for row in pending)
    age_counter = Counter(age_bucket(row.get("age_days", "")) for row in pending)

    group_totals: dict[tuple[str, str], int] = defaultdict(int)
    group_pending: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        key = (row.get("campaign_name", ""), row.get("adgroup_name", ""))
        group_totals[key] += 1
    for row in pending:
        key = (row.get("campaign_name", ""), row.get("adgroup_name", ""))
        group_pending[key] += 1

    group_rows = []
    for key, count in group_pending.items():
        total = group_totals.get(key, 0)
        rate = (count / total) if total else 0.0
        group_rows.append((rate, count, total, key[0], key[1]))
    group_rows.sort(reverse=True)

    # Build a sample that deliberately includes TYPE_CORE plus both youngest
    # and oldest GENERAL pending rows. This makes manual review more useful
    # than taking only the first N rows from the CSV.
    type_core = [r for r in pending if r.get("tier") == "TYPE_CORE"]
    general = [r for r in pending if r.get("tier") == "GENERAL"]

    def age_num(row: dict) -> int:
        try:
            return int(float(row.get("age_days", 0) or 0))
        except (TypeError, ValueError):
            return -1

    general_oldest = sorted(general, key=age_num, reverse=True)
    general_youngest = sorted(general, key=age_num)

    sample: list[dict] = []
    seen: set[str] = set()

    def add_rows(candidates: list[dict], limit: int) -> None:
        for row in candidates:
            kid = row.get("keyword_id", "")
            if not kid or kid in seen:
                continue
            seen.add(kid)
            sample.append(row)
            if len(sample) >= limit:
                return

    add_rows(type_core, min(40, SAMPLE_SIZE))
    add_rows(general_youngest, min(80, SAMPLE_SIZE))
    add_rows(general_oldest, SAMPLE_SIZE)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sample_path = BACKUP_DIR / f"v2_pending_sample_{stamp}.csv"
    fields = [
        "campaign_name",
        "adgroup_name",
        "keyword",
        "keyword_id",
        "tier",
        "age_days",
        "status",
        "reason",
        "inactivity_window_days",
        "inactivity_impressions",
        "inactivity_clicks",
        "click_window_days",
        "click_window_clicks",
        "exposure_eligible",
        "pending_first_at",
        "recheck_ready_at",
    ]
    with sample_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sample)

    print("=" * 72)
    print("V2 DELETE_PENDING REVIEW — CSV ONLY / NO API / NO DELETION")
    print(f"Source              : {source}")
    print(f"Total keywords      : {len(rows):,}")
    print(f"DELETE_PENDING      : {len(pending):,}")
    print("")

    print("PENDING BY TIER")
    for key, value in tier_counter.most_common():
        print(f"  {key:12s}: {value:,}")

    print("\nPENDING BY AGE")
    for key in ("21-29d", "30-44d", "45-59d", "60-89d", "90d+", "UNKNOWN"):
        if age_counter[key]:
            print(f"  {key:12s}: {age_counter[key]:,}")

    print("\nPENDING BY CAMPAIGN")
    for key, value in campaign_counter.most_common():
        print(f"  {key:20s}: {value:,}")

    print("\nTOP AD GROUPS BY PENDING RATE")
    for rate, count, total, campaign, group in group_rows[:20]:
        print(
            f"  {campaign} / {group}: {count:,}/{total:,} "
            f"({rate * 100:.1f}%) pending"
        )

    print("")
    print(f"Manual review sample: {sample_path}")
    print(f"Sample rows          : {len(sample):,}")
    print("")
    print("No API request was made. Nothing was deleted or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
