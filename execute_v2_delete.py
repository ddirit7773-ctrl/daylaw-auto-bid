from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.lifecycle_store import LifecycleStore
from src.keyword_cleaner.live_safety import (
    count_account_keywords_live,
    evaluate_general_delete_gate,
    stats_range,
)
from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from src.keyword_cleaner.policy_v2 import CleanerPolicy, normalize


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


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise SystemExit(f"Refusing to write empty CSV: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            out.append(value)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Approval-gated V2 keyword delete executor. Dry-run by default."
    )
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--max-delete", type=int, default=None)
    return parser.parse_args()


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
        "history_impressions",
        "history_clicks",
        "gate_result",
        "gate_reason",
        "archive_id",
        "delete_result",
        "verify_result",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def persist_live_holds(
    *,
    scan_rows: list[dict[str, str]],
    plan_rows: list[dict[str, str]],
    blocked_reason_by_id: dict[str, str],
) -> tuple[Path, Path]:
    """Demote final-gate failures so they do not repeatedly block the next batch."""
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    new_scan_rows: list[dict[str, object]] = []
    for row in scan_rows:
        out = dict(row)
        kid = row.get("keyword_id", "")
        if kid in blocked_reason_by_id and row.get("status") == "DELETE_APPROVED":
            out["status"] = "WATCH"
            out["reason"] = "live_delete_hold:" + blocked_reason_by_id[kid]
        new_scan_rows.append(out)

    new_plan_rows: list[dict[str, object]] = []
    for row in plan_rows:
        out = dict(row)
        kid = row.get("keyword_id", "")
        if kid in blocked_reason_by_id:
            out["status"] = "WATCH"
            out["plan_action"] = "LIVE_DELETE_HOLD"
            out["plan_reason"] = (
                str(out.get("plan_reason", ""))
                + "; final live delete hold: "
                + blocked_reason_by_id[kid]
            )
        new_plan_rows.append(out)

    scan_out = BACKUP_DIR / f"v2_scan_{stamp}.csv"
    plan_out = BACKUP_DIR / f"v2_target_delete_plan_{stamp}.csv"
    write_csv(scan_out, new_scan_rows)
    write_csv(plan_out, new_plan_rows)
    return scan_out, plan_out


def main() -> int:
    args = parse_args()
    load_dotenv()
    policy = CleanerPolicy.load()

    max_delete = policy.first_test_batch if args.max_delete is None else int(args.max_delete)
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
            "Run V2 scan and rebuild the delete plan first."
        )
    if plan_path.stat().st_mtime < scan_path.stat().st_mtime:
        raise SystemExit("Delete plan is older than the latest V2 scan. Rebuild it first.")

    scan_rows = read_csv(scan_path)
    plan_rows = read_csv(plan_path)
    scan_by_id = {row.get("keyword_id", ""): row for row in scan_rows if row.get("keyword_id")}

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
    print("V2 SAFE DELETE EXECUTOR — COMMON 30D/60D/90D FINAL GATE")
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
        print("[SAFE STOP] No DELETE_APPROVED rows are executable yet.")
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
    campaigns = {str(row.get("nccCampaignId", "")): row for row in client.get_campaigns()}
    needed_campaign_ids = {row.get("campaign_id", "") for row in selected}
    adgroups: dict[str, dict] = {}
    for campaign_id in needed_campaign_ids:
        if not campaign_id:
            continue
        for group in client.get_adgroups(campaign_id):
            adgroups[str(group.get("nccAdgroupId", ""))] = group

    # Conservative group snapshot before expensive per-keyword checks.
    selected_by_group: Counter[str] = Counter(row.get("adgroup_id", "") for row in selected)
    current_group_keywords: dict[str, list[dict]] = {}
    for adgroup_id in selected_by_group:
        current_group_keywords[adgroup_id] = client.get_keywords(adgroup_id)
        total = len(current_group_keywords[adgroup_id])
        planned = selected_by_group[adgroup_id]
        if planned > math.floor(total * PER_GROUP_DELETE_CAP):
            raise SystemExit(f"[SAFE STOP] Group {adgroup_id} would exceed 50% delete cap.")
        if total - planned < MIN_GROUP_SURVIVORS:
            raise SystemExit(f"[SAFE STOP] Group {adgroup_id} would fall below 4 survivors.")

    audit_rows: list[dict[str, object]] = []
    ready: list[tuple[dict[str, str], dict]] = []
    blocked_reason_by_id: dict[str, str] = {}
    checked_at = datetime.now(KST).isoformat()

    for index, plan in enumerate(selected, start=1):
        kid = plan.get("keyword_id", "")
        print(f"@@IMMEDIATE_PROGRESS|{index}|{len(selected)}", flush=True)
        gate = evaluate_general_delete_gate(
            client=client,
            plan=plan,
            policy=policy,
            campaigns=campaigns,
            adgroups=adgroups,
            manual_permanent_keywords=manual_permanent_keywords,
            manual_type_core_keywords=manual_type_core_keywords,
        )
        current = gate.current_keyword or {}
        audit_rows.append(
            {
                "checked_at": checked_at,
                "campaign_name": plan.get("campaign_name", ""),
                "campaign_id": plan.get("campaign_id", ""),
                "adgroup_name": plan.get("adgroup_name", ""),
                "adgroup_id": plan.get("adgroup_id", ""),
                "keyword": plan.get("keyword", ""),
                "keyword_id": kid,
                "tier": plan.get("tier", ""),
                "plan_status": scan_by_id.get(kid, {}).get("status", ""),
                "current_status": current.get("status", ""),
                "current_inspect_status": current.get("inspectStatus", ""),
                "inactivity_impressions": gate.inactivity_impressions,
                "inactivity_clicks": gate.inactivity_clicks,
                "click_window_clicks": gate.click_window_clicks,
                "history_impressions": gate.history_impressions,
                "history_clicks": gate.history_clicks,
                "gate_result": "READY" if gate.ready else "BLOCK",
                "gate_reason": gate.reason,
                "archive_id": "",
                "delete_result": "NOT_ATTEMPTED",
                "verify_result": "NOT_ATTEMPTED",
            }
        )
        if gate.ready and gate.current_keyword is not None:
            ready.append((plan, gate.current_keyword))
        else:
            blocked_reason_by_id[kid] = gate.reason

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    manifest_path = AUDIT_DIR / f"v2_delete_manifest_{stamp}.csv"
    write_manifest(manifest_path, audit_rows)

    print("")
    print(f"Final gate READY        : {len(ready):,}")
    print(f"Final gate HOLD         : {len(blocked_reason_by_id):,}")
    print(f"Pre-delete manifest     : {manifest_path}")

    if not live:
        print("[DRY RUN] Nothing was deleted or modified.")
        return 0

    # Final-gate failures are held out of subsequent batches instead of blocking
    # independently safe rows. A future full scan may reconsider them.
    if blocked_reason_by_id:
        hold_scan, hold_plan = persist_live_holds(
            scan_rows=scan_rows,
            plan_rows=plan_rows,
            blocked_reason_by_id=blocked_reason_by_id,
        )
        print(f"Held-state scan         : {hold_scan}")
        print(f"Held-state plan         : {hold_plan}")

    if not ready:
        print("[SAFE STOP] No keyword passed the final 30d/60d/90d gate. Nothing deleted.")
        return 2

    # Real-time account floor guard. We deliberately recount the account from
    # Naver immediately before destructive requests. Any read failure aborts.
    print("[LIVE FLOOR] Counting current account keywords before deletion...", flush=True)
    live_keyword_count = count_account_keywords_live(client)
    print(f"[LIVE FLOOR] Current live keywords: {live_keyword_count:,}")
    if live_keyword_count <= policy.cleanup_stop:
        print("[SAFE STOP] Live account is already at/below cleanup stop. Nothing deleted.")
        return 0

    allowable = max(live_keyword_count - policy.cleanup_stop, 0)
    if len(ready) > allowable:
        ready = ready[:allowable]
        print(
            f"[LIVE FLOOR] Batch trimmed to {len(ready):,} so the account cannot cross "
            f"below {policy.cleanup_stop:,}."
        )
    if not ready:
        print("[SAFE STOP] Live floor leaves no safe deletion capacity.")
        return 0

    # Re-apply group guard to the actual READY subset.
    ready_by_group: Counter[str] = Counter(plan.get("adgroup_id", "") for plan, _ in ready)
    for adgroup_id, planned in ready_by_group.items():
        total = len(current_group_keywords.get(adgroup_id) or client.get_keywords(adgroup_id))
        if planned > math.floor(total * PER_GROUP_DELETE_CAP):
            raise SystemExit(f"[SAFE STOP] READY subset exceeds 50% delete cap for {adgroup_id}.")
        if total - planned < MIN_GROUP_SURVIVORS:
            raise SystemExit(f"[SAFE STOP] READY subset would leave fewer than 4 in {adgroup_id}.")

    inactivity_since, inactivity_until = stats_range(policy.general_inactivity_days)
    click_since, click_until = stats_range(policy.click_protection_days)
    history_since, history_until = stats_range(policy.reference_history_days)

    store = LifecycleStore()
    deleted_ids: list[str] = []
    deleted_by_group: defaultdict[str, list[str]] = defaultdict(list)
    archive_by_id: dict[str, int] = {}
    delete_error: tuple[str, str] | None = None

    try:
        for plan, current in ready:
            if live_keyword_count - 1 < policy.cleanup_stop:
                print("[SAFE STOP] Live floor reached during batch. Remaining READY rows were not touched.")
                break

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
                "inactivity_window": {"since": inactivity_since, "until": inactivity_until},
                "click_window": {"since": click_since, "until": click_until},
                "history_window": {"since": history_since, "until": history_until},
            }

            # Critical ordering: the complete restore payload is committed to
            # SQLite before any DELETE request is sent.
            archive_id = store.prepare_keyword_archive(
                identity_key=identity_key,
                keyword_id=kid,
                campaign_id=plan.get("campaign_id", ""),
                adgroup_id=plan.get("adgroup_id", ""),
                keyword=plan.get("keyword", ""),
                tier="GENERAL",
                delete_reason="V2_FINAL_30D_60D_90D_LIVE_REVALIDATED",
                payload=payload,
            )
            archive_by_id[kid] = archive_id
            for audit in audit_rows:
                if audit.get("keyword_id") == kid:
                    audit["archive_id"] = archive_id

            store.mark_archive_delete_requested(archive_id)
            try:
                client.delete_keyword(kid)
            except NaverSearchAdsError as exc:
                store.mark_archive_delete_failed(archive_id, str(exc))
                for audit in audit_rows:
                    if audit.get("keyword_id") == kid:
                        audit["delete_result"] = f"ERROR:{exc}"
                delete_error = (kid, str(exc))
                break

            store.mark_archive_delete_api_ok(archive_id)
            deleted_ids.append(kid)
            deleted_by_group[plan.get("adgroup_id", "")].append(kid)
            live_keyword_count -= 1
            for audit in audit_rows:
                if audit.get("keyword_id") == kid:
                    audit["delete_result"] = "DELETED"

        # Verify every keyword for which DELETE returned success, even when a
        # later keyword caused an emergency stop.
        verification_failed = False
        for adgroup_id, ids in deleted_by_group.items():
            remaining_ids = {
                str(row.get("nccKeywordId", "")) for row in client.get_keywords(adgroup_id)
            }
            for kid in ids:
                gone = kid not in remaining_ids
                archive_id = archive_by_id.get(kid)
                if archive_id is not None:
                    store.mark_archive_verified(
                        archive_id,
                        gone=gone,
                        error=None if gone else "keyword still present after DELETE response",
                    )
                for audit in audit_rows:
                    if audit.get("keyword_id") == kid:
                        audit["verify_result"] = "ABSENT_OK" if gone else "STILL_PRESENT"
                if not gone:
                    verification_failed = True

        write_manifest(manifest_path, audit_rows)
        print("")
        print(f"Deleted                : {len(deleted_ids):,}")
        print(f"Post-delete checked    : {len(deleted_ids):,}")
        print(f"Estimated live count   : {live_keyword_count:,}")

        if delete_error is not None:
            kid, error = delete_error
            print(f"[EMERGENCY STOP] Delete failed for {kid}: {error}")
            print("Already-deleted rows were still post-verified and their restore archives remain intact.")
            return 3
        if verification_failed:
            print("[WARNING] At least one deleted ID was still present during verification.")
            return 4
        print("[OK] Safe subset deletion and post-verification completed.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
