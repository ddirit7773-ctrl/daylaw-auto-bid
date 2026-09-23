from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


BACKUP_DIR = Path("data/backups")
PER_GROUP_DELETE_CAP = 0.50
MIN_GROUP_SURVIVORS = 4


def latest_v2_scan() -> Path:
    files = sorted(BACKUP_DIR.glob("v2_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No v2_scan_*.csv found in data/backups")
    return files[-1]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    source = latest_v2_scan()
    rows = read_rows(source)
    if not rows:
        raise SystemExit("Latest V2 scan is empty")

    target = max(as_int(rows[0].get("cleanup_target_count"), 0), 0)
    if target <= 0:
        print("Cleanup target is 0. No delete plan needed.")
        return 0

    by_group: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_group[(row.get("campaign_id", ""), row.get("adgroup_id", ""))].append(row)

    group_allowance: dict[tuple[str, str], int] = {}
    for key, group_rows in by_group.items():
        total = len(group_rows)
        pending_general = sum(
            1
            for row in group_rows
            if row.get("status") == "DELETE_PENDING" and row.get("tier") == "GENERAL"
        )
        cap_by_rate = math.floor(total * PER_GROUP_DELETE_CAP)
        cap_by_survivors = max(total - MIN_GROUP_SURVIVORS, 0)
        group_allowance[key] = max(min(pending_general, cap_by_rate, cap_by_survivors), 0)

    candidates = [
        row
        for row in rows
        if row.get("status") == "DELETE_PENDING" and row.get("tier") == "GENERAL"
    ]

    # Safest first: oldest keywords first. Every DELETE_PENDING row already has
    # 0 impressions / 0 clicks in the inactivity window and 0 clicks in the
    # click-protection window. TYPE_CORE is intentionally excluded in wave 1.
    candidates.sort(
        key=lambda row: (
            -as_int(row.get("age_days"), 0),
            row.get("campaign_name", ""),
            row.get("adgroup_name", ""),
            row.get("keyword", ""),
        )
    )

    selected: list[dict[str, object]] = []
    held: list[dict[str, object]] = []
    selected_per_group: Counter[tuple[str, str]] = Counter()

    for row in candidates:
        key = (row.get("campaign_id", ""), row.get("adgroup_id", ""))
        allowance = group_allowance.get(key, 0)
        if len(selected) < target and selected_per_group[key] < allowance:
            out = dict(row)
            out["plan_action"] = "TARGET_DELETE_AFTER_APPROVAL"
            out["plan_reason"] = (
                "GENERAL_DELETE_PENDING; oldest-first; TYPE_CORE excluded; "
                f"group delete cap={int(PER_GROUP_DELETE_CAP*100)}%; "
                f"min survivors={MIN_GROUP_SURVIVORS}"
            )
            selected.append(out)
            selected_per_group[key] += 1
        else:
            out = dict(row)
            out["plan_action"] = "HOLD"
            if len(selected) >= target:
                out["plan_reason"] = "account cleanup target already filled"
            else:
                out["plan_reason"] = "group safety cap / minimum survivors"
            held.append(out)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    selected_path = BACKUP_DIR / f"v2_target_delete_plan_{stamp}.csv"
    group_path = BACKUP_DIR / f"v2_target_delete_groups_{stamp}.csv"

    selected_fields = list(rows[0].keys()) + ["plan_action", "plan_reason"]
    write_csv(selected_path, selected, selected_fields)

    group_rows_out: list[dict[str, object]] = []
    for key, group_rows in by_group.items():
        campaign_name = group_rows[0].get("campaign_name", "")
        adgroup_name = group_rows[0].get("adgroup_name", "")
        total = len(group_rows)
        pending_general = sum(
            1
            for row in group_rows
            if row.get("status") == "DELETE_PENDING" and row.get("tier") == "GENERAL"
        )
        selected_count = selected_per_group.get(key, 0)
        group_rows_out.append(
            {
                "campaign_name": campaign_name,
                "adgroup_name": adgroup_name,
                "total_keywords": total,
                "pending_general": pending_general,
                "selected_for_target": selected_count,
                "projected_survivors": total - selected_count,
                "delete_rate_pct": round((selected_count / total * 100) if total else 0, 1),
            }
        )

    group_rows_out.sort(
        key=lambda row: (-as_int(row["selected_for_target"]), str(row["adgroup_name"]))
    )
    write_csv(
        group_path,
        group_rows_out,
        [
            "campaign_name",
            "adgroup_name",
            "total_keywords",
            "pending_general",
            "selected_for_target",
            "projected_survivors",
            "delete_rate_pct",
        ],
    )

    print("=" * 72)
    print("V2 TARGET DELETE PLAN — PLAN ONLY / NO API / NO DELETION")
    print("=" * 72)
    print(f"Source                  : {source}")
    print(f"Current keywords        : {len(rows):,}")
    print(f"Account target removals : {target:,}")
    print(f"GENERAL pending pool    : {len(candidates):,}")
    print(f"TYPE_CORE selected      : 0")
    print(f"Planned target deletes  : {len(selected):,}")
    print(f"Held GENERAL candidates : {len(held):,}")
    print(f"Per-group max delete    : {int(PER_GROUP_DELETE_CAP*100)}%")
    print(f"Min survivors/group     : {MIN_GROUP_SURVIVORS}")
    print(f"Projected keywords      : {len(rows) - len(selected):,}")
    print(f"Plan CSV                : {selected_path}")
    print(f"Group summary CSV       : {group_path}")
    print("")
    if len(selected) < target:
        print(
            f"WARNING: safety caps filled only {len(selected):,}/{target:,} target removals. "
            "Do not weaken caps automatically. Review before changing policy."
        )
    else:
        print("Target can be filled using GENERAL candidates only; TYPE_CORE stays untouched in wave 1.")
    print("")
    print("No API request was made. Nothing was deleted or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
