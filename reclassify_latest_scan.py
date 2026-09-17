from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.keyword_cleaner.rules import DEFAULT_PROTECTED_SUFFIXES, classify_keyword


BACKUP_DIR = Path("data/backups")


def latest_scan_csv() -> Path:
    files = sorted(BACKUP_DIR.glob("keyword_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No keyword_scan_*.csv found in data/backups")
    return files[-1]


def split_env(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def to_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def to_optional_int(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def main() -> int:
    load_dotenv()
    source = latest_scan_csv()

    recent_days = int(os.getenv("RECENT_DAYS", "21"))
    history_days = int(os.getenv("HISTORY_DAYS", "90"))
    min_age_days = int(os.getenv("MIN_KEYWORD_AGE_DAYS", "21"))
    protected_suffixes = split_env("PROTECTED_SUFFIXES") or list(DEFAULT_PROTECTED_SUFFIXES)
    extra_exact_keywords = split_env("EXTRA_PROTECTED_KEYWORDS")

    with source.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    for row in rows:
        decision = classify_keyword(
            adgroup_name=row.get("adgroup_name", ""),
            keyword=row.get("keyword", ""),
            age_days=to_optional_int(row.get("age_days")),
            recent_impressions=to_int(row.get("recent_impressions")),
            recent_clicks=to_int(row.get("recent_clicks")),
            history_impressions=to_int(row.get("history_impressions")),
            history_clicks=to_int(row.get("history_clicks")),
            min_age_days=min_age_days,
            recent_days=recent_days,
            history_days=history_days,
            protected_suffixes=protected_suffixes,
            extra_exact_keywords=extra_exact_keywords,
        )
        row["status"] = decision.status
        row["reason"] = decision.reason
        row["protected"] = str(decision.protected)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = BACKUP_DIR / f"keyword_scan_{stamp}.csv"
    with output.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row.get("status", "") for row in rows)
    core = sum(
        1
        for row in rows
        if str(row.get("reason", "")).startswith("core:")
        or row.get("reason") == "extra_whitelist"
    )

    print("=" * 72)
    print("RECLASSIFY LATEST SCAN — NO API CALL / NO DELETION")
    print(f"Source            : {source}")
    print(f"Output            : {output}")
    print(f"Keywords          : {len(rows):,}")
    print(f"KEEP              : {counts['KEEP']:,}")
    print(f"WATCH             : {counts['WATCH']:,}")
    print(f"DELETE_CANDIDATE  : {counts['DELETE_CANDIDATE']:,}")
    print(f"CORE/WHITELIST    : {core:,}")
    print("")
    print("No API request was made. No keyword was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
