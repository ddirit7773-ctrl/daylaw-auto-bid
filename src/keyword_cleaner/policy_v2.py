from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, replace
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
        policy = cls(
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
        policy.validate()
        return policy

    def validate(self) -> None:
        day_fields = {
            "일반 신규 보호일": self.general_protection_days,
            "유형 핵심 보호일": self.type_core_protection_days,
            "일반 무활동 판단일": self.general_inactivity_days,
            "유형 핵심 무활동 판단일": self.type_core_inactivity_days,
            "클릭 보호일": self.click_protection_days,
            "참고 이력일": self.reference_history_days,
            "DELETE 재검증 대기일": self.pending_recheck_days,
        }
        for label, value in day_fields.items():
            if value < 1 or value > 365:
                raise ValueError(f"{label}은(는) 1~365일 사이여야 합니다: {value}")

        if self.hard_limit_reference < 1:
            raise ValueError("계정 한도 기준값은 1 이상이어야 합니다.")
        if not (0 <= self.cleanup_stop < self.cleanup_start < self.hard_limit_reference):
            raise ValueError(
                "키워드 정리 기준은 '정리 중단 < 정리 시작 < 계정 한도' 순서여야 합니다."
            )

        batch_fields = {
            "최초 테스트 삭제량": self.first_test_batch,
            "2차 테스트 삭제량": self.second_test_batch,
            "정상 배치 삭제량": self.normal_batch,
        }
        for label, value in batch_fields.items():
            if value < 1 or value > 10000:
                raise ValueError(f"{label}은(는) 1~10,000 사이여야 합니다: {value}")
        if not (
            self.first_test_batch <= self.second_test_batch <= self.normal_batch
        ):
            raise ValueError(
                "삭제 배치 크기는 '최초 테스트 <= 2차 테스트 <= 정상 배치' 순서여야 합니다."
            )

    def with_updates(self, **changes: object) -> "CleanerPolicy":
        """Return a validated copy after settings-screen changes."""
        updated = replace(self, **changes)
        updated.validate()
        return updated

    def to_dict(self) -> dict:
        return {
            "schema_version": 2,
            "permanent_suffixes": list(self.permanent_suffixes),
            "type_core_suffixes": list(self.type_core_suffixes),
            "protection_days": {
                "general": self.general_protection_days,
                "type_core": self.type_core_protection_days,
            },
            "inactivity_days": {
                "general": self.general_inactivity_days,
                "type_core": self.type_core_inactivity_days,
            },
            "click_protection_days": self.click_protection_days,
            "reference_history_days": self.reference_history_days,
            "pending_recheck_days": self.pending_recheck_days,
            "account_capacity": {
                "cleanup_start": self.cleanup_start,
                "cleanup_stop": self.cleanup_stop,
                "hard_limit_reference": self.hard_limit_reference,
            },
            "deletion_batches": {
                "first_test": self.first_test_batch,
                "second_test": self.second_test_batch,
                "normal": self.normal_batch,
            },
            "safety": {
                "require_exposure_eligible": self.require_exposure_eligible,
                "require_complete_stats": self.require_complete_stats,
                "require_second_confirmation": self.require_second_confirmation,
                "monthly_search_volume_is_delete_gate": False,
            },
        }

    def save(self, path: Path = DEFAULT_POLICY_PATH) -> None:
        """Persist settings atomically so the future desktop UI can edit them."""
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)


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
