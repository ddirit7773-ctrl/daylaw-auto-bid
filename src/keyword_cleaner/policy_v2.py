from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_POLICY_PATH = Path("config/cleaner_policy.json")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s\-_./·ㆍ()\[\]{}]+", "", text)


@dataclass(frozen=True)
class CleanerPolicy:
    permanent_suffixes: tuple[str, ...]
    type_core_suffixes: tuple[str, ...]
    general_protection_days: int
    type_core_protection_days: int
    general_inactivity_days: int
    type_core_inactivity_days: int
    click_protection_days: int
    reference_history_days: int
    pending_recheck_days: int
    cleanup_start: int
    cleanup_stop: int
    hard_limit_reference: int
    first_test_batch: int
    second_test_batch: int
    normal_batch: int
    require_exposure_eligible: bool
    require_complete_stats: bool
    require_second_confirmation: bool

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_PATH) -> "CleanerPolicy":
        payload = json.loads(path.read_text(encoding="utf-8"))
        protection = payload["protection_days"]
        inactivity = payload["inactivity_days"]
        capacity = payload["account_capacity"]
        batches = payload["deletion_batches"]
        safety = payload["safety"]
        return cls(
            permanent_suffixes=tuple(payload["permanent_suffixes"]),
            type_core_suffixes=tuple(payload["type_core_suffixes"]),
            general_protection_days=int(protection["general"]),
            type_core_protection_days=int(protection["type_core"]),
            general_inactivity_days=int(inactivity["general"]),
            type_core_inactivity_days=int(inactivity["type_core"]),
            click_protection_days=int(payload["click_protection_days"]),
            reference_history_days=int(payload["reference_history_days"]),
            pending_recheck_days=int(payload["pending_recheck_days"]),
            cleanup_start=int(capacity["cleanup_start"]),
            cleanup_stop=int(capacity["cleanup_stop"]),
            hard_limit_reference=int(capacity["hard_limit_reference"]),
            first_test_batch=int(batches["first_test"]),
            second_test_batch=int(batches["second_test"]),
            normal_batch=int(batches["normal"]),
            require_exposure_eligible=bool(safety["require_exposure_eligible"]),
            require_complete_stats=bool(safety["require_complete_stats"]),
            require_second_confirmation=bool(safety["require_second_confirmation"]),
        )


@dataclass(frozen=True)
class PolicyDecision:
    status: str
    tier: str
    reason: str
    protected: bool = False


@dataclass(frozen=True)
class ActivitySnapshot:
    age_days: int | None
    inactivity_impressions: int | None
    inactivity_clicks: int | None
    click_window_clicks: int | None
    exposure_eligible: bool | None
    stats_complete: bool


def _normalized_set(values: Iterable[str]) -> set[str]:
    return {normalize(v) for v in values if str(v).strip()}


def _matches_group_suffix(adgroup_name: str, keyword: str, suffixes: Iterable[str]) -> str | None:
    group = normalize(adgroup_name)
    kw = normalize(keyword)
    for suffix in suffixes:
        if kw == group + normalize(suffix):
            return suffix
    return None


def keyword_tier(
    *,
    adgroup_name: str,
    keyword: str,
    policy: CleanerPolicy,
    manual_permanent_keywords: Iterable[str] = (),
    manual_type_core_keywords: Iterable[str] = (),
) -> tuple[str, str]:
    kw = normalize(keyword)
    if not kw:
        return "PERMANENT", "empty_keyword_safety"

    manual_permanent = _normalized_set(manual_permanent_keywords)
    if kw in manual_permanent:
        return "PERMANENT", "manual_permanent"

    permanent_suffix = _matches_group_suffix(
        adgroup_name, keyword, policy.permanent_suffixes
    )
    if permanent_suffix is not None:
        label = "adgroup_name" if permanent_suffix == "" else permanent_suffix
        return "PERMANENT", f"permanent:{label}"

    manual_type_core = _normalized_set(manual_type_core_keywords)
    if kw in manual_type_core:
        return "TYPE_CORE", "manual_type_core"

    type_suffix = _matches_group_suffix(
        adgroup_name, keyword, policy.type_core_suffixes
    )
    if type_suffix is not None:
        return "TYPE_CORE", f"type_core:{type_suffix}"

    return "GENERAL", "general"


def classify_snapshot(
    *,
    adgroup_name: str,
    keyword: str,
    snapshot: ActivitySnapshot,
    policy: CleanerPolicy,
    manual_permanent_keywords: Iterable[str] = (),
    manual_type_core_keywords: Iterable[str] = (),
) -> PolicyDecision:
    tier, tier_reason = keyword_tier(
        adgroup_name=adgroup_name,
        keyword=keyword,
        policy=policy,
        manual_permanent_keywords=manual_permanent_keywords,
        manual_type_core_keywords=manual_type_core_keywords,
    )

    if tier == "PERMANENT":
        return PolicyDecision("PERMANENT", tier, tier_reason, True)

    if snapshot.age_days is None:
        return PolicyDecision(
            "DATA_INSUFFICIENT",
            tier,
            "registration_age_unknown",
            False,
        )

    protection_days = (
        policy.type_core_protection_days
        if tier == "TYPE_CORE"
        else policy.general_protection_days
    )
    if snapshot.age_days < protection_days:
        return PolicyDecision(
            "PROTECTED_NEW",
            tier,
            f"age={snapshot.age_days}d<protection={protection_days}d",
            True,
        )

    if policy.require_exposure_eligible and snapshot.exposure_eligible is not True:
        return PolicyDecision(
            "DATA_INSUFFICIENT",
            tier,
            f"exposure_eligible={snapshot.exposure_eligible}",
            False,
        )

    if policy.require_complete_stats and not snapshot.stats_complete:
        return PolicyDecision(
            "DATA_INSUFFICIENT",
            tier,
            "stats_incomplete",
            False,
        )

    if (
        snapshot.inactivity_impressions is None
        or snapshot.inactivity_clicks is None
        or snapshot.click_window_clicks is None
    ):
        return PolicyDecision(
            "DATA_INSUFFICIENT",
            tier,
            "required_activity_window_missing",
            False,
        )

    if snapshot.click_window_clicks > 0:
        return PolicyDecision(
            "KEEP",
            tier,
            f"clicks_within_{policy.click_protection_days}d={snapshot.click_window_clicks}",
            True,
        )

    if snapshot.inactivity_clicks > 0:
        return PolicyDecision(
            "KEEP",
            tier,
            f"recent_clicks={snapshot.inactivity_clicks}",
            True,
        )

    if snapshot.inactivity_impressions > 0:
        return PolicyDecision(
            "WATCH",
            tier,
            f"recent_impressions={snapshot.inactivity_impressions},clicks=0",
            False,
        )

    inactivity_days = (
        policy.type_core_inactivity_days
        if tier == "TYPE_CORE"
        else policy.general_inactivity_days
    )
    return PolicyDecision(
        "DELETE_PENDING",
        tier,
        f"{inactivity_days}d_0_impressions_0_clicks_and_{policy.click_protection_days}d_0_clicks",
        False,
    )
