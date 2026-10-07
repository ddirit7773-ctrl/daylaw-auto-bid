from __future__ import annotations

import argparse
import csv
import math
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.lifecycle_store import LifecycleStore
from src.keyword_cleaner.live_safety import count_account_keywords_live, exposure_ok, stats_range
from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from src.keyword_cleaner.policy_v2 import CleanerPolicy, keyword_tier, normalize
from src.keyword_cleaner.stats_v2 import get_singular_verified_keyword_stat


KST = ZoneInfo("Asia/Seoul")
BACKUP_DIR = Path("data/backups")
AUDIT_DIR = Path("data/delete_audit")

MAX_SCAN_AGE_MINUTES = 120
MAX_REVIEW_AGE_MINUTES = 30
MIN_RESTRICTED_AGE_DAYS = 90
MAX_BATCH = 20
PER_GROUP_DELETE_CAP = 0.50
MIN_GROUP_SURVIVORS = 4

REVIEW_FIELDS = [
    "checked_at",
    "campaign_name",
    "campaign_id",
    "adgroup_name",
    "adgroup_id",
    "keyword",
    "keyword_id",
    "age_days",
    "tier",
    "scan_keyword_status",
    "scan_inspect_status",
    "current_keyword_status",
    "current_inspect_status",
    "history_impressions",
    "history_clicks",
    "gate_result",
    "gate_reason",
    "archive_id",
    "delete_result",
    "verify_result",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safe cleanup lane for long-term keyword exposure restrictions."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--review", action="store_true")
    mode.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--max-items", type=int, default=MAX_BATCH)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def latest_file(folder: Path, pattern: str) -> Path:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"No file found for {pattern}")
    return files[-1]


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            out.append(value)
    return out


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def text_upper(value: object) -> str:
    return str(value or "").strip().upper()


def is_limited_inspect_status(value: object) -> bool:
    """Return True for Naver's keyword exposure-limit inspection state.

    Naver exposes the keyword-level restriction through Ad Keyword Inspect
    Status 30 / LIMITED_APPROVED.  The keyword's ordinary status may still be
    ELIGIBLE, so that field must not be used to discover this special lane.
    """
    return text_upper(value) in {"LIMITED_APPROVED", "30"}


def scan_restricted_candidate(row: dict[str, str]) -> bool:
    """Conservative discovery from the latest V2 scan.

    The special lane is identified by LIMITED_APPROVED / 30 itself. Parent
    campaign/adgroup must be healthy; only old GENERAL keywords are considered.
    Keyword status is intentionally not required to be non-ELIGIBLE because the
    API can report an otherwise eligible keyword while its inspect status carries
    the exposure restriction.
    """
    return bool(
        row.get("tier") == "GENERAL"
        and as_int(row.get("age_days")) >= MIN_RESTRICTED_AGE_DAYS
        and text_upper(row.get("campaign_status")) == "ELIGIBLE"
        and text_upper(row.get("adgroup_status")) == "ELIGIBLE"
        and is_limited_inspect_status(row.get("inspect_status"))
    )


def load_policy_and_manual() -> tuple[CleanerPolicy, list[str], list[str]]:
    policy = CleanerPolicy.load()
    permanent_keywords = load_lines(Path("config/protected_keywords.txt"))
    permanent_suffixes = load_lines(Path("config/protected_suffixes.txt"))
    type_keywords = load_lines(Path("config/type_core_keywords.txt"))
    type_suffixes = load_lines(Path("config/type_core_suffixes.txt"))
    policy = policy.with_updates(
        permanent_suffixes=tuple(dict.fromkeys((*policy.permanent_suffixes, *permanent_suffixes))),
        type_core_suffixes=tuple(dict.fromkeys((*policy.type_core_suffixes, *type_suffixes))),
    )
    return policy, permanent_keywords, type_keywords


def current_maps(
    client: NaverSearchAdsClient,
    rows: list[dict[str, str]],
) -> tuple[dict[str, dict], dict[str, dict]]:
    campaigns = {
        str(row.get("nccCampaignId", "")): row for row in client.get_campaigns()
    }
    needed_campaign_ids = {
        str(row.get("campaign_id", "")).strip() for row in rows if row.get("campaign_id")
    }
    adgroups: dict[str, dict] = {}
    for campaign_id in needed_campaign_ids:
        for group in client.get_adgroups(campaign_id):
            adgroups[str(group.get("nccAdgroupId", ""))] = group
    return campaigns, adgroups


def restricted_live_gate(
    *,
    client: NaverSearchAdsClient,
    row: dict[str, str],
    policy: CleanerPolicy,
    campaigns: dict[str, dict],
    adgroups: dict[str, dict],
    permanent_keywords: list[str],
    type_keywords: list[str],
) -> tuple[bool, str, dict | None, int | None, int | None]:
    kid = str(row.get("keyword_id", "")).strip()
    try:
        current = client.get_keyword(kid)
    except NaverSearchAdsError as exc:
        return False, f"keyword_lookup_failed:{exc}", None, None, None

    if normalize(str(current.get("keyword", ""))) != normalize(str(row.get("keyword", ""))):
        return False, "keyword_text_changed", current, None, None
    if str(current.get("nccAdgroupId", "")) != str(row.get("adgroup_id", "")):
        return False, "adgroup_changed", current, None, None

    campaign = campaigns.get(str(row.get("campaign_id", "")))
    adgroup = adgroups.get(str(row.get("adgroup_id", "")))
    if not campaign or not adgroup:
        return False, "parent_not_found", current, None, None

    # Parent campaign/ad group must be healthy. A parent pause must never be
    # mistaken for a keyword-level exposure restriction.
    if not exposure_ok(campaign) or not exposure_ok(adgroup):
        return False, "parent_not_eligible", current, None, None

    # Manually OFF keywords are excluded from this lane. We only handle
    # keyword-level system restriction while the keyword itself remains ON.
    if current.get("userLock") is True:
        return False, "keyword_user_locked", current, None, None

    # The exposure restriction is carried by inspectStatus 30 /
    # LIMITED_APPROVED. Do not require ordinary keyword status to be PAUSED:
    # Naver can keep status ELIGIBLE while inspectStatus represents the limit.
    inspect_status = text_upper(current.get("inspectStatus"))
    if not is_limited_inspect_status(inspect_status):
        return False, f"restriction_cleared:{inspect_status or 'UNKNOWN'}", current, None, None

    current_tier, tier_reason = keyword_tier(
        adgroup_name=str(adgroup.get("name", "")),
        keyword=str(current.get("keyword", "")),
        policy=policy,
        manual_permanent_keywords=permanent_keywords,
        manual_type_core_keywords=type_keywords,
    )
    if current_tier != "GENERAL":
        return False, f"tier_changed:{current_tier}:{tier_reason}", current, None, None

    # Restriction itself can create 0/0. Therefore the special lane never uses
    # a short-window 0/0 as enough proof. It requires age >= 90d AND a complete
    # 90-day history of 0 impressions / 0 clicks.
    if as_int(row.get("age_days")) < MIN_RESTRICTED_AGE_DAYS:
        return False, "age_under_90d", current, None, None

    since, until = stats_range(policy.reference_history_days)
    history = get_singular_verified_keyword_stat(
        client,
        kid,
        since=since,
        until=until,
    )
    if not history.complete:
        return False, "history_stats_incomplete", current, history.impressions, history.clicks
    if (history.impressions or 0) != 0 or (history.clicks or 0) != 0:
        return (
            False,
            "history_90d_activity_detected",
            current,
            history.impressions,
            history.clicks,
        )

    return True, "restricted_general_90d_zero", current, history.impressions, history.clicks


def run_review(max_items: int) -> int:
    scan_path = latest_file(BACKUP_DIR, "v2_scan_*.csv")
    age_minutes = (time.time() - scan_path.stat().st_mtime) / 60
    if age_minutes > MAX_SCAN_AGE_MINUTES:
        raise SystemExit(
            f"Latest V2 scan is {age_minutes:.0f} minutes old. Run a new V2 scan first."
        )

    scan_rows = read_csv(scan_path)
    candidates = [row for row in scan_rows if scan_restricted_candidate(row)]
    candidates.sort(
        key=lambda row: (
            -as_int(row.get("age_days")),
            row.get("campaign_name", ""),
            row.get("adgroup_name", ""),
            row.get("keyword", ""),
        )
    )
    selected = candidates[:max_items]

    print("=" * 78)
    print("RESTRICTED KEYWORD SAFE REVIEW — NO DELETION")
    print("=" * 78)
    print(f"Latest scan             : {scan_path}")
    print(f"Restricted GENERAL pool : {len(candidates):,}")
    print(f"Selected for review     : {len(selected):,}")
    print(f"Minimum keyword age     : {MIN_RESTRICTED_AGE_DAYS} days")
    print("Required history        : 90d = 0 impressions / 0 clicks")

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    out_path = AUDIT_DIR / f"restricted_review_{stamp}.csv"

    if not selected:
        write_csv(out_path, [], REVIEW_FIELDS)
        print("[SAFE STOP] No restricted GENERAL candidate matched the conservative signature.")
        print(f"Review CSV              : {out_path}")
        return 0

    policy, permanent_keywords, type_keywords = load_policy_and_manual()
    client = NaverSearchAdsClient(NaverConfig.from_env(), min_interval_seconds=0.20)
    campaigns, adgroups = current_maps(client, selected)

    selected_by_group = Counter(row.get("adgroup_id", "") for row in selected)
    blocked_group_ids: set[str] = set()
    for gid, planned in selected_by_group.items():
        rows = client.get_keywords(gid)
        total = len(rows)
        if planned > math.floor(total * PER_GROUP_DELETE_CAP) or total - planned < MIN_GROUP_SURVIVORS:
            blocked_group_ids.add(gid)

    audit: list[dict[str, object]] = []
    for index, row in enumerate(selected, start=1):
        print(f"@@RESTRICTED_PROGRESS|{index}|{len(selected)}", flush=True)
        gid = row.get("adgroup_id", "")
        if gid in blocked_group_ids:
            ready = False
            reason = "group_safety_cap"
            current = None
            imp90 = None
            clk90 = None
        else:
            ready, reason, current, imp90, clk90 = restricted_live_gate(
                client=client,
                row=row,
                policy=policy,
                campaigns=campaigns,
                adgroups=adgroups,
                permanent_keywords=permanent_keywords,
                type_keywords=type_keywords,
            )
        current = current or {}
        audit.append(
            {
                "checked_at": datetime.now(KST).isoformat(),
                "campaign_name": row.get("campaign_name", ""),
                "campaign_id": row.get("campaign_id", ""),
                "adgroup_name": row.get("adgroup_name", ""),
                "adgroup_id": gid,
                "keyword": row.get("keyword", ""),
                "keyword_id": row.get("keyword_id", ""),
                "age_days": row.get("age_days", ""),
                "tier": row.get("tier", ""),
                "scan_keyword_status": row.get("keyword_status", ""),
                "scan_inspect_status": row.get("inspect_status", ""),
                "current_keyword_status": current.get("status", ""),
                "current_inspect_status": current.get("inspectStatus", ""),
                "history_impressions": imp90,
                "history_clicks": clk90,
                "gate_result": "READY" if ready else "HOLD",
                "gate_reason": reason,
                "archive_id": "",
                "delete_result": "NOT_ATTEMPTED",
                "verify_result": "NOT_ATTEMPTED",
            }
        )

    write_csv(out_path, audit, REVIEW_FIELDS)
    ready_count = sum(1 for row in audit if row["gate_result"] == "READY")
    print(f"READY                   : {ready_count:,}")
    print(f"HOLD                    : {len(audit) - ready_count:,}")
    print(f"Review CSV              : {out_path}")
    print("Nothing was deleted.")
    return 0


def run_delete(max_items: int) -> int:
    review_path = latest_file(AUDIT_DIR, "restricted_review_*.csv")
    age_minutes = (time.time() - review_path.stat().st_mtime) / 60
    if age_minutes > MAX_REVIEW_AGE_MINUTES:
        raise SystemExit(
            f"Restricted review is {age_minutes:.0f} minutes old. Run restricted review again."
        )

    review_rows = read_csv(review_path)
    ready_rows = [row for row in review_rows if row.get("gate_result") == "READY"][:max_items]
    if not ready_rows:
        print("[SAFE STOP] No READY restricted keyword exists. Nothing deleted.")
        return 0

    policy, permanent_keywords, type_keywords = load_policy_and_manual()
    client = NaverSearchAdsClient(NaverConfig.from_env(), min_interval_seconds=0.20)
    campaigns, adgroups = current_maps(client, ready_rows)

    final_ready: list[tuple[dict[str, str], dict]] = []
    for index, row in enumerate(ready_rows, start=1):
        print(f"@@RESTRICTED_PROGRESS|{index}|{len(ready_rows)}", flush=True)
        ready, reason, current, imp90, clk90 = restricted_live_gate(
            client=client,
            row=row,
            policy=policy,
            campaigns=campaigns,
            adgroups=adgroups,
            permanent_keywords=permanent_keywords,
            type_keywords=type_keywords,
        )
        row["history_impressions"] = "" if imp90 is None else str(imp90)
        row["history_clicks"] = "" if clk90 is None else str(clk90)
        row["gate_reason"] = reason
        if ready and current is not None:
            final_ready.append((row, current))
        else:
            row["gate_result"] = "HOLD"

    if not final_ready:
        stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
        final_path = AUDIT_DIR / f"restricted_delete_{stamp}.csv"
        write_csv(final_path, review_rows, REVIEW_FIELDS)
        print("[SAFE STOP] All restricted rows failed final live revalidation.")
        print(f"Delete audit            : {final_path}")
        return 0

    by_group = Counter(row.get("adgroup_id", "") for row, _ in final_ready)
    for gid, planned in by_group.items():
        current_group = client.get_keywords(gid)
        total = len(current_group)
        if planned > math.floor(total * PER_GROUP_DELETE_CAP):
            raise SystemExit(f"[SAFE STOP] Group {gid} would exceed 50% delete cap.")
        if total - planned < MIN_GROUP_SURVIVORS:
            raise SystemExit(f"[SAFE STOP] Group {gid} would fall below 4 survivors.")

    print("[LIVE FLOOR] Counting current account keywords before restricted deletion...", flush=True)
    live_count = count_account_keywords_live(client)
    if live_count <= policy.cleanup_stop:
        print("[SAFE STOP] Account is already at/below cleanup stop. Nothing deleted.")
        return 0

    allowable = max(live_count - policy.cleanup_stop, 0)
    final_ready = final_ready[:allowable]
    if not final_ready:
        print("[SAFE STOP] 82,000 floor leaves no deletion capacity.")
        return 0

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    final_path = AUDIT_DIR / f"restricted_delete_{stamp}.csv"
    by_id = {row.get("keyword_id", ""): row for row in review_rows}

    store = LifecycleStore()
    deleted_by_group: defaultdict[str, list[str]] = defaultdict(list)
    deleted_ids: list[str] = []
    archive_by_id: dict[str, int] = {}

    try:
        for index, (row, current) in enumerate(final_ready, start=1):
            if live_count - 1 < policy.cleanup_stop:
                break

            kid = row.get("keyword_id", "")
            identity_key = "|".join(
                (
                    row.get("campaign_id", ""),
                    row.get("adgroup_id", ""),
                    normalize(row.get("keyword", "")),
                )
            )
            payload = {
                "plan": row,
                "keyword_before_delete": current,
                "restricted_lane": {
                    "minimum_age_days": MIN_RESTRICTED_AGE_DAYS,
                    "required_history_days": policy.reference_history_days,
                    "required_signature": "keyword_status_not_eligible_and_inspect_approved",
                },
            }

            archive_id = store.prepare_keyword_archive(
                identity_key=identity_key,
                keyword_id=kid,
                campaign_id=row.get("campaign_id", ""),
                adgroup_id=row.get("adgroup_id", ""),
                keyword=row.get("keyword", ""),
                tier="GENERAL",
                delete_reason="RESTRICTED_GENERAL_90D_ZERO_LIVE_REVALIDATED",
                payload=payload,
            )
            archive_by_id[kid] = archive_id
            if kid in by_id:
                by_id[kid]["archive_id"] = str(archive_id)

            store.mark_archive_delete_requested(archive_id)
            try:
                client.delete_keyword(kid)
            except NaverSearchAdsError as exc:
                store.mark_archive_delete_failed(archive_id, str(exc))
                if kid in by_id:
                    by_id[kid]["delete_result"] = f"ERROR:{exc}"
                break

            store.mark_archive_delete_api_ok(archive_id)
            live_count -= 1
            deleted_ids.append(kid)
            deleted_by_group[row.get("adgroup_id", "")].append(kid)
            if kid in by_id:
                by_id[kid]["delete_result"] = "DELETED"

            print(f"@@RESTRICTED_DELETE_PROGRESS|{index}|{len(final_ready)}", flush=True)

        verification_failed = False
        for gid, ids in deleted_by_group.items():
            remaining = {
                str(item.get("nccKeywordId", "")) for item in client.get_keywords(gid)
            }
            for kid in ids:
                gone = kid not in remaining
                archive_id = archive_by_id.get(kid)
                if archive_id is not None:
                    store.mark_archive_verified(
                        archive_id,
                        gone=gone,
                        error=None if gone else "keyword still present after DELETE response",
                    )
                if kid in by_id:
                    by_id[kid]["verify_result"] = "ABSENT_OK" if gone else "STILL_PRESENT"
                if not gone:
                    verification_failed = True

        write_csv(final_path, review_rows, REVIEW_FIELDS)
        print(f"Deleted                 : {len(deleted_ids):,}")
        print(f"Post-delete verified    : {len(deleted_ids):,}")
        print(f"Estimated live keywords : {live_count:,}")
        print(f"Delete audit            : {final_path}")
        if verification_failed:
            print("[WARNING] At least one restricted keyword was still present after DELETE.")
            return 4
        print("[OK] Restricted-keyword safe subset deletion completed.")
        return 0
    finally:
        store.close()


def main() -> int:
    args = parse_args()
    load_dotenv()
    max_items = max(1, min(int(args.max_items), MAX_BATCH))

    if args.delete:
        if args.confirm != "DELETE_RESTRICTED":
            raise SystemExit("Live restricted deletion requires --confirm DELETE_RESTRICTED")
        return run_delete(max_items)

    return run_review(max_items)


if __name__ == "__main__":
    raise SystemExit(main())
