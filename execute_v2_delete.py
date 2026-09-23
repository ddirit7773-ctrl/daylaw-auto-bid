from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.lifecycle_store import LifecycleStore
from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from src.keyword_cleaner.policy_v2 import CleanerPolicy, keyword_tier, normalize
from src.keyword_cleaner.stats_v2 import get_singular_verified_keyword_stat


KST = ZoneInfo("Asia/Seoul")
BACKUP_DIR = Path("data/backups")
AUDIT_DIR = Path("data/delete_audit")
MAX_SCAN_AGE_MINUTES = 120
PER_GROUP_DELETE_CAP = 0.50
MIN_GROUP_SURVIVORS = 4


def latest_file(pattern: str) -> Path:
    files = sorted(BACKUP_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"No file found for {pattern}")
    return files[-1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            out.append(value)
    return out


def stats_range(days: int) -> tuple[str, str]:
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since.isoformat(), until.isoformat()


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Approval-gated V2 keyword delete executor. Dry-run by default."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete after all safety gates pass.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="Live deletion requires exactly: --confirm DELETE",
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=None,
        help="Maximum keywords for this run. Default is first-test batch from policy.",
    )
    return parser.parse_args()


def exposure_ok(row: dict) -> bool:
    if row.get("userLock") is True:
        return False
    if str(row.get("status", "")).strip().upper() != "ELIGIBLE":
        return False
    return True


def keyword_exposure_ok(row: dict) -> bool:
    return exposure_ok(row) and str(row.get("inspectStatus", "")).strip().upper() == "APPROVED"


def write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "checked_at",
        "campaign_name",
        "campaign_id",
        "adgroup_name",
        "adgroup_id",
        "keyword",
        "keyword_id",
        "tier",
        "plan_status",
        "current_status",
        "current_inspect_status",
        "inactivity_impressions",
        "inactivity_clicks",
        "click_window_clicks",
        "gate_result",
        "gate_reason",
        "delete_result",
        "verify_result",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    load_dotenv()
    policy = CleanerPolicy.load()

    max_delete = policy.first_test_batch if args.max_delete is None else args.max_delete
    if max_delete < 1:
        raise SystemExit("--max-delete must be at least 1")
    if max_delete > policy.first_test_batch:
        raise SystemExit(
            f"First live executor is capped at policy.first_test_batch={policy.first_test_batch}. "
            "Do not increase the first batch yet."
        )

    live = bool(args.delete)
    if live and args.confirm != "DELETE":
        raise SystemExit("Live deletion requires both --delete and --confirm DELETE")

    scan_path = latest_file("v2_scan_*.csv")
    plan_path = latest_file("v2_target_delete_plan_*.csv")
    scan_age_minutes = (datetime.now().timestamp() - scan_path.stat().st_mtime) / 60
    if scan_age_minutes > MAX_SCAN_AGE_MINUTES:
        raise SystemExit(
            f"Latest V2 scan is {scan_age_minutes:.0f} minutes old. "
            "Run python run_v2_scan.py and rebuild the delete plan first."
        )
    if plan_path.stat().st_mtime < scan_path.stat().st_mtime:
        raise SystemExit(
            "Delete plan is older than the latest V2 scan. Run python build_v2_delete_plan.py first."
        )

    scan_rows = read_csv(scan_path)
    plan_rows = read_csv(plan_path)
    scan_by_id = {row.get("keyword_id", ""): row for row in scan_rows if row.get("keyword_id")}

    # The plan may contain pending rows for capacity planning, but execution may
    # only touch rows that the latest scan has explicitly promoted to APPROVED.
    approved_plan: list[dict[str, str]] = []
    for row in plan_rows:
        kid = row.get("keyword_id", "")
        latest = scan_by_id.get(kid)
        if not latest:
            continue
        if row.get("tier") != "GENERAL" or latest.get("tier") != "GENERAL":
            continue
        if latest.get("status") != "DELETE_APPROVED":
            continue
        if row.get("plan_action") != "TARGET_DELETE_AFTER_APPROVAL":
            continue
        approved_plan.append(row)

    selected = approved_plan[:max_delete]

    print("=" * 76)
    print("V2 SAFE DELETE EXECUTOR")
    print("=" * 76)
    print(f"Mode                    : {'LIVE DELETE' if live else 'DRY RUN'}")
    print(f"Latest scan             : {scan_path}")
    print(f"Latest plan             : {plan_path}")
    print(f"Current scan keywords   : {len(scan_rows):,}")
    print(f"Cleanup stop            : {policy.cleanup_stop:,}")
    print(f"Approved rows in plan   : {len(approved_plan):,}")
    print(f"This run max            : {max_delete:,}")
    print(f"This run selected       : {len(selected):,}")

    if len(scan_rows) <= policy.cleanup_stop:
        print("[SAFE STOP] Account is already at/below cleanup stop. Nothing to do.")
        return 0

    if not selected:
        print("")
        print(
            f"[SAFE STOP] No DELETE_APPROVED rows are executable yet. "
            f"The {policy.pending_recheck_days}-day recheck gate is working."
        )
        print("Nothing was deleted or modified.")
        return 0

    # Never delete more than needed to reach the stop threshold.
    remaining_to_stop = max(len(scan_rows) - policy.cleanup_stop, 0)
    selected = selected[:remaining_to_stop]
    if not selected:
        print("[SAFE STOP] No removal is needed to reach cleanup stop.")
        return 0

    manual_permanent_keywords = load_lines(Path("config/protected_keywords.txt"))
    manual_permanent_suffixes = load_lines(Path("config/protected_suffixes.txt"))
    manual_type_core_keywords = load_lines(Path("config/type_core_keywords.txt"))
    manual_type_core_suffixes = load_lines(Path("config/type_core_suffixes.txt"))
    policy = policy.with_updates(
        permanent_suffixes=tuple(dict.fromkeys((*policy.permanent_suffixes, *manual_permanent_suffixes))),
        type_core_suffixes=tuple(dict.fromkeys((*policy.type_core_suffixes, *manual_type_core_suffixes))),
    )

    client = NaverSearchAdsClient(NaverConfig.from_env(), min_interval_seconds=0.20)

    # Fresh parent-state maps. A campaign/ad group turned off since the scan
    # blocks deletion instead of turning zero activity into a false signal.
    campaigns = {str(row.get("nccCampaignId", "")): row for row in client.get_campaigns()}
    needed_campaign_ids = {row.get("campaign_id", "") for row in selected}
    adgroups: dict[str, dict] = {}
    for campaign_id in needed_campaign_ids:
        if not campaign_id:
            continue
        for group in client.get_adgroups(campaign_id):
            adgroups[str(group.get("nccAdgroupId", ""))] = group

    inactivity_since, inactivity_until = stats_range(policy.general_inactivity_days)
    click_since, click_until = stats_range(policy.click_protection_days)

    # Snapshot current group sizes and enforce the same group guard again.
    selected_by_group: Counter[str] = Counter(row.get("adgroup_id", "") for row in selected)
    current_group_keywords: dict[str, list[dict]] = {}
    for adgroup_id in selected_by_group:
        current_group_keywords[adgroup_id] = client.get_keywords(adgroup_id)
        total = len(current_group_keywords[adgroup_id])
        planned = selected_by_group[adgroup_id]
        if planned > math.floor(total * PER_GROUP_DELETE_CAP):
            raise SystemExit(
                f"[SAFE STOP] Group {adgroup_id} would exceed {int(PER_GROUP_DELETE_CAP*100)}% delete cap."
            )
        if total - planned < MIN_GROUP_SURVIVORS:
            raise SystemExit(
                f"[SAFE STOP] Group {adgroup_id} would fall below {MIN_GROUP_SURVIVORS} survivors."
            )

    audit_rows: list[dict[str, object]] = []
    ready: list[tuple[dict[str, str], dict]] = []
    checked_at = datetime.now(KST).isoformat()

    for plan in selected:
        kid = plan.get("keyword_id", "")
        audit: dict[str, object] = {
            "checked_at": checked_at,
            "campaign_name": plan.get("campaign_name", ""),
            "campaign_id": plan.get("campaign_id", ""),
            "adgroup_name": plan.get("adgroup_name", ""),
            "adgroup_id": plan.get("adgroup_id", ""),
            "keyword": plan.get("keyword", ""),
            "keyword_id": kid,
            "tier": plan.get("tier", ""),
            "plan_status": scan_by_id.get(kid, {}).get("status", ""),
            "gate_result": "BLOCK",
            "gate_reason": "",
            "delete_result": "NOT_ATTEMPTED",
            "verify_result": "NOT_ATTEMPTED",
        }

        try:
            current = client.get_keyword(kid)
        except NaverSearchAdsError as exc:
            audit["gate_reason"] = f"keyword_lookup_failed:{exc}"
            audit_rows.append(audit)
            continue

        audit["current_status"] = current.get("status")
        audit["current_inspect_status"] = current.get("inspectStatus")

        if normalize(str(current.get("keyword", ""))) != normalize(plan.get("keyword", "")):
            audit["gate_reason"] = "keyword_text_changed"
            audit_rows.append(audit)
            continue
        if str(current.get("nccAdgroupId", "")) != plan.get("adgroup_id", ""):
            audit["gate_reason"] = "adgroup_changed"
            audit_rows.append(audit)
            continue

        campaign = campaigns.get(plan.get("campaign_id", ""))
        adgroup = adgroups.get(plan.get("adgroup_id", ""))
        if not campaign or not adgroup:
            audit["gate_reason"] = "parent_not_found"
            audit_rows.append(audit)
            continue
        if not exposure_ok(campaign) or not exposure_ok(adgroup) or not keyword_exposure_ok(current):
            audit["gate_reason"] = "current_exposure_not_eligible"
            audit_rows.append(audit)
            continue

        current_tier, tier_reason = keyword_tier(
            adgroup_name=str(adgroup.get("name", "")),
            keyword=str(current.get("keyword", "")),
            policy=policy,
            manual_permanent_keywords=manual_permanent_keywords,
            manual_type_core_keywords=manual_type_core_keywords,
        )
        if current_tier != "GENERAL":
            audit["gate_reason"] = f"tier_changed:{current_tier}:{tier_reason}"
            audit_rows.append(audit)
            continue

        recent = get_singular_verified_keyword_stat(
            client,
            kid,
            since=inactivity_since,
            until=inactivity_until,
        )
        clicks = get_singular_verified_keyword_stat(
            client,
            kid,
            since=click_since,
            until=click_until,
        )
        audit["inactivity_impressions"] = recent.impressions
        audit["inactivity_clicks"] = recent.clicks
        audit["click_window_clicks"] = clicks.clicks

        if not recent.complete or not clicks.complete:
            audit["gate_reason"] = "stats_revalidation_incomplete"
            audit_rows.append(audit)
            continue
        if (recent.impressions or 0) != 0 or (recent.clicks or 0) != 0:
            audit["gate_reason"] = "recent_activity_detected"
            audit_rows.append(audit)
            continue
        if (clicks.clicks or 0) != 0:
            audit["gate_reason"] = "click_protection_activity_detected"
            audit_rows.append(audit)
            continue

        audit["gate_result"] = "READY"
        audit["gate_reason"] = "all_live_gates_passed"
        audit_rows.append(audit)
        ready.append((plan, current))

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    manifest_path = AUDIT_DIR / f"v2_delete_manifest_{stamp}.csv"
    write_manifest(manifest_path, audit_rows)

    print("")
    print(f"Live-gate READY         : {len(ready):,}")
    print(f"Blocked at live gate    : {len(selected) - len(ready):,}")
    print(f"Pre-delete manifest     : {manifest_path}")

    if not live:
        print("[DRY RUN] Nothing was deleted. Use live flags only after reviewing the manifest.")
        return 0

    if len(ready) != len(selected):
        print("[SAFE STOP] At least one selected keyword failed a live gate. Nothing will be deleted.")
        return 2

    store = LifecycleStore()
    deleted_ids: list[str] = []
    deleted_by_group: defaultdict[str, list[str]] = defaultdict(list)

    try:
        for plan, current in ready:
            kid = plan.get("keyword_id", "")
            identity_key = "|".join(
                (
                    plan.get("campaign_id", ""),
                    plan.get("adgroup_id", ""),
                    normalize(plan.get("keyword", "")),
                )
            )
            payload = {
                "plan": plan,
                "keyword_before_delete": current,
                "inactivity_window": {
                    "since": inactivity_since,
                    "until": inactivity_until,
                },
                "click_window": {
                    "since": click_since,
                    "until": click_until,
                },
            }

            try:
                client.delete_keyword(kid)
            except NaverSearchAdsError as exc:
                for audit in audit_rows:
                    if audit.get("keyword_id") == kid:
                        audit["delete_result"] = f"ERROR:{exc}"
                write_manifest(manifest_path, audit_rows)
                print(f"[EMERGENCY STOP] Delete failed for {kid}: {exc}")
                return 3

            store.archive_deleted_keyword(
                identity_key=identity_key,
                keyword_id=kid,
                campaign_id=plan.get("campaign_id", ""),
                adgroup_id=plan.get("adgroup_id", ""),
                keyword=plan.get("keyword", ""),
                tier="GENERAL",
                delete_reason="V2_DELETE_APPROVED_AND_LIVE_REVALIDATED",
                payload=payload,
            )
            deleted_ids.append(kid)
            deleted_by_group[plan.get("adgroup_id", "")].append(kid)
            for audit in audit_rows:
                if audit.get("keyword_id") == kid:
                    audit["delete_result"] = "DELETED"

        # Verify by re-reading every touched ad group. Any still-present id is a
        # hard failure that is surfaced immediately.
        verification_failed = False
        for adgroup_id, ids in deleted_by_group.items():
            remaining_ids = {
                str(row.get("nccKeywordId", "")) for row in client.get_keywords(adgroup_id)
            }
            for kid in ids:
                gone = kid not in remaining_ids
                for audit in audit_rows:
                    if audit.get("keyword_id") == kid:
                        audit["verify_result"] = "ABSENT_OK" if gone else "STILL_PRESENT"
                if not gone:
                    verification_failed = True

        write_manifest(manifest_path, audit_rows)
        print("")
        print(f"Deleted                : {len(deleted_ids):,}")
        print(f"Post-delete verified   : {len(deleted_ids):,}")
        if verification_failed:
            print("[WARNING] At least one deleted ID was still present during verification.")
            return 4
        print("[OK] Batch deletion and post-verification completed.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
