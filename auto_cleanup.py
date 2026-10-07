from __future__ import annotations

import argparse
import csv
import math
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

DELETE_CHUNK_SIZE = 50
PREVALIDATION_RESERVE = 25000
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
    "started_at", "finished_at", "scan_keywords", "live_keywords_before", "cleanup_stop",
    "target_removals", "candidate_pool", "bulk_safe_candidates", "final_ready", "deleted",
    "verified", "delete_errors", "verify_failures", "estimated_keywords_after", "stop_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production one-click safe keyword cleanup.")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", default="")
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
    if not exposure_ok(campaign) or not exposure_ok(adgroup):
        return False, "parent_not_eligible"

    tier, tier_reason = keyword_tier(
        adgroup_name=str(adgroup.get("name", "")),
        keyword=str(current.get("keyword", "")),
        policy=policy,
        manual_permanent_keywords=permanent_keywords,
        manual_type_core_keywords=type_keywords,
    )
    if tier != "GENERAL":
        return False, f"tier_changed:{tier}:{tier_reason}"

    if lane == "RESTRICTED":
        if current.get("userLock") is True:
            return False, "keyword_user_locked"
        if kid not in limited_ids:
            return False, "restriction_not_in_master"
        if as_int(row.get("age_days")) < 90:
            return False, "restricted_age_under_90d"
    elif not keyword_exposure_ok(current):
        return False, "current_exposure_not_eligible"

    return True, "current_state_ok"


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
) -> tuple[list[dict[str, str]], dict[str, str]]:
    restricted_ids = {
        str(row.get("keyword_id", ""))
        for row in scan_rows
        if row.get("tier") == "GENERAL"
        and as_int(row.get("age_days")) >= 90
        and str(row.get("keyword_id", "")) in limited_ids
    }

    raw_candidates: list[dict[str, str]] = []
    for row in scan_rows:
        kid = str(row.get("keyword_id", ""))
        if not kid or row.get("tier") != "GENERAL":
            continue

        if kid in restricted_ids:
            out = dict(row)
            out["_lane"] = "RESTRICTED"
            raw_candidates.append(out)
        elif (
            row.get("status") in {"DELETE_PENDING", "DELETE_APPROVED"}
            and str(row.get("exposure_eligible", "")).lower() == "true"
        ):
            out = dict(row)
            out["_lane"] = "GENERAL"
            raw_candidates.append(out)

    raw_candidates.sort(
        key=lambda row: (
            0 if row.get("_lane") == "RESTRICTED" else 1,
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

    print(f"@@AUTO_PHASE|{phase}|30d|{len(ids)}", flush=True)
    recent = get_verified_keyword_stats(client, ids, since=recent_since, until=recent_until)
    print(f"@@AUTO_PHASE|{phase}|60d|{len(ids)}", flush=True)
    clicks = get_verified_keyword_stats(client, ids, since=click_since, until=click_until)
    print(f"@@AUTO_PHASE|{phase}|90d|{len(ids)}", flush=True)
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
            held[kid] = "recent_30d_activity_detected"
        elif (c.clicks or 0) != 0:
            held[kid] = "click_within_60d"
        elif (h.impressions or 0) != 0 or (h.clicks or 0) != 0:
            held[kid] = "history_within_90d"
        else:
            safe.append(row)

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
                    "bulk_live_gates_passed_30d_60d_90d"
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


def run_cleanup(*, live: bool) -> int:
    started_at = datetime.now(KST)
    load_dotenv()
    policy, permanent_keywords, type_keywords = load_policy_and_manual()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 84)
    print("DAYLAW PRODUCTION SAFE CLEANUP")
    print("=" * 84)
    print(f"Mode                    : {'LIVE DELETE' if live else 'DRY RUN'}")
    print(f"Cleanup start           : {policy.cleanup_start:,}")
    print(f"Cleanup stop            : {policy.cleanup_stop:,}")
    print(f"Checkpoint size         : {DELETE_CHUNK_SIZE:,}")

    print("@@AUTO_STAGE|SCAN|0", flush=True)
    rc = int(run_v2_scan.main() or 0)
    if rc != 0:
        return rc

    scan_path = latest_scan()
    scan_rows = read_csv(scan_path)
    scan_total = len(scan_rows)
    print(f"@@AUTO_STAGE|SCAN_DONE|{scan_total}", flush=True)

    if scan_total < policy.cleanup_start:
        print(
            f"[SAFE STOP] {scan_total:,} keywords is below cleanup start "
            f"{policy.cleanup_start:,}. Nothing deleted."
        )
        return 0

    client = NaverSearchAdsClient(NaverConfig.from_env(), min_interval_seconds=0.20)

    print("@@AUTO_STAGE|MASTER|0", flush=True)
    limited_ids = load_limited_keyword_ids_from_master(client)
    print(f"@@AUTO_STAGE|MASTER_DONE|{len(limited_ids)}", flush=True)

    print("@@AUTO_STAGE|ACCOUNT_SNAPSHOT|0", flush=True)
    snapshot = fetch_account_snapshot(client, label="PRECHECK")
    live_before = int(snapshot["total_keywords"])
    target = max(live_before - policy.cleanup_stop, 0)

    if target <= 0:
        print("[SAFE STOP] Live account is already at/below cleanup stop.")
        return 0

    reserve_limit = min(
        PREVALIDATION_RESERVE,
        max(target + 3000, math.ceil(target * 1.35)),
    )
    prelim, current_holds = build_candidate_pool(
        scan_rows=scan_rows,
        snapshot=snapshot,
        limited_ids=limited_ids,
        policy=policy,
        permanent_keywords=permanent_keywords,
        type_keywords=type_keywords,
        reserve_limit=reserve_limit,
    )
    print(f"@@AUTO_STAGE|CANDIDATES|{len(prelim)}|{target}", flush=True)

    if not prelim:
        print("[SAFE STOP] No current GENERAL candidate passed the structural gate.")
        return 0

    bulk_safe, stat_holds, stats_map = bulk_stats_gate(
        client=client,
        rows=prelim,
        policy=policy,
        phase="PRECHECK",
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
        print("[SAFE STOP] No keyword passed the 30d/60d/90d gate.")
        return 0

    print("@@AUTO_STAGE|FINAL_SNAPSHOT|0", flush=True)
    final_snapshot = fetch_account_snapshot(client, label="FINAL")
    final_live_count = int(final_snapshot["total_keywords"])
    final_target = max(final_live_count - policy.cleanup_stop, 0)
    if final_target <= 0:
        print("[SAFE STOP] Fresh live count is already at/below cleanup stop.")
        return 0

    if any(row.get("_lane") == "RESTRICTED" for row in selected):
        print("@@AUTO_STAGE|FINAL_MASTER|0", flush=True)
        final_limited_ids = load_limited_keyword_ids_from_master(client)
    else:
        final_limited_ids = limited_ids

    structural_ready: list[dict[str, str]] = []
    final_holds: dict[str, str] = {}
    for row in selected:
        lane = str(row.get("_lane", "GENERAL"))
        ok, reason = candidate_current_ok(
            row=row,
            lane=lane,
            snapshot=final_snapshot,
            policy=policy,
            permanent_keywords=permanent_keywords,
            type_keywords=type_keywords,
            limited_ids=final_limited_ids,
        )
        kid = str(row.get("keyword_id", ""))
        if ok:
            structural_ready.append(row)
        else:
            final_holds[kid] = reason

    final_safe, final_stat_holds, final_stats_map = bulk_stats_gate(
        client=client,
        rows=structural_ready,
        policy=policy,
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
        return 0

    final_ready.sort(
        key=lambda row: (
            row.get("adgroup_id", ""),
            0 if row.get("_lane") == "RESTRICTED" else 1,
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

            # A full-run group cap was already applied. Re-read every group touched
            # by this checkpoint to ensure it still keeps at least four survivors.
            chunk_by_group: Counter[str] = Counter(
                str(row.get("adgroup_id", "")) for row in chunk
            )
            for gid, planned in chunk_by_group.items():
                current_total = len(client.get_keywords(gid))
                if current_total - planned < MIN_GROUP_SURVIVORS:
                    raise SystemExit(
                        f"[SAFE STOP] Group {gid} would fall below {MIN_GROUP_SURVIVORS} survivors."
                    )

            deleted_by_group: defaultdict[str, list[str]] = defaultdict(list)
            for row in chunk:
                if final_live_count <= policy.cleanup_stop:
                    stop_reason = "cleanup_stop_reached"
                    break

                kid = str(row.get("keyword_id", ""))
                current = final_snapshot["keywords"].get(kid, {})  # type: ignore[index]
                identity_key = "|".join(
                    (
                        str(row.get("campaign_id", "")),
                        str(row.get("adgroup_id", "")),
                        normalize(str(row.get("keyword", ""))),
                    )
                )
                payload = {
                    "plan": row,
                    "keyword_before_delete": current,
                    "auto_cleanup": {
                        "lane": row.get("_lane", "GENERAL"),
                        "required_windows": "30d/60d/90d zero",
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
                        "AUTO_RESTRICTED_90D_ZERO_LIVE_REVALIDATED"
                        if row.get("_lane") == "RESTRICTED"
                        else "AUTO_GENERAL_30D_60D_90D_LIVE_REVALIDATED"
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

    finished_at = datetime.now(KST)
    summary = {
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
    return run_cleanup(live=live)


if __name__ == "__main__":
    raise SystemExit(main())
