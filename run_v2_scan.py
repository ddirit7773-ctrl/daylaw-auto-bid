from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.lifecycle_store import LifecycleStore
from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient
from src.keyword_cleaner.policy_v2 import (
    ActivitySnapshot,
    CleanerPolicy,
    classify_snapshot,
    keyword_tier,
    normalize,
)
from src.keyword_cleaner.stats_v2 import get_verified_keyword_stats


KST = ZoneInfo("Asia/Seoul")
BACKUP_DIR = Path("data/backups")

CSV_FIELDS = [
    "campaign_name",
    "campaign_id",
    "adgroup_name",
    "adgroup_id",
    "keyword",
    "keyword_id",
    "reg_tm",
    "age_days",
    "tier",
    "status",
    "reason",
    "exposure_eligible",
    "campaign_status",
    "adgroup_status",
    "keyword_status",
    "inspect_status",
    "inactivity_window_days",
    "inactivity_since",
    "inactivity_until",
    "inactivity_impressions",
    "inactivity_clicks",
    "inactivity_stats_source",
    "click_window_days",
    "click_since",
    "click_until",
    "click_window_clicks",
    "click_stats_source",
    "stats_complete",
    "pending_first_at",
    "pending_count",
    "recheck_ready_at",
    "cleanup_needed",
    "cleanup_target_count",
]


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    values: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        values.append(value)
    return values


def _stats_range(days: int) -> tuple[str, str]:
    if days < 1 or days > 90:
        raise SystemExit(
            f"Current direct Naver stats window must be 1~90 days, got {days}. "
            "Change config/cleaner_policy.json before running."
        )
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since.isoformat(), until.isoformat()


def _parse_reg_tm(value: object) -> datetime | None:
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
    return parsed.astimezone(KST)


def _age_days(value: object) -> int | None:
    parsed = _parse_reg_tm(value)
    if parsed is None:
        return None
    return max((datetime.now(KST).date() - parsed.date()).days, 0)


def _status_text(row: dict) -> str:
    return str(row.get("status", "")).strip().upper()


def _exposure_eligible(campaign: dict, adgroup: dict, keyword: dict) -> bool | None:
    """Conservative tri-state exposure eligibility.

    True  = all required status signals are explicitly healthy.
    False = at least one explicit blocking signal exists.
    None  = a required status signal is missing/unknown.
    """
    for row in (campaign, adgroup, keyword):
        if row.get("userLock") is True:
            return False
        status = _status_text(row)
        if not status:
            return None
        if status != "ELIGIBLE":
            return False

    inspect = str(keyword.get("inspectStatus", "")).strip().upper()
    if not inspect:
        return None
    if inspect != "APPROVED":
        return False
    return True


def _iso_to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_csv(rows: list[dict]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"v2_scan_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> int:
    load_dotenv()
    policy = CleanerPolicy.load()

    manual_permanent_keywords = _load_lines(Path("config/protected_keywords.txt"))
    manual_permanent_suffixes = _load_lines(Path("config/protected_suffixes.txt"))
    manual_type_core_keywords = _load_lines(Path("config/type_core_keywords.txt"))
    manual_type_core_suffixes = _load_lines(Path("config/type_core_suffixes.txt"))

    policy = policy.with_updates(
        permanent_suffixes=tuple(
            dict.fromkeys((*policy.permanent_suffixes, *manual_permanent_suffixes))
        ),
        type_core_suffixes=tuple(
            dict.fromkeys((*policy.type_core_suffixes, *manual_type_core_suffixes))
        ),
    )

    # Current API scan uses direct complete-day windows, so keep them <= 90.
    for value in (
        policy.general_inactivity_days,
        policy.type_core_inactivity_days,
        policy.click_protection_days,
    ):
        if value > 90:
            raise SystemExit(
                "V2 scan currently supports direct activity windows up to 90 days. "
                "Please keep deletion windows <= 90 until local daily-history aggregation is added."
            )

    client = NaverSearchAdsClient(NaverConfig.from_env())

    print("=" * 78)
    print("DAYLAW KEYWORD CLEANER V2 — FULL SAFE SCAN / NO DELETION")
    print("=" * 78)
    print("[1/6] Campaigns and ad groups loading...")
    campaigns = client.get_campaigns()

    collected: list[dict] = []
    adgroup_count = 0
    for campaign in campaigns:
        campaign_id = str(campaign.get("nccCampaignId", ""))
        groups = client.get_adgroups(campaign_id)
        adgroup_count += len(groups)
        for adgroup in groups:
            adgroup_id = str(adgroup.get("nccAdgroupId", ""))
            keywords = client.get_keywords(adgroup_id)
            for keyword in keywords:
                keyword_id = str(keyword.get("nccKeywordId", "")).strip()
                if not keyword_id:
                    continue
                text = str(keyword.get("keyword", ""))
                group_name = str(adgroup.get("name", ""))
                tier, tier_reason = keyword_tier(
                    adgroup_name=group_name,
                    keyword=text,
                    policy=policy,
                    manual_permanent_keywords=manual_permanent_keywords,
                    manual_type_core_keywords=manual_type_core_keywords,
                )
                age = _age_days(keyword.get("regTm"))
                exposure = _exposure_eligible(campaign, adgroup, keyword)
                collected.append(
                    {
                        "campaign_name": str(campaign.get("name", "")),
                        "campaign_id": campaign_id,
                        "campaign_raw": campaign,
                        "adgroup_name": group_name,
                        "adgroup_id": adgroup_id,
                        "adgroup_raw": adgroup,
                        "keyword": text,
                        "keyword_id": keyword_id,
                        "keyword_raw": keyword,
                        "reg_tm": keyword.get("regTm"),
                        "age_days": age,
                        "tier": tier,
                        "tier_reason": tier_reason,
                        "exposure_eligible": exposure,
                    }
                )
        print(
            f"      {campaign.get('name')}: {len(groups):,} ad groups "
            f"(running total keywords {len(collected):,})"
        )

    total_keywords = len(collected)
    cleanup_needed = total_keywords >= policy.cleanup_start
    cleanup_target_count = max(0, total_keywords - policy.cleanup_stop) if cleanup_needed else 0

    print("")
    print(f"Campaigns        : {len(campaigns):,}")
    print(f"Ad groups        : {adgroup_count:,}")
    print(f"Keywords         : {total_keywords:,}")
    print(f"Cleanup needed   : {cleanup_needed}")
    print(f"Target removals  : {cleanup_target_count:,}")

    # Only keywords that are old enough, exposure-eligible, and non-permanent
    # need activity API calls. Everything else is decided before stats.
    eligible_general: list[str] = []
    eligible_type: list[str] = []
    eligible_all: list[str] = []
    for item in collected:
        if item["tier"] == "PERMANENT":
            continue
        age = item["age_days"]
        if age is None:
            continue
        protection_days = (
            policy.type_core_protection_days
            if item["tier"] == "TYPE_CORE"
            else policy.general_protection_days
        )
        if age < protection_days:
            continue
        if item["exposure_eligible"] is not True:
            continue
        eligible_all.append(item["keyword_id"])
        if item["tier"] == "TYPE_CORE":
            eligible_type.append(item["keyword_id"])
        else:
            eligible_general.append(item["keyword_id"])

    general_since, general_until = _stats_range(policy.general_inactivity_days)
    type_since, type_until = _stats_range(policy.type_core_inactivity_days)
    click_since, click_until = _stats_range(policy.click_protection_days)

    print("")
    print(
        f"[2/6] GENERAL {policy.general_inactivity_days}d activity: "
        f"{len(eligible_general):,} keywords..."
    )
    general_stats = get_verified_keyword_stats(
        client,
        eligible_general,
        since=general_since,
        until=general_until,
    )

    print(
        f"[3/6] TYPE CORE {policy.type_core_inactivity_days}d activity: "
        f"{len(eligible_type):,} keywords..."
    )
    type_stats = get_verified_keyword_stats(
        client,
        eligible_type,
        since=type_since,
        until=type_until,
    )

    print(
        f"[4/6] Click protection {policy.click_protection_days}d: "
        f"{len(eligible_all):,} keywords..."
    )
    click_stats = get_verified_keyword_stats(
        client,
        eligible_all,
        since=click_since,
        until=click_until,
    )

    print("[5/6] Applying V2 lifecycle rules...")
    store = LifecycleStore()
    observed_at = datetime.now(timezone.utc)
    output: list[dict] = []

    try:
        for index, item in enumerate(collected, start=1):
            tier = item["tier"]
            if tier == "TYPE_CORE":
                inactivity_stat = type_stats.get(item["keyword_id"])
                inactivity_days = policy.type_core_inactivity_days
                inactivity_since = type_since
                inactivity_until = type_until
            else:
                inactivity_stat = general_stats.get(item["keyword_id"])
                inactivity_days = policy.general_inactivity_days
                inactivity_since = general_since
                inactivity_until = general_until

            click_stat = click_stats.get(item["keyword_id"])

            if inactivity_stat is None:
                inactivity_imp = None
                inactivity_clk = None
                inactivity_source = "not_required"
                inactivity_complete = False
            else:
                inactivity_imp = inactivity_stat.impressions
                inactivity_clk = inactivity_stat.clicks
                inactivity_source = inactivity_stat.source
                inactivity_complete = inactivity_stat.complete

            if click_stat is None:
                click_count = None
                click_source = "not_required"
                click_complete = False
            else:
                click_count = click_stat.clicks
                click_source = click_stat.source
                click_complete = click_stat.complete

            # Stats completeness only matters after the earlier permanent/new/
            # exposure checks. classify_snapshot applies those checks first.
            stats_complete = inactivity_complete and click_complete
            snapshot = ActivitySnapshot(
                age_days=item["age_days"],
                inactivity_impressions=inactivity_imp,
                inactivity_clicks=inactivity_clk,
                click_window_clicks=click_count,
                exposure_eligible=item["exposure_eligible"],
                stats_complete=stats_complete,
            )
            decision = classify_snapshot(
                adgroup_name=item["adgroup_name"],
                keyword=item["keyword"],
                snapshot=snapshot,
                policy=policy,
                manual_permanent_keywords=manual_permanent_keywords,
                manual_type_core_keywords=manual_type_core_keywords,
            )

            identity_key = "|".join(
                (
                    item["campaign_id"],
                    item["adgroup_id"],
                    normalize(item["keyword"]),
                )
            )
            state = store.record_decision(
                identity_key=identity_key,
                keyword_id=item["keyword_id"],
                campaign_id=item["campaign_id"],
                adgroup_id=item["adgroup_id"],
                keyword=item["keyword"],
                tier=decision.tier,
                decision=decision.status,
                reason=decision.reason,
                observed_at=observed_at.isoformat(),
                commit=False,
            )

            final_status = decision.status
            final_reason = decision.reason
            recheck_ready_at = ""
            if decision.status == "DELETE_PENDING" and state.first_pending_at:
                first_pending = _iso_to_utc(state.first_pending_at)
                if first_pending is not None:
                    ready_at = first_pending + timedelta(days=policy.pending_recheck_days)
                    recheck_ready_at = ready_at.isoformat()
                    if (
                        not policy.require_second_confirmation
                        or (
                            state.consecutive_pending >= 2
                            and observed_at >= ready_at
                        )
                    ):
                        final_status = "DELETE_APPROVED"
                        final_reason = (
                            f"second_confirmation_passed_after_"
                            f"{policy.pending_recheck_days}d:{decision.reason}"
                        )

            output.append(
                {
                    "campaign_name": item["campaign_name"],
                    "campaign_id": item["campaign_id"],
                    "adgroup_name": item["adgroup_name"],
                    "adgroup_id": item["adgroup_id"],
                    "keyword": item["keyword"],
                    "keyword_id": item["keyword_id"],
                    "reg_tm": item["reg_tm"],
                    "age_days": item["age_days"],
                    "tier": decision.tier,
                    "status": final_status,
                    "reason": final_reason,
                    "exposure_eligible": item["exposure_eligible"],
                    "campaign_status": item["campaign_raw"].get("status"),
                    "adgroup_status": item["adgroup_raw"].get("status"),
                    "keyword_status": item["keyword_raw"].get("status"),
                    "inspect_status": item["keyword_raw"].get("inspectStatus"),
                    "inactivity_window_days": inactivity_days,
                    "inactivity_since": inactivity_since,
                    "inactivity_until": inactivity_until,
                    "inactivity_impressions": inactivity_imp,
                    "inactivity_clicks": inactivity_clk,
                    "inactivity_stats_source": inactivity_source,
                    "click_window_days": policy.click_protection_days,
                    "click_since": click_since,
                    "click_until": click_until,
                    "click_window_clicks": click_count,
                    "click_stats_source": click_source,
                    "stats_complete": stats_complete,
                    "pending_first_at": state.first_pending_at or "",
                    "pending_count": state.consecutive_pending,
                    "recheck_ready_at": recheck_ready_at,
                    "cleanup_needed": cleanup_needed,
                    "cleanup_target_count": cleanup_target_count,
                }
            )

            if index % 10000 == 0:
                print(f"      classified {index:,}/{total_keywords:,}")

        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        store.close()

    path = _write_csv(output)
    counts = Counter(row["status"] for row in output)
    tiers = Counter(row["tier"] for row in output)

    print("[6/6] Complete")
    print("")
    print("V2 RESULT")
    print(f"  PERMANENT         : {counts['PERMANENT']:,}")
    print(f"  PROTECTED_NEW     : {counts['PROTECTED_NEW']:,}")
    print(f"  KEEP              : {counts['KEEP']:,}")
    print(f"  WATCH             : {counts['WATCH']:,}")
    print(f"  DATA_INSUFFICIENT : {counts['DATA_INSUFFICIENT']:,}")
    print(f"  DELETE_PENDING    : {counts['DELETE_PENDING']:,}")
    print(f"  DELETE_APPROVED   : {counts['DELETE_APPROVED']:,}")
    print("")
    print(f"  TIER PERMANENT    : {tiers['PERMANENT']:,}")
    print(f"  TIER TYPE_CORE    : {tiers['TYPE_CORE']:,}")
    print(f"  TIER GENERAL      : {tiers['GENERAL']:,}")
    print("")
    print(f"  Cleanup threshold : {policy.cleanup_start:,}")
    print(f"  Cleanup stop      : {policy.cleanup_stop:,}")
    print(f"  Current keywords  : {total_keywords:,}")
    print(f"  Target removals   : {cleanup_target_count:,}")
    print(f"  CSV               : {path}")
    print("")
    print("[SAFE STOP] V2 scan is read-only. No keyword was deleted or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
