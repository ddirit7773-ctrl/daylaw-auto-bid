from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


CSV_FIELDS = [
    "campaign_name",
    "campaign_id",
    "adgroup_name",
    "adgroup_id",
    "keyword",
    "keyword_id",
    "bid_amt",
    "use_group_bid_amt",
    "impressions",
    "clicks",
    "status",
    "reason",
    "protected",
    "stats_since",
    "stats_until",
]


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_backup_dir(path: str | Path) -> Path:
    backup_dir = Path(path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def write_scan_backups(
    rows: Iterable[dict[str, Any]],
    raw_records: Iterable[dict[str, Any]],
    *,
    backup_dir: str | Path,
    prefix: str,
) -> tuple[Path, Path]:
    folder = ensure_backup_dir(backup_dir)
    stamp = timestamp_slug()
    csv_path = folder / f"{prefix}_{stamp}.csv"
    json_path = folder / f"{prefix}_{stamp}.json"

    rows_list = list(rows)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_list)

    with json_path.open("w", encoding="utf-8") as fp:
        json.dump(list(raw_records), fp, ensure_ascii=False, indent=2)

    return csv_path, json_path


def append_delete_log(
    record: dict[str, Any],
    *,
    log_path: str | Path,
) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fields = [
        "deleted_at",
        "campaign_name",
        "campaign_id",
        "adgroup_name",
        "adgroup_id",
        "keyword",
        "keyword_id",
        "impressions",
        "clicks",
        "result",
        "message",
    ]
    with path.open("a", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(record)
