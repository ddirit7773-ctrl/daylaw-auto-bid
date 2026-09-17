from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path


BACKUP_DIR = Path("data/backups")
SAMPLE_SIZE = 300
RANDOM_SEED = 20260917


def latest_scan_csv() -> Path:
    files = sorted(BACKUP_DIR.glob("keyword_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No keyword_scan_*.csv found in data/backups")
    return files[-1]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def int_value(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    source = latest_scan_csv()
    rows = read_rows(source)
    candidates = [r for r in rows if r.get("status") == "DELETE_CANDIDATE"]

    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    all_grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)

    for row in rows:
        key = (
            row.get("campaign_name", ""),
            row.get("campaign_id", ""),
            row.get("adgroup_name", ""),
            row.get("adgroup_id", ""),
        )
        all_grouped[key].append(row)
        if row.get("status") == "DELETE_CANDIDATE":
            grouped[key].append(row)

    summary: list[dict] = []
    for key, group_rows in all_grouped.items():
        candidate_rows = grouped.get(key, [])
        keep = sum(r.get("status") == "KEEP" for r in group_rows)
        watch = sum(r.get("status") == "WATCH" for r in group_rows)
        delete = len(candidate_rows)
        total = len(group_rows)
        survivors = keep + watch
        delete_rate = delete / total if total else 0.0
        summary.append(
            {
                "campaign_name": key[0],
                "campaign_id": key[1],
                "adgroup_name": key[2],
                "adgroup_id": key[3],
                "total_keywords": total,
                "keep": keep,
                "watch": watch,
                "delete_candidates": delete,
                "survivors_after_delete": survivors,
                "delete_rate_pct": round(delete_rate * 100, 2),
                "risk_flag": (
                    "VERY_HIGH" if survivors <= 2 and delete > 0
                    else "HIGH" if survivors <= 5 and delete > 0
                    else "HIGH" if delete_rate >= 0.95 and delete > 0
                    else "REVIEW" if delete_rate >= 0.80 and delete > 0
                    else "OK"
                ),
            }
        )

    summary.sort(
        key=lambda r: (
            0 if r["risk_flag"] == "VERY_HIGH" else 1 if r["risk_flag"] == "HIGH" else 2 if r["risk_flag"] == "REVIEW" else 3,
            r["survivors_after_delete"],
            -r["delete_candidates"],
        )
    )

    # Stratified sample: first take candidates from the riskiest groups,
    # then fill the remainder with deterministic random candidates across the account.
    sample: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    risky_keys = [
        (r["campaign_name"], r["campaign_id"], r["adgroup_name"], r["adgroup_id"])
        for r in summary
        if r["risk_flag"] in {"VERY_HIGH", "HIGH", "REVIEW"}
    ]

    for key in risky_keys:
        for row in grouped.get(key, [])[:2]:
            kid = row.get("keyword_id", "")
            if kid and kid not in seen_ids:
                sample.append(row)
                seen_ids.add(kid)
                if len(sample) >= SAMPLE_SIZE // 2:
                    break
        if len(sample) >= SAMPLE_SIZE // 2:
            break

    remaining = [r for r in candidates if r.get("keyword_id", "") not in seen_ids]
    random.Random(RANDOM_SEED).shuffle(remaining)
    for row in remaining:
        if len(sample) >= SAMPLE_SIZE:
            break
        sample.append(row)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = BACKUP_DIR / f"candidate_group_review_{stamp}.csv"
    sample_path = BACKUP_DIR / f"candidate_sample_{stamp}.csv"

    write_csv(
        summary_path,
        summary,
        [
            "campaign_name",
            "campaign_id",
            "adgroup_name",
            "adgroup_id",
            "total_keywords",
            "keep",
            "watch",
            "delete_candidates",
            "survivors_after_delete",
            "delete_rate_pct",
            "risk_flag",
        ],
    )

    sample_fields = list(rows[0].keys()) if rows else []
    write_csv(sample_path, sample, sample_fields)

    risk_counts = defaultdict(int)
    for row in summary:
        risk_counts[row["risk_flag"]] += 1

    print("=" * 72)
    print("KEYWORD DELETE-CANDIDATE REVIEW")
    print(f"Source scan         : {source}")
    print(f"Total keywords      : {len(rows):,}")
    print(f"Delete candidates   : {len(candidates):,}")
    print(f"Ad groups           : {len(summary):,}")
    print(f"VERY_HIGH risk      : {risk_counts['VERY_HIGH']:,}")
    print(f"HIGH risk           : {risk_counts['HIGH']:,}")
    print(f"REVIEW risk         : {risk_counts['REVIEW']:,}")
    print(f"OK groups           : {risk_counts['OK']:,}")
    print(f"Group review CSV    : {summary_path}")
    print(f"Candidate sample CSV: {sample_path}")
    print("")
    print("No keyword was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
