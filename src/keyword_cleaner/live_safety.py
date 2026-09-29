from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .naver_api import NaverSearchAdsClient, NaverSearchAdsError
from .policy_v2 import CleanerPolicy, keyword_tier, normalize
from .stats_v2 import get_singular_verified_keyword_stat


KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class LiveSafetyResult:
    ready: bool
    reason: str
    current_keyword: dict | None
    inactivity_impressions: int | None
    inactivity_clicks: int | None
    click_window_clicks: int | None
    history_impressions: int | None
    history_clicks: int | None


def stats_range(days: int) -> tuple[str, str]:
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since.isoformat(), until.isoformat()


def exposure_ok(row: dict) -> bool:
    if row.get("userLock") is True:
        return False
    return str(row.get("status", "")).strip().upper() == "ELIGIBLE"


def keyword_exposure_ok(row: dict) -> bool:
    return exposure_ok(row) and str(row.get("inspectStatus", "")).strip().upper() == "APPROVED"


def evaluate_general_delete_gate(
    *,
    client: NaverSearchAdsClient,
    plan: dict[str, str],
    policy: CleanerPolicy,
    campaigns: dict[str, dict],
    adgroups: dict[str, dict],
    manual_permanent_keywords: Iterable[str] = (),
    manual_type_core_keywords: Iterable[str] = (),
) -> LiveSafetyResult:
    """Run the single-keyword final safety gate used by approval and deletion.

    A GENERAL keyword is READY only when all of the following still hold now:
    - keyword text and ad-group identity are unchanged;
    - campaign/ad group/keyword remain exposure-eligible;
    - protection rules still classify it as GENERAL;
    - inactivity window is 0 impressions / 0 clicks;
    - click-protection window is 0 clicks;
    - full reference-history window is 0 impressions / 0 clicks.

    Any missing or failed read is BLOCK, never zero-filled.
    """
    kid = str(plan.get("keyword_id", "")).strip()
    try:
        current = client.get_keyword(kid)
    except NaverSearchAdsError as exc:
        return LiveSafetyResult(
            False,
            f"keyword_lookup_failed:{exc}",
            None,
            None,
            None,
            None,
            None,
            None,
        )

    if normalize(str(current.get("keyword", ""))) != normalize(str(plan.get("keyword", ""))):
        return LiveSafetyResult(False, "keyword_text_changed", current, None, None, None, None, None)
    if str(current.get("nccAdgroupId", "")) != str(plan.get("adgroup_id", "")):
        return LiveSafetyResult(False, "adgroup_changed", current, None, None, None, None, None)

    campaign = campaigns.get(str(plan.get("campaign_id", "")))
    adgroup = adgroups.get(str(plan.get("adgroup_id", "")))
    if not campaign or not adgroup:
        return LiveSafetyResult(False, "parent_not_found", current, None, None, None, None, None)
    if not exposure_ok(campaign) or not exposure_ok(adgroup) or not keyword_exposure_ok(current):
        return LiveSafetyResult(
            False,
            "current_exposure_not_eligible",
            current,
            None,
            None,
            None,
            None,
            None,
        )

    current_tier, tier_reason = keyword_tier(
        adgroup_name=str(adgroup.get("name", "")),
        keyword=str(current.get("keyword", "")),
        policy=policy,
        manual_permanent_keywords=manual_permanent_keywords,
        manual_type_core_keywords=manual_type_core_keywords,
    )
    if current_tier != "GENERAL":
        return LiveSafetyResult(
            False,
            f"tier_changed:{current_tier}:{tier_reason}",
            current,
            None,
            None,
            None,
            None,
            None,
        )

    inactivity_since, inactivity_until = stats_range(policy.general_inactivity_days)
    click_since, click_until = stats_range(policy.click_protection_days)
    history_since, history_until = stats_range(policy.reference_history_days)

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
    history = get_singular_verified_keyword_stat(
        client,
        kid,
        since=history_since,
        until=history_until,
    )

    if not recent.complete or not clicks.complete or not history.complete:
        return LiveSafetyResult(
            False,
            "stats_revalidation_incomplete",
            current,
            recent.impressions,
            recent.clicks,
            clicks.clicks,
            history.impressions,
            history.clicks,
        )
    if (recent.impressions or 0) != 0 or (recent.clicks or 0) != 0:
        return LiveSafetyResult(
            False,
            "recent_30d_activity_detected",
            current,
            recent.impressions,
            recent.clicks,
            clicks.clicks,
            history.impressions,
            history.clicks,
        )
    if (clicks.clicks or 0) != 0:
        return LiveSafetyResult(
            False,
            "click_within_60d",
            current,
            recent.impressions,
            recent.clicks,
            clicks.clicks,
            history.impressions,
            history.clicks,
        )
    if (history.impressions or 0) != 0 or (history.clicks or 0) != 0:
        return LiveSafetyResult(
            False,
            "history_within_90d",
            current,
            recent.impressions,
            recent.clicks,
            clicks.clicks,
            history.impressions,
            history.clicks,
        )

    return LiveSafetyResult(
        True,
        "all_live_gates_passed_30d_60d_90d",
        current,
        recent.impressions,
        recent.clicks,
        clicks.clicks,
        history.impressions,
        history.clicks,
    )


def count_account_keywords_live(
    client: NaverSearchAdsClient,
    *,
    progress_every: int = 25,
) -> int:
    """Read the account structure and return the current live keyword count.

    This is intentionally read-only and conservative. Any API failure raises and
    blocks deletion rather than falling back to a stale count.
    """
    campaigns = client.get_campaigns()
    groups: list[dict] = []
    for campaign in campaigns:
        campaign_id = str(campaign.get("nccCampaignId", ""))
        if campaign_id:
            groups.extend(client.get_adgroups(campaign_id))

    total_groups = len(groups)
    total_keywords = 0
    for index, group in enumerate(groups, start=1):
        adgroup_id = str(group.get("nccAdgroupId", ""))
        if not adgroup_id:
            continue
        total_keywords += len(client.get_keywords(adgroup_id))
        if index == 1 or index == total_groups or index % max(progress_every, 1) == 0:
            print(
                f"@@ACCOUNT_COUNT_PROGRESS|{index}|{total_groups}|{total_keywords}",
                flush=True,
            )
    return total_keywords
