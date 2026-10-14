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

import run_v2_scan
from restricted_cleanup import load_limited_keyword_ids_from_master
from src.keyword_cleaner.lifecycle_store import LifecycleStore
from src.keyword_cleaner.live_safety import exposure_ok, keyword_exposure_ok, stats_range
from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from src.keyword_cleaner.policy_v2 import CleanerPolicy, keyword_tier, normalize
from src.keyword_cleaner.stats_v2 import get_verified_keyword_stats


KST = ZoneInfo("Asia/Seoul")
BACKUP_DIR = Path("data/backups")
AUDIT_DIR = Path("data/delete_audit")
STOP_FILE = Path("data/state/auto_cleanup.stop")

DELETE_CHUNK_SIZE = 50
PREVALIDATION_RESERVE = 50000
FINAL_FLOOR_RECOUNT_MARGIN = 100
MAX_DELETE_ERRORS = 3
PER_GROUP_DELETE_CAP = 0.50
MIN_GROUP_SURVIVORS = 4

MANIFEST_FIELDS = [
    "checked_at", "lane", "campaign_name", "campaign_id", "adgroup_name", "adgroup_id",
    "keyword", "keyword_id", "age_days", "tier", "scan_status", "current_status",
    "current_inspect_status", "recent_impressions", "recent_clicks", "click_window_clicks",
    "history_impressions", "history_clicks", "gate_result", "gate_reason", "archive_id",
    "delete_result", "verify_result",
]

SUMMARY_FIELDS = [
    "master_initial_status", "master_final_status", "started_at", "finished_at", "scan_keywords", "live_keywords_before", "cleanup_stop",
    "target_removals", "candidate_pool", "bulk_safe_candidates", "final_ready", "deleted",
    "verified", "delete_errors", "verify_failures", "estimated_keywords_after", "stop_reason",
    "exposure_diag_top", "exposure_diag_path", "cleanup_mode", "max_delete",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production one-click safe keyword cleanup.")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--max-delete", type=int, default=0)
    parser.add_argument(
        "--mode",
        choices=("safe", "aggressive", "very_aggressive"),
        default="safe",
    )
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def latest_scan() -> Path:
    files = sorted(BACKUP_DIR.glob("v2_scan_*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("V2 scan output was not created.")
    return files[-1]


def write_rows(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_post_cleanup_scan(
    *,
    scan_rows: list[dict[str, str]],
    manifest_rows: list[dict[str, object]],
    stamp: str,
    expected_total: int,
) -> Path | None:
    """Publish a dashboard-ready scan snapshot after verified deletions.

    The classifications of surviving rows come from the full scan performed at
    the start of this same cleanup run. Only keyword IDs whose DELETE was
    post-verified as absent are removed. If another process changed account
    membership concurrently and the resulting row count no longer agrees with
    our verified live counter, no synthetic scan is published.
    """
    verified_ids = {
        str(row.get("keyword_id", "")).strip()
        for row in manifest_rows
        if row.get("verify_result") == "ABSENT_OK" and row.get("keyword_id")
    }
    if not verified_ids or not scan_rows:
        return None

    filtered = [
        row for row in scan_rows
        if str(row.get("keyword_id", "")).strip() not in verified_ids
    ]
    if len(filtered) != int(expected_total):
        print(
            f"@@AUTO_DASHBOARD_REFRESH|SKIP|{len(filtered)}|{int(expected_total)}",
            flush=True,
        )
        return None

    path = BACKUP_DIR / f"v2_scan_postcleanup_{stamp}.csv"
    fields = list(scan_rows[0].keys())
    write_rows(path, filtered, fields)
    print(f"@@AUTO_DASHBOARD_REFRESH|OK|{len(filtered)}|{int(expected_total)}", flush=True)
    return path


def write_cleanup_summary(
    *,
    stamp: str,
    started_at: datetime,
    scan_keywords: int,
    live_keywords_before: int,
    cleanup_stop: int,
    target_removals: int,
    candidate_pool: int,
    bulk_safe_candidates: int,
    final_ready: int,
    deleted: int,
    verified: int,
    delete_errors: int,
    verify_failures: int,
    estimated_keywords_after: int,
    stop_reason: str,
    master_initial_status: str = "NOT_RUN",
    master_final_status: str = "NOT_RUN",
    exposure_diag_top: str = "",
    exposure_diag_path: str = "",
    cleanup_mode: str = "safe",
    max_delete: int = 0,
) -> Path:
    summary = {
        "master_initial_status": master_initial_status,
        "master_final_status": master_final_status,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(KST).isoformat(),
        "scan_keywords": scan_keywords,
        "live_keywords_before": live_keywords_before,
        "cleanup_stop": cleanup_stop,
        "target_removals": target_removals,
        "candidate_pool": candidate_pool,
        "bulk_safe_candidates": bulk_safe_candidates,
        "final_ready": final_ready,
        "deleted": deleted,
        "verified": verified,
        "delete_errors": delete_errors,
        "verify_failures": verify_failures,
        "estimated_keywords_after": estimated_keywords_after,
        "stop_reason": stop_reason,
        "exposure_diag_top": exposure_diag_top,
        "exposure_diag_path": exposure_diag_path,
        "cleanup_mode": cleanup_mode,
        "max_delete": max_delete,
    }
    path = AUDIT_DIR / f"auto_cleanup_summary_{stamp}.csv"
    write_rows(path, [summary], SUMMARY_FIELDS)
    print(f"Summary                 : {path}")
    return path


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        raw.strip()
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#")
    ]


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def _inspect_approved(value: object) -> bool:
    return str(value or "").strip().upper() in {"APPROVED", "20"}


def _timestamp_age_days(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return max((datetime.now(KST).date() - parsed.astimezone(KST).date()).days, 0)


def _live_keyword_blocked_approved(keyword: dict) -> bool:
    """Conservative direct-API fallback for a keyword-level system block.

    Parent campaign/adgroup health is checked separately. A row qualifies here
    only when the keyword itself is not currently ELIGIBLE, inspection is
    already APPROVED(20), and the operator did not user-lock it. This matches
    the stable "중지: 키워드 노출 제한" shape without relying on undocumented
    statusReason strings or the flaky Keyword Master download.
    """
    if keyword.get("userLock") is True:
        return False
    status = str(keyword.get("status", "")).strip().upper()
    if not status or status == "ELIGIBLE":
        return False
    return _inspect_approved(keyword.get("inspectStatus"))


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


def cleanup_stage_policies(base: CleanerPolicy, cleanup_mode: str) -> list[tuple[str, CleanerPolicy]]:
    stages: list[tuple[str, CleanerPolicy]] = [("strict", base)]

    def add(name: str, inactivity: int, clicks: int, history: int) -> None:
        candidate = base.with_updates(
            general_inactivity_days=min(base.general_inactivity_days, inactivity),
            click_protection_days=min(base.click_protection_days, clicks),
            reference_history_days=min(base.reference_history_days, history),
        )
        signature = (
            candidate.general_inactivity_days,
            candidate.click_protection_days,
            candidate.reference_history_days,
        )
        existing = {
            (
                policy.general_inactivity_days,
                policy.click_protection_days,
                policy.reference_history_days,
            )
            for _, policy in stages
        }
        if signature not in existing:
            stages.append((name, candidate))

    if cleanup_mode in {"aggressive", "very_aggressive"}:
        add("expand1", 21, 45, 60)
        add("expand2", 14, 30, 45)
    if cleanup_mode == "very_aggressive":
        add("expand3", 7, 21, 30)
    return stages


def policy_for_row(base: CleanerPolicy, row: dict[str, str]) -> CleanerPolicy:
    return base.with_updates(
        general_inactivity_days=as_int(
            row.get("_policy_inactivity_days"), base.general_inactivity_days
        ),
        click_protection_days=as_int(
            row.get("_policy_click_days"), base.click_protection_days
        ),
        reference_history_days=as_int(
            row.get("_policy_history_days"), base.reference_history_days
        ),
    )


def assign_policy_stage(
    row: dict[str, str], stage_name: str, policy: CleanerPolicy
) -> dict[str, str]:
    out = dict(row)
    out["_policy_stage"] = stage_name
    out["_policy_inactivity_days"] = str(policy.general_inactivity_days)
    out["_policy_click_days"] = str(policy.click_protection_days)
    out["_policy_history_days"] = str(policy.reference_history_days)
    return out


def try_keyword_master(
    client: NaverSearchAdsClient, *, phase: str, max_attempts: int = 3
) -> tuple[set[str], bool]:
    """Fetch an authoritative restriction list, or explicitly disable that lane.

    The keyword master is not treated as an empty, successfully verified list
    when the Naver report-download endpoint fails. This distinction ensures
    that no RESTRICTED deletion uses an unverified/stale report.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            limited = load_limited_keyword_ids_from_master(client)
            print(f"@@AUTO_MASTER_STATUS|{phase}|OK|{len(limited)}", flush=True)
            return limited, True
        except NaverSearchAdsError as exc:
            print(
                f"[MASTER WARNING] {phase} attempt {attempt}/{max_attempts} failed: {exc}",
                flush=True,
            )
            if attempt < max_attempts:
                print(f"@@AUTO_MASTER_STATUS|{phase}|RETRY|{attempt}", flush=True)
                time.sleep(3)

    print(f"@@AUTO_MASTER_STATUS|{phase}|SKIP|0", flush=True)
    print(
        "[MASTER SAFE HOLD] Keyword Master could not be verified. "
        "Master-only restriction rows are held. Direct API BLOCKED rows and "
        "other mode-eligible GENERAL rows may still proceed through complete "
        "activity revalidation.",
        flush=True,
    )
    return set(), False


def fetch_account_snapshot(client: NaverSearchAdsClient, *, label: str) -> dict[str, object]:
    campaign_rows = client.get_campaigns()
    campaigns = {str(row.get("nccCampaignId", "")): row for row in campaign_rows}

    groups: list[dict] = []
    adgroups: dict[str, dict] = {}
    for campaign in campaign_rows:
        cid = str(campaign.get("nccCampaignId", ""))
        if not cid:
            continue
        for raw_group in client.get_adgroups(cid):
            group = dict(raw_group)
            group["_campaign_id"] = cid
            gid = str(group.get("nccAdgroupId", ""))
            if gid:
                groups.append(group)
                adgroups[gid] = group

    keywords: dict[str, dict] = {}
    group_keyword_ids: dict[str, list[str]] = defaultdict(list)
    total_groups = len(groups)
    total_keywords = 0

    for index, group in enumerate(groups, start=1):
        gid = str(group.get("nccAdgroupId", ""))
        rows = client.get_keywords(gid)
        total_keywords += len(rows)
        for raw in rows:
            current = dict(raw)
            kid = str(current.get("nccKeywordId", "")).strip()
            if kid:
                keywords[kid] = current
                group_keyword_ids[gid].append(kid)
        if index == 1 or index == total_groups or index % 25 == 0:
            print(
                f"@@AUTO_ACCOUNT_PROGRESS|{label}|{index}|{total_groups}|{total_keywords}",
                flush=True,
            )

    return {
        "campaigns": campaigns,
        "adgroups": adgroups,
        "keywords": keywords,
        "group_keyword_ids": dict(group_keyword_ids),
        "total_keywords": total_keywords,
    }


def candidate_current_ok(
    *,
    row: dict[str, str],
    lane: str,
    snapshot: dict[str, object],
    policy: CleanerPolicy,
    permanent_keywords: list[str],
    type_keywords: list[str],
    limited_ids: set[str],
    cleanup_mode: str = "safe",
) -> tuple[bool, str]:
    kid = str(row.get("keyword_id", "")).strip()
    current = snapshot["keywords"].get(kid)  # type: ignore[index]
    if not current:
        return False, "keyword_missing"

    gid = str(row.get("adgroup_id", ""))
    cid = str(row.get("campaign_id", ""))
    if str(current.get("nccAdgroupId", "")) != gid:
        return False, "adgroup_changed"
    if normalize(str(current.get("keyword", ""))) != normalize(str(row.get("keyword", ""))):
        return False, "keyword_text_changed"

    campaign = snapshot["campaigns"].get(cid)  # type: ignore[index]
    adgroup = snapshot["adgroups"].get(gid)  # type: ignore[index]
    if not campaign or not adgroup:
        return False, "parent_missing"
    if not exposure_ok(campaign):
        return False, "campaign_not_eligible"

    adgroup_status = str(adgroup.get("status", "")).strip().upper()
    if lane in {"PAUSED_GROUP", "PAUSED_GROUP_STALE"}:
        if cleanup_mode not in {"aggressive", "very_aggressive"}:
            return False, "paused_group_not_enabled"
        if adgroup_status != "PAUSED":
            return False, f"paused_group_state_changed:{adgroup_status or 'UNKNOWN'}"
        if adgroup.get("userLock") is True:
            return False, "paused_group_user_locked"
        adgroup_edit_age = _timestamp_age_days(adgroup.get("editTm"))
        if (
            adgroup_edit_age is not None
            and adgroup_edit_age < policy.general_protection_days
        ):
            return False, f"paused_group_recent_edit:{adgroup_edit_age}d"
    elif not exposure_ok(adgroup):
        return False, "adgroup_not_eligible"

    tier, tier_reason = keyword_tier(
        adgroup_name=str(adgroup.get("name", "")),
        keyword=str(current.get("keyword", "")),
        policy=policy,
        manual_permanent_keywords=permanent_keywords,
        manual_type_core_keywords=type_keywords,
    )
    if tier != "GENERAL":
        return False, f"tier_changed:{tier}:{tier_reason}"

    edit_age = _timestamp_age_days(current.get("editTm"))
    if edit_age is not None and edit_age < policy.general_protection_days:
        return False, f"recent_edit_protection:{edit_age}d"

    if lane == "RESTRICTED":
        if current.get("userLock") is True:
            return False, "keyword_user_locked"
        if kid not in limited_ids:
            return False, "restriction_not_in_master"
        if as_int(row.get("age_days")) < policy.reference_history_days:
            return False, f"restricted_age_under_{policy.reference_history_days}d"
    elif lane == "BLOCKED":
        if as_int(row.get("age_days")) < policy.reference_history_days:
            return False, f"blocked_age_under_{policy.reference_history_days}d"
        if not _live_keyword_blocked_approved(current):
            return False, "blocked_state_not_confirmed"
    elif lane == "PAUSED_GROUP":
        if as_int(row.get("age_days")) < policy.reference_history_days:
            return False, f"paused_group_age_under_{policy.reference_history_days}d"
        if current.get("userLock") is True:
            return False, "keyword_user_locked"
        if not _inspect_approved(current.get("inspectStatus")):
            return False, "paused_group_keyword_not_approved"
    elif lane == "PAUSED_GROUP_STALE":
        stale_age = max(90, policy.reference_history_days * 2)
        if as_int(row.get("age_days")) < stale_age:
            return False, f"paused_group_stale_age_under_{stale_age}d"
        if current.get("userLock") is True:
            return False, "keyword_user_locked"
        inspect = str(current.get("inspectStatus", "")).strip().upper()
        if not inspect:
            return False, "paused_group_stale_inspect_unknown"
        if _inspect_approved(inspect):
            return False, "paused_group_stale_became_approved"
    else:
        # A keyword known to be exposure-limited must NEVER flow through the
        # ordinary lane, even if the normal keyword API calls it ELIGIBLE.
        if kid in limited_ids:
            return False, "restricted_keyword_not_general"
        managed = current.get("managedKeyword")
        if isinstance(managed, dict) and managed.get("isRestricted") is True:
            return False, "managed_keyword_is_restricted"
        reason = str(current.get("statusReason", "")).strip().upper()
        if "RESTRICT" in reason or "노출제한" in reason:
            return False, "keyword_restriction_status"
        if not keyword_exposure_ok(current):
            return False, "current_exposure_not_eligible"

    return True, "current_state_ok"


def build_exposure_diagnostic(
    *,
    scan_rows: list[dict[str, str]],
    snapshot: dict[str, object],
    stamp: str,
    reference_history_days: int,
) -> tuple[Path, list[tuple[str, int]]]:
    """Summarize the real NAVER state shape for old GENERAL exposure-blocked rows."""
    counts: Counter[str] = Counter()
    for row in scan_rows:
        if (
            row.get("tier") != "GENERAL"
            or as_int(row.get("age_days")) < reference_history_days
            or str(row.get("exposure_eligible", "")).lower() != "false"
        ):
            continue

        kid = str(row.get("keyword_id", "")).strip()
        current = snapshot["keywords"].get(kid, {})  # type: ignore[index]
        campaign = snapshot["campaigns"].get(str(row.get("campaign_id", "")), {})  # type: ignore[index]
        adgroup = snapshot["adgroups"].get(str(row.get("adgroup_id", "")), {})  # type: ignore[index]
        managed = current.get("managedKeyword")
        managed_restricted = (
            managed.get("isRestricted")
            if isinstance(managed, dict)
            else None
        )
        key = "|".join(
            [
                f"campaign={str(campaign.get('status', '')).strip() or 'EMPTY'}",
                f"adgroup={str(adgroup.get('status', '')).strip() or 'EMPTY'}",
                f"keyword={str(current.get('status', '')).strip() or 'EMPTY'}",
                f"inspect={str(current.get('inspectStatus', '')).strip() or 'EMPTY'}",
                f"reason={str(current.get('statusReason', '')).strip() or 'EMPTY'}",
                f"userLock={current.get('userLock')}",
                f"isRestricted={managed_restricted}",
            ]
        )
        counts[key] += 1

    rows = [
        {"count": count, "state_shape": shape}
        for shape, count in counts.most_common()
    ]
    path = AUDIT_DIR / f"exposure_state_diagnostic_{stamp}.csv"
    write_rows(path, rows, ["count", "state_shape"])
    top = counts.most_common(8)
    print(f"@@AUTO_EXPOSURE_DIAG_FILE|{path}", flush=True)
    for shape, count in top:
        print(f"@@AUTO_EXPOSURE_DIAG|{count}|{shape}", flush=True)
    return path, top


def group_allowances(snapshot: dict[str, object]) -> dict[str, int]:
    result: dict[str, int] = {}
    for gid, ids in snapshot["group_keyword_ids"].items():  # type: ignore[union-attr]
        total = len(ids)
        result[gid] = max(
            min(math.floor(total * PER_GROUP_DELETE_CAP), total - MIN_GROUP_SURVIVORS),
            0,
        )
    return result


def build_candidate_pool(
    *,
    scan_rows: list[dict[str, str]],
    snapshot: dict[str, object],
    limited_ids: set[str],
    policy: CleanerPolicy,
    permanent_keywords: list[str],
    type_keywords: list[str],
    reserve_limit: int,
    master_available: bool,
    cleanup_mode: str = "safe",
) -> tuple[list[dict[str, str]], dict[str, str]]:
    restricted_ids = {
        str(row.get("keyword_id", ""))
        for row in scan_rows
        if row.get("tier") == "GENERAL"
        and as_int(row.get("age_days")) >= policy.reference_history_days
        and str(row.get("keyword_id", "")) in limited_ids
    }

    # Direct keyword-API fallback/supplement. This deliberately does NOT accept
    # every exposure_eligible=False row. It only accepts an old GENERAL keyword
    # whose parents are healthy and whose own status is non-ELIGIBLE while
    # inspectStatus is already APPROVED(20). User-locked rows never qualify.
    blocked_ids: set[str] = set()
    for row in scan_rows:
        kid = str(row.get("keyword_id", "")).strip()
        if (
            not kid
            or row.get("tier") != "GENERAL"
            or as_int(row.get("age_days")) < policy.reference_history_days
            or kid in restricted_ids
        ):
            continue
        current = snapshot["keywords"].get(kid)  # type: ignore[index]
        campaign = snapshot["campaigns"].get(str(row.get("campaign_id", "")))  # type: ignore[index]
        adgroup = snapshot["adgroups"].get(str(row.get("adgroup_id", "")))  # type: ignore[index]
        if (
            current
            and campaign
            and adgroup
            and exposure_ok(campaign)
            and exposure_ok(adgroup)
            and _live_keyword_blocked_approved(current)
        ):
            blocked_ids.add(kid)

    print(
        f"@@AUTO_BLOCKED_FALLBACK|{len(blocked_ids)}|{len(restricted_ids)}|"
        f"{'MASTER_OK' if master_available else 'MASTER_UNAVAILABLE'}",
        flush=True,
    )

    paused_group_ids: set[str] = set()
    if cleanup_mode in {"aggressive", "very_aggressive"}:
        for row in scan_rows:
            kid = str(row.get("keyword_id", "")).strip()
            if (
                not kid
                or row.get("tier") != "GENERAL"
                or as_int(row.get("age_days")) < policy.reference_history_days
                or kid in restricted_ids
                or kid in blocked_ids
            ):
                continue
            current = snapshot["keywords"].get(kid)  # type: ignore[index]
            campaign = snapshot["campaigns"].get(str(row.get("campaign_id", "")))  # type: ignore[index]
            adgroup = snapshot["adgroups"].get(str(row.get("adgroup_id", "")))  # type: ignore[index]
            if (
                current
                and campaign
                and adgroup
                and exposure_ok(campaign)
                and str(adgroup.get("status", "")).strip().upper() == "PAUSED"
                and adgroup.get("userLock") is not True
                and current.get("userLock") is not True
                and _inspect_approved(current.get("inspectStatus"))
            ):
                paused_group_ids.add(kid)

    print(
        f"@@AUTO_PAUSED_GROUP|{len(paused_group_ids)}|{cleanup_mode}",
        flush=True,
    )

    raw_candidates: list[dict[str, str]] = []
    for row in scan_rows:
        kid = str(row.get("keyword_id", ""))
        if not kid or row.get("tier") != "GENERAL":
            continue

        if kid in limited_ids and kid not in restricted_ids:
            # Known exposure-limited but not old enough for the restricted
            # lane. Never reclassify as ordinary GENERAL.
            continue
        if kid in restricted_ids:
            out = dict(row)
            out["_lane"] = "RESTRICTED"
            raw_candidates.append(out)
        elif kid in blocked_ids:
            out = dict(row)
            out["_lane"] = "BLOCKED"
            raw_candidates.append(out)
        elif kid in paused_group_ids:
            out = dict(row)
            out["_lane"] = "PAUSED_GROUP"
            raw_candidates.append(out)
        elif (
            row.get("status") in {"DELETE_PENDING", "DELETE_APPROVED"}
            and str(row.get("exposure_eligible", "")).lower() == "true"
        ):
            # When Keyword Master is unavailable, UI-only exposure restriction
            # flags cannot be authoritatively identified. Keep the ordinary lane
            # running, but only for keywords at least 90 days old so an unknown
            # restricted keyword can never pass under a weaker age rule.
            if (
                not master_available
                and as_int(row.get("age_days")) < policy.reference_history_days
            ):
                continue
            out = dict(row)
            out["_lane"] = "GENERAL"
            raw_candidates.append(out)

    raw_candidates.sort(
        key=lambda row: (
            0 if row.get("_lane") in {"RESTRICTED", "BLOCKED", "PAUSED_GROUP"} else 1,
            0 if row.get("status") == "DELETE_APPROVED" else 1,
            -as_int(row.get("age_days")),
            row.get("campaign_name", ""),
            row.get("adgroup_name", ""),
            row.get("keyword", ""),
        )
    )

    allowance = group_allowances(snapshot)
    used: Counter[str] = Counter()
    prelim: list[dict[str, str]] = []
    held: dict[str, str] = {}

    for row in raw_candidates:
        kid = str(row.get("keyword_id", ""))
        lane = str(row.get("_lane", "GENERAL"))
        ok, reason = candidate_current_ok(
            row=row,
            lane=lane,
            snapshot=snapshot,
            policy=policy,
            permanent_keywords=permanent_keywords,
            type_keywords=type_keywords,
            limited_ids=limited_ids,
            cleanup_mode=cleanup_mode,
        )
        if not ok:
            held[kid] = reason
            continue

        gid = str(row.get("adgroup_id", ""))
        if used[gid] >= allowance.get(gid, 0):
            held[kid] = "group_safety_cap"
            continue

        prelim.append(row)
        used[gid] += 1
        if len(prelim) >= reserve_limit:
            break

    return prelim, held


def bulk_stats_gate(
    *,
    client: NaverSearchAdsClient,
    rows: list[dict[str, str]],
    policy: CleanerPolicy,
    phase: str,
) -> tuple[
    list[dict[str, str]],
    dict[str, str],
    dict[str, tuple[int | None, int | None, int | None, int | None, int | None]],
]:
    ids = [str(row.get("keyword_id", "")) for row in rows if row.get("keyword_id")]
    if not ids:
        return [], {}, {}

    recent_since, recent_until = stats_range(policy.general_inactivity_days)
    click_since, click_until = stats_range(policy.click_protection_days)
    history_since, history_until = stats_range(policy.reference_history_days)

    print(f"@@AUTO_PHASE|{phase}|{policy.general_inactivity_days}d|{len(ids)}", flush=True)
    recent = get_verified_keyword_stats(client, ids, since=recent_since, until=recent_until)
    print(f"@@AUTO_PHASE|{phase}|{policy.click_protection_days}d|{len(ids)}", flush=True)
    clicks = get_verified_keyword_stats(client, ids, since=click_since, until=click_until)
    print(f"@@AUTO_PHASE|{phase}|{policy.reference_history_days}d|{len(ids)}", flush=True)
    history = get_verified_keyword_stats(client, ids, since=history_since, until=history_until)

    safe: list[dict[str, str]] = []
    held: dict[str, str] = {}
    stats_map: dict[
        str, tuple[int | None, int | None, int | None, int | None, int | None]
    ] = {}

    for row in rows:
        kid = str(row.get("keyword_id", ""))
        r = recent.get(kid)
        c = clicks.get(kid)
        h = history.get(kid)
        stats_map[kid] = (
            r.impressions if r else None,
            r.clicks if r else None,
            c.clicks if c else None,
            h.impressions if h else None,
            h.clicks if h else None,
        )
        if not r or not c or not h or not r.complete or not c.complete or not h.complete:
            held[kid] = "stats_revalidation_incomplete"
        elif (r.impressions or 0) != 0 or (r.clicks or 0) != 0:
            held[kid] = f"recent_{policy.general_inactivity_days}d_activity_detected"
        elif (c.clicks or 0) != 0:
            held[kid] = f"click_within_{policy.click_protection_days}d"
        elif (h.impressions or 0) != 0 or (h.clicks or 0) != 0:
            held[kid] = f"history_within_{policy.reference_history_days}d"
        else:
            safe.append(row)

    return safe, held, stats_map


def target_seeking_stats_gate(
    *,
    client: NaverSearchAdsClient,
    rows: list[dict[str, str]],
    base_policy: CleanerPolicy,
    cleanup_mode: str,
    phase: str,
    target: int,
    snapshot: dict[str, object],
    permanent_keywords: list[str],
    type_keywords: list[str],
    limited_ids: set[str],
) -> tuple[
    list[dict[str, str]],
    dict[str, str],
    dict[str, tuple[int | None, int | None, int | None, int | None, int | None]],
]:
    stages = cleanup_stage_policies(base_policy, cleanup_mode)
    remaining = list(rows)
    accepted: list[dict[str, str]] = []
    held: dict[str, str] = {}
    stats_map: dict[
        str, tuple[int | None, int | None, int | None, int | None, int | None]
    ] = {}

    for index, (stage_name, stage_policy) in enumerate(stages, start=1):
        if not remaining or len(accepted) >= target:
            break
        print(
            f"@@AUTO_EXPANSION_STAGE|{phase}|{index}|{stage_name}|"
            f"{stage_policy.general_inactivity_days}|"
            f"{stage_policy.click_protection_days}|"
            f"{stage_policy.reference_history_days}|{len(remaining)}|{target}",
            flush=True,
        )
        stage_rows: list[dict[str, str]] = []
        retry_later: list[dict[str, str]] = []
        for row in remaining:
            kid = str(row.get("keyword_id", ""))
            ok, reason = candidate_current_ok(
                row=row,
                lane=str(row.get("_lane", "GENERAL")),
                snapshot=snapshot,
                policy=stage_policy,
                permanent_keywords=permanent_keywords,
                type_keywords=type_keywords,
                limited_ids=limited_ids,
                cleanup_mode=cleanup_mode,
            )
            if ok:
                stage_rows.append(row)
            else:
                held[kid] = reason
                if "age_under_" in reason:
                    retry_later.append(row)

        stage_safe, stage_holds, stage_stats = bulk_stats_gate(
            client=client,
            rows=stage_rows,
            policy=stage_policy,
            phase=f"{phase}_S{index}",
        )
        stats_map.update(stage_stats)
        safe_ids = {
            str(row.get("keyword_id", ""))
            for row in stage_safe
            if row.get("keyword_id")
        }
        for row in stage_safe:
            accepted.append(assign_policy_stage(row, stage_name, stage_policy))
            held.pop(str(row.get("keyword_id", "")), None)

        next_remaining: list[dict[str, str]] = list(retry_later)
        retry_ids = {
            str(row.get("keyword_id", ""))
            for row in retry_later
            if row.get("keyword_id")
        }
        for row in stage_rows:
            kid = str(row.get("keyword_id", ""))
            if kid in safe_ids:
                continue
            reason = stage_holds.get(kid, "stage_not_passed")
            held[kid] = reason
            if reason == "stats_revalidation_incomplete":
                continue
            if kid not in retry_ids:
                next_remaining.append(row)
        remaining = next_remaining

    return accepted, held, stats_map


def assigned_stage_stats_gate(
    *,
    client: NaverSearchAdsClient,
    rows: list[dict[str, str]],
    base_policy: CleanerPolicy,
    phase: str,
) -> tuple[
    list[dict[str, str]],
    dict[str, str],
    dict[str, tuple[int | None, int | None, int | None, int | None, int | None]],
]:
    grouped: dict[tuple[int, int, int], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        row_policy = policy_for_row(base_policy, row)
        key = (
            row_policy.general_inactivity_days,
            row_policy.click_protection_days,
            row_policy.reference_history_days,
        )
        grouped[key].append(row)

    safe_ids: set[str] = set()
    held: dict[str, str] = {}
    stats_map: dict[
        str, tuple[int | None, int | None, int | None, int | None, int | None]
    ] = {}

    for index, ((inactivity, clicks, history), group_rows) in enumerate(
        grouped.items(), start=1
    ):
        stage_policy = base_policy.with_updates(
            general_inactivity_days=inactivity,
            click_protection_days=clicks,
            reference_history_days=history,
        )
        stage_safe, stage_holds, stage_stats = bulk_stats_gate(
            client=client,
            rows=group_rows,
            policy=stage_policy,
            phase=f"{phase}_S{index}",
        )
        safe_ids.update(
            str(row.get("keyword_id", ""))
            for row in stage_safe
            if row.get("keyword_id")
        )
        held.update(stage_holds)
        stats_map.update(stage_stats)

    safe = [
        row for row in rows
        if str(row.get("keyword_id", "")) in safe_ids
    ]
    return safe, held, stats_map


def select_target_subset(
    *, rows: list[dict[str, str]], snapshot: dict[str, object], target: int
) -> list[dict[str, str]]:
    allowance = group_allowances(snapshot)
    used: Counter[str] = Counter()
    selected: list[dict[str, str]] = []

    for row in rows:
        gid = str(row.get("adgroup_id", ""))
        if used[gid] >= allowance.get(gid, 0):
            continue
        selected.append(row)
        used[gid] += 1
        if len(selected) >= target:
            break
    return selected


def make_manifest_rows(
    *,
    rows: list[dict[str, str]],
    snapshot: dict[str, object],
    stats_map: dict[str, tuple[int | None, int | None, int | None, int | None, int | None]],
    held: dict[str, str],
    selected_ids: set[str],
) -> list[dict[str, object]]:
    checked_at = datetime.now(KST).isoformat()
    out: list[dict[str, object]] = []

    for row in rows:
        kid = str(row.get("keyword_id", ""))
        current = snapshot["keywords"].get(kid, {})  # type: ignore[index]
        stat = stats_map.get(kid, (None, None, None, None, None))
        selected = kid in selected_ids
        out.append(
            {
                "checked_at": checked_at,
                "lane": row.get("_lane", "GENERAL"),
                "campaign_name": row.get("campaign_name", ""),
                "campaign_id": row.get("campaign_id", ""),
                "adgroup_name": row.get("adgroup_name", ""),
                "adgroup_id": row.get("adgroup_id", ""),
                "keyword": row.get("keyword", ""),
                "keyword_id": kid,
                "age_days": row.get("age_days", ""),
                "tier": row.get("tier", ""),
                "scan_status": row.get("status", ""),
                "current_status": current.get("status", ""),
                "current_inspect_status": current.get("inspectStatus", ""),
                "recent_impressions": stat[0],
                "recent_clicks": stat[1],
                "click_window_clicks": stat[2],
                "history_impressions": stat[3],
                "history_clicks": stat[4],
                "gate_result": "READY" if selected else "HOLD",
                "gate_reason": (
                    "bulk_live_gates_passed"
                    if selected
                    else held.get(kid, "not_selected_for_target")
                ),
                "archive_id": "",
                "delete_result": "NOT_ATTEMPTED",
                "verify_result": "NOT_ATTEMPTED",
            }
        )
    return out


def update_manifest(
    manifest_by_id: dict[str, dict[str, object]], kid: str, **changes: object
) -> None:
    row = manifest_by_id.get(kid)
    if row:
        row.update(changes)


def verify_chunk(
    *,
    client: NaverSearchAdsClient,
    deleted_by_group: dict[str, list[str]],
    store: LifecycleStore,
    archive_by_id: dict[str, int],
    manifest_by_id: dict[str, dict[str, object]],
) -> tuple[int, int]:
    verified = 0
    failures = 0

    for gid, ids in deleted_by_group.items():
        remaining = {
            str(row.get("nccKeywordId", ""))
            for row in client.get_keywords(gid)
        }
        for kid in ids:
            gone = kid not in remaining
            store.mark_archive_verified(
                archive_by_id[kid],
                gone=gone,
                error=None if gone else "keyword still present after DELETE response",
            )
            update_manifest(
                manifest_by_id,
                kid,
                verify_result="ABSENT_OK" if gone else "STILL_PRESENT",
            )
            if gone:
                verified += 1
            else:
                failures += 1
    return verified, failures


def run_cleanup(*, live: bool, max_delete: int = 0, cleanup_mode: str = "safe") -> int:
    started_at = datetime.now(KST)
    run_stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    load_dotenv()
    policy, permanent_keywords, type_keywords = load_policy_and_manual()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STOP_FILE.unlink()
    except FileNotFoundError:
        pass

    print("=" * 84)
    print("DAYLAW PRODUCTION SAFE CLEANUP")
    print("=" * 84)
    print(f"Mode                    : {'LIVE DELETE' if live else 'DRY RUN'}")
    print(f"Cleanup start           : {policy.cleanup_start:,}")
    print(f"Cleanup stop            : {policy.cleanup_stop:,}")
    print(f"Checkpoint size         : {DELETE_CHUNK_SIZE:,}")
    print(f"Cleanup mode            : {cleanup_mode}")
    print(f"Run delete cap          : {max_delete:,}" if max_delete > 0 else "Run delete cap          : policy target")

    print("@@AUTO_STAGE|SCAN|0", flush=True)
    rc = int(run_v2_scan.main() or 0)
    if rc != 0:
        write_cleanup_summary(
            stamp=run_stamp,
            started_at=started_at,
            scan_keywords=0,
            live_keywords_before=0,
            cleanup_stop=policy.cleanup_stop,
            target_removals=0,
            candidate_pool=0,
            bulk_safe_candidates=0,
            final_ready=0,
            deleted=0,
            verified=0,
            delete_errors=0,
            verify_failures=0,
            estimated_keywords_after=0,
            stop_reason="scan_data_quality_failed" if rc == 6 else "scan_failed",
            cleanup_mode=cleanup_mode,
            max_delete=max_delete,
        )
        return rc

    scan_path = latest_scan()
    scan_rows = read_csv(scan_path)
    scan_total = len(scan_rows)
    print(f"@@AUTO_STAGE|SCAN_DONE|{scan_total}", flush=True)

    if max_delete <= 0 and scan_total < policy.cleanup_start:
        print(
            f"[SAFE STOP] {scan_total:,} keywords is below cleanup start "
            f"{policy.cleanup_start:,}. Nothing deleted."
        )
        write_cleanup_summary(
            stamp=run_stamp,
            started_at=started_at,
            scan_keywords=scan_total,
            live_keywords_before=scan_total,
            cleanup_stop=policy.cleanup_stop,
            target_removals=0,
            candidate_pool=0,
            bulk_safe_candidates=0,
            final_ready=0,
            deleted=0,
            verified=0,
            delete_errors=0,
            verify_failures=0,
            estimated_keywords_after=scan_total,
            stop_reason="below_cleanup_start",
            cleanup_mode=cleanup_mode,
            max_delete=max_delete,
        )
        return 0

    client = NaverSearchAdsClient(NaverConfig.from_env(), min_interval_seconds=0.20)

    print("@@AUTO_STAGE|MASTER|0", flush=True)
    limited_ids, master_available = try_keyword_master(client, phase="INITIAL")
    print(f"@@AUTO_STAGE|MASTER_DONE|{len(limited_ids)}", flush=True)
    if not master_available:
        print(
            "[FALLBACK] Keyword Master unavailable. Direct keyword-state BLOCKED lane "
            "and settings-qualified GENERAL safety flow remain available.",
            flush=True,
        )

    print("@@AUTO_STAGE|ACCOUNT_SNAPSHOT|0", flush=True)
    snapshot = fetch_account_snapshot(client, label="PRECHECK")
    live_before = int(snapshot["total_keywords"])
    target = max(live_before - policy.cleanup_stop, 0)
    if max_delete > 0:
        target = min(target, max_delete)
    stage_policies = cleanup_stage_policies(policy, cleanup_mode)
    structural_policy = stage_policies[-1][1]
    exposure_diag_path, exposure_diag_top = build_exposure_diagnostic(
        scan_rows=scan_rows,
        snapshot=snapshot,
        stamp=run_stamp,
        reference_history_days=structural_policy.reference_history_days,
    )

    if target <= 0:
        print("[SAFE STOP] Live account is already at/below cleanup stop.")
        write_cleanup_summary(
            stamp=run_stamp,
            started_at=started_at,
            scan_keywords=scan_total,
            live_keywords_before=live_before,
            cleanup_stop=policy.cleanup_stop,
            target_removals=0,
            candidate_pool=0,
            bulk_safe_candidates=0,
            final_ready=0,
            deleted=0,
            verified=0,
            delete_errors=0,
            verify_failures=0,
            estimated_keywords_after=live_before,
            stop_reason="cleanup_stop_reached",
            master_initial_status="OK" if master_available else "SKIPPED",
            cleanup_mode=cleanup_mode,
            max_delete=max_delete,
        )
        return 0

    reserve_limit = min(
        PREVALIDATION_RESERVE,
        max(target + 5000, math.ceil(target * 2.5)),
    )
    prelim, current_holds = build_candidate_pool(
        scan_rows=scan_rows,
        snapshot=snapshot,
        limited_ids=limited_ids,
        policy=structural_policy,
        permanent_keywords=permanent_keywords,
        type_keywords=type_keywords,
        reserve_limit=reserve_limit,
        master_available=master_available,
        cleanup_mode=cleanup_mode,
    )
    print(f"@@AUTO_STAGE|CANDIDATES|{len(prelim)}|{target}", flush=True)

    if not prelim:
        print("[SAFE STOP] No current GENERAL candidate passed the structural gate.")
        write_cleanup_summary(
            stamp=run_stamp,
            started_at=started_at,
            scan_keywords=scan_total,
            live_keywords_before=live_before,
            cleanup_stop=policy.cleanup_stop,
            target_removals=target,
            candidate_pool=0,
            bulk_safe_candidates=0,
            final_ready=0,
            deleted=0,
            verified=0,
            delete_errors=0,
            verify_failures=0,
            estimated_keywords_after=live_before,
            stop_reason="no_structural_candidates",
            master_initial_status="OK" if master_available else "SKIPPED",
            master_final_status="NOT_RUN",
            exposure_diag_top=" || ".join(
                f"{count:,}개:{shape}" for shape, count in exposure_diag_top[:5]
            ),
            exposure_diag_path=str(exposure_diag_path),
            cleanup_mode=cleanup_mode,
            max_delete=max_delete,
        )
        return 0

    bulk_safe, stat_holds, stats_map = target_seeking_stats_gate(
        client=client,
        rows=prelim,
        base_policy=policy,
        cleanup_mode=cleanup_mode,
        phase="PRECHECK",
        target=target,
        snapshot=snapshot,
        permanent_keywords=permanent_keywords,
        type_keywords=type_keywords,
        limited_ids=limited_ids,
    )
    holds = {**current_holds, **stat_holds}
    selected = select_target_subset(rows=bulk_safe, snapshot=snapshot, target=target)
    selected_ids = {str(row.get("keyword_id", "")) for row in selected}

    manifest_rows = make_manifest_rows(
        rows=prelim,
        snapshot=snapshot,
        stats_map=stats_map,
        held=holds,
        selected_ids=selected_ids,
    )
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    manifest_path = AUDIT_DIR / f"auto_cleanup_manifest_{stamp}.csv"
    write_rows(manifest_path, manifest_rows, MANIFEST_FIELDS)

    print(f"Candidate pool          : {len(prelim):,}")
    print(f"Bulk safe candidates    : {len(bulk_safe):,}")
    print(f"Selected toward target  : {len(selected):,}/{target:,}")
    print(f"Pre-delete manifest     : {manifest_path}")

    if not live:
        print("[DRY RUN] Nothing was deleted.")
        return 0
    if not selected:
        print("[SAFE STOP] No keyword passed the configured activity-history gates.")
        write_cleanup_summary(
            stamp=run_stamp,
            started_at=started_at,
            scan_keywords=scan_total,
            live_keywords_before=live_before,
            cleanup_stop=policy.cleanup_stop,
            target_removals=target,
            candidate_pool=len(prelim),
            bulk_safe_candidates=len(bulk_safe),
            final_ready=0,
            deleted=0,
            verified=0,
            delete_errors=0,
            verify_failures=0,
            estimated_keywords_after=live_before,
            stop_reason="no_stats_safe_candidates",
            master_initial_status="OK" if master_available else "SKIPPED",
            master_final_status="NOT_RUN",
            cleanup_mode=cleanup_mode,
            max_delete=max_delete,
        )
        return 0

    print("@@AUTO_STAGE|FINAL_SNAPSHOT|0", flush=True)
    final_snapshot = fetch_account_snapshot(client, label="FINAL")
    final_live_count = int(final_snapshot["total_keywords"])
    final_target = max(final_live_count - policy.cleanup_stop, 0)
    if max_delete > 0:
        final_target = min(final_target, max_delete)
    if final_target <= 0:
        print("[SAFE STOP] Fresh live count is already at/below cleanup stop.")
        return 0

    final_master_available = master_available
    if any(row.get("_lane") == "RESTRICTED" for row in selected):
        print("@@AUTO_STAGE|FINAL_MASTER|0", flush=True)
        checked_limited_ids, final_master_available = try_keyword_master(
            client, phase="FINAL"
        )
        # Preserve the initial report's restricted IDs as exclusions even if
        # the final report omits one (status changed) or was unavailable.
        final_limited_ids = limited_ids | checked_limited_ids
    else:
        final_limited_ids = limited_ids

    structural_ready: list[dict[str, str]] = []
    final_holds: dict[str, str] = {}
    for row in selected:
        lane = str(row.get("_lane", "GENERAL"))
        if lane == "RESTRICTED" and not final_master_available:
            final_holds[str(row.get("keyword_id", ""))] = "final_master_unavailable"
            continue
        if lane == "RESTRICTED" and str(row.get("keyword_id", "")) not in checked_limited_ids:
            final_holds[str(row.get("keyword_id", ""))] = "restriction_not_confirmed_in_final_master"
            continue
        row_policy = policy_for_row(policy, row)
        ok, reason = candidate_current_ok(
            row=row,
            lane=lane,
            snapshot=final_snapshot,
            policy=row_policy,
            permanent_keywords=permanent_keywords,
            type_keywords=type_keywords,
            limited_ids=final_limited_ids,
            cleanup_mode=cleanup_mode,
        )
        kid = str(row.get("keyword_id", ""))
        if ok:
            structural_ready.append(row)
        else:
            final_holds[kid] = reason

    final_safe, final_stat_holds, final_stats_map = assigned_stage_stats_gate(
        client=client,
        rows=structural_ready,
        base_policy=policy,
        phase="FINAL",
    )
    final_holds.update(final_stat_holds)
    final_ready = select_target_subset(
        rows=final_safe,
        snapshot=final_snapshot,
        target=final_target,
    )

    manifest_by_id = {
        str(row.get("keyword_id", "")): row
        for row in manifest_rows
        if row.get("keyword_id")
    }
    final_ready_ids = {str(row.get("keyword_id", "")) for row in final_ready}

    for row in selected:
        kid = str(row.get("keyword_id", ""))
        current = final_snapshot["keywords"].get(kid, {})  # type: ignore[index]
        stat = final_stats_map.get(kid)
        changes: dict[str, object] = {
            "current_status": current.get("status", ""),
            "current_inspect_status": current.get("inspectStatus", ""),
        }
        if stat:
            changes.update(
                recent_impressions=stat[0],
                recent_clicks=stat[1],
                click_window_clicks=stat[2],
                history_impressions=stat[3],
                history_clicks=stat[4],
            )
        if kid in final_ready_ids:
            changes.update(gate_result="READY", gate_reason="final_bulk_live_gates_passed")
        else:
            changes.update(
                gate_result="HOLD",
                gate_reason=final_holds.get(kid, "final_target_or_group_cap"),
            )
        update_manifest(manifest_by_id, kid, **changes)

    write_rows(manifest_path, manifest_rows, MANIFEST_FIELDS)
    print(f"Final READY             : {len(final_ready):,}/{final_target:,}")

    if not final_ready:
        print("[SAFE STOP] Final live revalidation produced no READY keyword.")
        write_cleanup_summary(
            stamp=run_stamp,
            started_at=started_at,
            scan_keywords=scan_total,
            live_keywords_before=live_before,
            cleanup_stop=policy.cleanup_stop,
            target_removals=target,
            candidate_pool=len(prelim),
            bulk_safe_candidates=len(bulk_safe),
            final_ready=0,
            deleted=0,
            verified=0,
            delete_errors=0,
            verify_failures=0,
            estimated_keywords_after=final_live_count,
            stop_reason="no_final_ready_candidates",
            master_initial_status="OK" if master_available else "SKIPPED",
            master_final_status="OK" if final_master_available else "SKIPPED",
            cleanup_mode=cleanup_mode,
            max_delete=max_delete,
        )
        return 0

    final_ready.sort(
        key=lambda row: (
            0 if row.get("_lane") in {"RESTRICTED", "BLOCKED", "PAUSED_GROUP"} else 1,
            row.get("adgroup_id", ""),
            -as_int(row.get("age_days")),
            row.get("keyword", ""),
        )
    )

    store = LifecycleStore()
    deleted = 0
    verified = 0
    delete_errors = 0
    verify_failures = 0
    floor_refreshed = False
    stop_reason = "completed_ready_subset"
    archive_by_id: dict[str, int] = {}

    try:
        for offset in range(0, len(final_ready), DELETE_CHUNK_SIZE):
            if STOP_FILE.exists():
                stop_reason = "user_stop_requested"
                break
            if final_live_count <= policy.cleanup_stop:
                stop_reason = "cleanup_stop_reached"
                break

            remaining_to_floor = final_live_count - policy.cleanup_stop
            if not floor_refreshed and remaining_to_floor <= FINAL_FLOOR_RECOUNT_MARGIN:
                refreshed = fetch_account_snapshot(client, label="FLOOR")
                final_live_count = int(refreshed["total_keywords"])
                floor_refreshed = True
                if final_live_count <= policy.cleanup_stop:
                    stop_reason = "cleanup_stop_reached_after_recount"
                    break

            chunk = final_ready[offset : offset + DELETE_CHUNK_SIZE]
            chunk = chunk[: max(final_live_count - policy.cleanup_stop, 0)]
            if not chunk:
                stop_reason = "cleanup_stop_reached"
                break

            # Re-read the exact groups touched by this checkpoint. Long production
            # runs can last many minutes, so keyword status/text is checked again
            # immediately before DELETE rather than trusting the earlier snapshot.
            chunk_by_group: Counter[str] = Counter(
                str(row.get("adgroup_id", "")) for row in chunk
            )
            checkpoint_keywords: dict[str, dict] = {}
            checkpoint_group_ids: dict[str, list[str]] = {}
            for gid in chunk_by_group:
                current_rows = client.get_keywords(gid)
                checkpoint_group_ids[gid] = []
                for current in current_rows:
                    kid = str(current.get("nccKeywordId", "")).strip()
                    if kid:
                        checkpoint_keywords[kid] = current
                        checkpoint_group_ids[gid].append(kid)

            checkpoint_snapshot = {
                "campaigns": final_snapshot["campaigns"],
                "adgroups": final_snapshot["adgroups"],
                "keywords": checkpoint_keywords,
                "group_keyword_ids": checkpoint_group_ids,
                "total_keywords": final_live_count,
            }
            checkpoint_ready: list[dict[str, str]] = []
            for row in chunk:
                kid = str(row.get("keyword_id", ""))
                row_policy = policy_for_row(policy, row)
                ok, reason = candidate_current_ok(
                    row=row,
                    lane=str(row.get("_lane", "GENERAL")),
                    snapshot=checkpoint_snapshot,
                    policy=row_policy,
                    permanent_keywords=permanent_keywords,
                    type_keywords=type_keywords,
                    limited_ids=final_limited_ids,
                    cleanup_mode=cleanup_mode,
                )
                if ok:
                    checkpoint_ready.append(row)
                else:
                    update_manifest(
                        manifest_by_id,
                        kid,
                        gate_result="HOLD",
                        gate_reason="checkpoint:" + reason,
                    )

            checkpoint_by_group: Counter[str] = Counter(
                str(row.get("adgroup_id", "")) for row in checkpoint_ready
            )
            for gid, planned in checkpoint_by_group.items():
                current_total = len(checkpoint_group_ids.get(gid, []))
                if planned > math.floor(current_total * PER_GROUP_DELETE_CAP):
                    raise SystemExit(
                        f"[SAFE STOP] Group {gid} would exceed the 50% checkpoint delete cap."
                    )
                if current_total - planned < MIN_GROUP_SURVIVORS:
                    raise SystemExit(
                        f"[SAFE STOP] Group {gid} would fall below {MIN_GROUP_SURVIVORS} survivors."
                    )

            deleted_by_group: defaultdict[str, list[str]] = defaultdict(list)
            for row in checkpoint_ready:
                if STOP_FILE.exists():
                    stop_reason = "user_stop_requested"
                    break
                if final_live_count <= policy.cleanup_stop:
                    stop_reason = "cleanup_stop_reached"
                    break

                kid = str(row.get("keyword_id", ""))
                current = checkpoint_keywords.get(kid, {})
                identity_key = "|".join(
                    (
                        str(row.get("campaign_id", "")),
                        str(row.get("adgroup_id", "")),
                        normalize(str(row.get("keyword", ""))),
                    )
                )
                row_policy = policy_for_row(policy, row)
                payload = {
                    "plan": row,
                    "keyword_before_delete": current,
                    "auto_cleanup": {
                        "lane": row.get("_lane", "GENERAL"),
                        "policy_stage": row.get("_policy_stage", "strict"),
                        "required_windows": (
                            f"{row_policy.general_inactivity_days}d/"
                            f"{row_policy.click_protection_days}d/"
                            f"{row_policy.reference_history_days}d zero"
                        ),
                        "checkpoint_size": DELETE_CHUNK_SIZE,
                        "cleanup_stop": policy.cleanup_stop,
                    },
                }

                archive_id = store.prepare_keyword_archive(
                    identity_key=identity_key,
                    keyword_id=kid,
                    campaign_id=str(row.get("campaign_id", "")),
                    adgroup_id=str(row.get("adgroup_id", "")),
                    keyword=str(row.get("keyword", "")),
                    tier="GENERAL",
                    delete_reason=(
                        "AUTO_RESTRICTED_STAGED_ZERO_LIVE_REVALIDATED"
                        if row.get("_lane") == "RESTRICTED"
                        else (
                            "AUTO_BLOCKED_APPROVED_HISTORY_ZERO_LIVE_REVALIDATED"
                            if row.get("_lane") == "BLOCKED"
                            else (
                                "AUTO_PAUSED_GROUP_HISTORY_ZERO_LIVE_REVALIDATED"
                                if row.get("_lane") == "PAUSED_GROUP"
                                else "AUTO_GENERAL_STAGED_HISTORY_LIVE_REVALIDATED"
                            )
                        )
                    ),
                    payload=payload,
                )
                archive_by_id[kid] = archive_id
                update_manifest(manifest_by_id, kid, archive_id=archive_id)

                store.mark_archive_delete_requested(archive_id)
                try:
                    client.delete_keyword(kid)
                except NaverSearchAdsError as exc:
                    store.mark_archive_delete_failed(archive_id, str(exc))
                    update_manifest(
                        manifest_by_id,
                        kid,
                        delete_result=f"ERROR:{exc}",
                        verify_result="NOT_ATTEMPTED",
                    )
                    delete_errors += 1
                    if delete_errors >= MAX_DELETE_ERRORS:
                        stop_reason = "delete_error_limit_reached"
                        break
                    continue

                store.mark_archive_delete_api_ok(archive_id)
                update_manifest(manifest_by_id, kid, delete_result="DELETED")
                deleted_by_group[str(row.get("adgroup_id", ""))].append(kid)
                deleted += 1
                final_live_count -= 1
                print(
                    f"@@AUTO_DELETE_PROGRESS|{deleted}|{len(final_ready)}|{final_live_count}",
                    flush=True,
                )

            chunk_verified, chunk_failures = verify_chunk(
                client=client,
                deleted_by_group=dict(deleted_by_group),
                store=store,
                archive_by_id=archive_by_id,
                manifest_by_id=manifest_by_id,
            )
            verified += chunk_verified
            verify_failures += chunk_failures
            write_rows(manifest_path, manifest_rows, MANIFEST_FIELDS)

            print(
                f"@@AUTO_CHECKPOINT|{deleted}|{verified}|{delete_errors}|"
                f"{verify_failures}|{final_live_count}",
                flush=True,
            )
            if verify_failures:
                stop_reason = "post_delete_verification_failed"
                break
            if delete_errors >= MAX_DELETE_ERRORS:
                break

        if final_live_count <= policy.cleanup_stop:
            stop_reason = "cleanup_stop_reached"
    finally:
        store.close()
        write_rows(manifest_path, manifest_rows, MANIFEST_FIELDS)

    post_cleanup_scan = write_post_cleanup_scan(
        scan_rows=scan_rows,
        manifest_rows=manifest_rows,
        stamp=stamp,
        expected_total=final_live_count,
    )

    finished_at = datetime.now(KST)
    summary = {
        "master_initial_status": "OK" if master_available else "SKIPPED",
        "master_final_status": "OK" if final_master_available else "SKIPPED",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "scan_keywords": scan_total,
        "live_keywords_before": live_before,
        "cleanup_stop": policy.cleanup_stop,
        "target_removals": target,
        "candidate_pool": len(prelim),
        "bulk_safe_candidates": len(bulk_safe),
        "final_ready": len(final_ready),
        "deleted": deleted,
        "verified": verified,
        "delete_errors": delete_errors,
        "verify_failures": verify_failures,
        "estimated_keywords_after": final_live_count,
        "stop_reason": stop_reason,
        "exposure_diag_top": " || ".join(
            f"{count:,}개:{shape}" for shape, count in exposure_diag_top[:5]
        ),
        "exposure_diag_path": str(exposure_diag_path),
        "cleanup_mode": cleanup_mode,
        "max_delete": max_delete,
    }
    summary_path = AUDIT_DIR / f"auto_cleanup_summary_{stamp}.csv"
    write_rows(summary_path, [summary], SUMMARY_FIELDS)

    print("=" * 84)
    print("AUTO CLEANUP RESULT")
    print(f"Deleted                 : {deleted:,}")
    print(f"Post-delete verified    : {verified:,}")
    print(f"Delete errors           : {delete_errors:,}")
    print(f"Verification failures   : {verify_failures:,}")
    print(f"Estimated live keywords : {final_live_count:,}")
    print(f"Stop reason             : {stop_reason}")
    print(f"Manifest                : {manifest_path}")
    print(f"Summary                 : {summary_path}")

    if verify_failures:
        return 4
    if delete_errors >= MAX_DELETE_ERRORS:
        return 5
    return 0


def main() -> int:
    args = parse_args()
    live = bool(args.delete)
    if live and args.confirm != "AUTO_CLEAN":
        raise SystemExit("Live auto cleanup requires --delete --confirm AUTO_CLEAN")
    max_delete = max(int(args.max_delete or 0), 0)
    return run_cleanup(
        live=live,
        max_delete=max_delete,
        cleanup_mode=str(args.mode or "safe"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
