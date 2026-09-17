from __future__ import annotations

import csv
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


BACKUP_DIR = Path("data/backups")
DEFAULT_MIN_SURVIVORS = 20
DEFAULT_MAX_DELETE_RATE = 0.60
DEFAULT_RESERVE_TOKENS = (
    "사기",
    "피해",
    "피해금",
    "사칭",
    "출금",
    "출금거부",
    "환불",
    "환전",
    "입금",
    "송금",
    "투자",
    "리딩방",
    "거래소",
    "부업",
    "알바",
    "재택",
    "팀미션",
    "미션",
    "쇼핑몰",
    "구매대행",
    "리뷰",
    "먹튀",
    "후기",
    "신고",
    "고소",
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s\-_./·ㆍ()\[\]{}]+", "", text)


def latest_scan_csv() -> Path:
    files = sorted(BACKUP_DIR.glob("keyword_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("No keyword_scan_*.csv found in data/backups")
    return files[-1]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def reserve_tokens() -> tuple[str, ...]:
    raw = os.getenv("RESERVE_TOKENS", "").strip()
    if not raw:
        return DEFAULT_RESERVE_TOKENS
    # Built-ins are always retained; .env values only add reserve concepts.
    extras = tuple(part.strip() for part in raw.split(",") if part.strip())
    return tuple(dict.fromkeys((*DEFAULT_RESERVE_TOKENS, *extras)))


def reserve_score(row: dict[str, str], tokens: tuple[str, ...]) -> tuple[int, int, str]:
    """Higher score means more valuable to HOLD instead of delete.

    All DELETE_CANDIDATE rows have no 21/90-day activity already. Among them,
    favor keeping semantically useful fraud-related variants and shorter phrases.
    """
    keyword = normalize(row.get("keyword", ""))
    token_hits = sum(1 for token in tokens if normalize(token) and normalize(token) in keyword)
    shortness = max(0, 40 - len(keyword))
    return (token_hits * 100 + shortness, -len(keyword), keyword)


def main() -> int:
    load_dotenv()
    source = latest_scan_csv()
    rows = read_rows(source)

    min_survivors = int(os.getenv("MIN_SURVIVORS_PER_GROUP", str(DEFAULT_MIN_SURVIVORS)))
    max_delete_rate = float(os.getenv("MAX_DELETE_RATE_PER_GROUP", str(DEFAULT_MAX_DELETE_RATE)))
    if min_survivors < 1:
        raise SystemExit("MIN_SURVIVORS_PER_GROUP must be >= 1")
    if not (0 < max_delete_rate < 1):
        raise SystemExit("MAX_DELETE_RATE_PER_GROUP must be between 0 and 1")

    tokens = reserve_tokens()

    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("campaign_name", ""),
            row.get("campaign_id", ""),
            row.get("adgroup_name", ""),
            row.get("adgroup_id", ""),
        )
        grouped[key].append(row)

    plan_rows: list[dict] = []
    approved_rows: list[dict] = []
    group_summary: list[dict] = []

    for key, group_rows in grouped.items():
        candidates = [r for r in group_rows if r.get("status") == "DELETE_CANDIDATE"]
        total = len(group_rows)
        existing_survivors = total - len(candidates)

        by_ratio = math.floor(total * max_delete_rate)
        by_floor = max(0, total - min_survivors)
        allowed_delete = min(len(candidates), by_ratio, by_floor)

        reserve_count = len(candidates) - allowed_delete
        ranked_for_hold = sorted(candidates, key=lambda r: reserve_score(r, tokens), reverse=True)
        held_ids = {
            row.get("keyword_id", "")
            for row in ranked_for_hold[:reserve_count]
            if row.get("keyword_id", "")
        }

        approved = 0
        held = 0
        for row in candidates:
            kid = row.get("keyword_id", "")
            if kid in held_ids:
                action = "GROUP_GUARD_HOLD"
                held += 1
            else:
                action = "SAFE_DELETE"
                approved += 1
                approved_rows.append({**row, "plan_action": action})

            plan_rows.append(
                {
                    **row,
                    "plan_action": action,
                    "group_total_keywords": total,
                    "group_existing_survivors": existing_survivors,
                    "group_safe_delete_count": allowed_delete,
                    "group_min_survivors": min_survivors,
                    "group_max_delete_rate": max_delete_rate,
                    "group_survivors_after_plan": total - allowed_delete,
                }
            )

        group_summary.append(
            {
                "campaign_name": key[0],
                "campaign_id": key[1],
                "adgroup_name": key[2],
                "adgroup_id": key[3],
                "total_keywords": total,
                "existing_keep_watch": existing_survivors,
                "raw_delete_candidates": len(candidates),
                "safe_delete": approved,
                "group_guard_hold": held,
                "survivors_after_plan": total - approved,
                "planned_delete_rate_pct": round((approved / total * 100) if total else 0, 2),
            }
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = BACKUP_DIR / f"safe_delete_plan_{stamp}.csv"
    approved_path = BACKUP_DIR / f"safe_delete_approved_{stamp}.csv"
    summary_path = BACKUP_DIR / f"safe_delete_group_summary_{stamp}.csv"

    base_fields = list(rows[0].keys()) if rows else []
    plan_extra = [
        "plan_action",
        "group_total_keywords",
        "group_existing_survivors",
        "group_safe_delete_count",
        "group_min_survivors",
        "group_max_delete_rate",
        "group_survivors_after_plan",
    ]
    write_csv(plan_path, plan_rows, base_fields + plan_extra)
    write_csv(approved_path, approved_rows, base_fields + ["plan_action"])
    write_csv(
        summary_path,
        group_summary,
        [
            "campaign_name",
            "campaign_id",
            "adgroup_name",
            "adgroup_id",
            "total_keywords",
            "existing_keep_watch",
            "raw_delete_candidates",
            "safe_delete",
            "group_guard_hold",
            "survivors_after_plan",
            "planned_delete_rate_pct",
        ],
    )

    action_counts = Counter(row["plan_action"] for row in plan_rows)
    all_candidates = len(plan_rows)
    projected_keywords = len(rows) - action_counts["SAFE_DELETE"]

    print("=" * 72)
    print("CONSERVATIVE SAFE DELETE PLAN — NO DELETION")
    print(f"Source scan            : {source}")
    print(f"Total keywords         : {len(rows):,}")
    print(f"Raw delete candidates  : {all_candidates:,}")
    print(f"SAFE_DELETE            : {action_counts['SAFE_DELETE']:,}")
    print(f"GROUP_GUARD_HOLD       : {action_counts['GROUP_GUARD_HOLD']:,}")
    print(f"Projected after delete : {projected_keywords:,}")
    print(f"Min survivors/group    : {min_survivors}")
    print(f"Max delete/group       : {max_delete_rate:.0%}")
    print(f"Groups                 : {len(grouped):,}")
    print(f"Plan CSV               : {plan_path}")
    print(f"Approved-only CSV      : {approved_path}")
    print(f"Group summary CSV      : {summary_path}")
    print("")
    print("No keyword was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
