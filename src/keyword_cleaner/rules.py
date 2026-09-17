from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Permanent core variants. These are matched only as an exact
# "ad-group name + suffix" combination after normalization.
#
# Example: for ad group "ABC", suffix "해외선물" protects only
# "ABC해외선물" (including harmless spacing/punctuation differences).
DEFAULT_PROTECTED_SUFFIXES = (
    # Common fraud / response intent
    "사기",
    "피해",
    "피해금",
    "사칭",
    "신고",
    "고소",

    # Stock / coin / futures / investment
    "주식",
    "코인",
    "리딩방",
    "주식리딩방",
    "코인리딩방",
    "해외선물",
    "선물",
    "투자",
    "AI투자",
    "AI자동매매",
    "자동매매",
    "가상자산",
    "가상자산거래소",
    "가상화폐",
    "플랫폼",
    "지수거래",
    "HTS",
    "MTS",

    # Shopping / travel / movie / gift-card team mission
    "쇼핑몰",
    "부업",
    "팀미션",
    "미션",
    "구매대행",
    "리뷰",
    "여행사",
    "여행",
    "영화예매",
    "영화",
    "예매",
    "기프트카드",
    "상품권",

    # Lotto / compensation / refund
    "로또",
    "코인보상",
    "피해보상",
    "보상",
    "보험금",
    "보험금환급",
    "환급",

    # Romance scam
    "결혼정보회사",
    "로맨스스캠",
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s\-_./·ㆍ()\[\]{}]+", "", text)


def _read_user_list(filename: str) -> list[str]:
    """Load editable protection entries from config/*.txt.

    Blank lines and lines beginning with # are ignored. Missing files are safe.
    The directory can be changed with PROTECTION_CONFIG_DIR.
    """
    config_dir = Path(os.getenv("PROTECTION_CONFIG_DIR", "config"))
    path = config_dir / filename
    if not path.exists():
        return []

    values: list[str] = []
    with path.open("r", encoding="utf-8-sig") as fp:
        for raw in fp:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            values.append(value)
    return values


def user_protected_suffixes() -> list[str]:
    """Suffixes automatically combined with every ad-group name."""
    return _read_user_list("protected_suffixes.txt")


def user_exact_protected_keywords() -> list[str]:
    """Full exact keywords that must always be kept."""
    return _read_user_list("protected_keywords.txt")


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    protected: bool


def is_protected_keyword(
    *,
    adgroup_name: str,
    keyword: str,
    protected_suffixes: Iterable[str] = DEFAULT_PROTECTED_SUFFIXES,
    extra_exact_keywords: Iterable[str] = (),
) -> tuple[bool, str]:
    group = normalize(adgroup_name)
    kw = normalize(keyword)

    if not kw:
        return True, "empty_keyword_safety"

    if kw == group:
        return True, "core:adgroup_name"

    # Three layers are merged:
    # 1) built-ins in code
    # 2) optional .env additions
    # 3) editable config/protected_suffixes.txt additions
    # None of the user layers can remove built-in protection.
    effective_suffixes = tuple(
        dict.fromkeys(
            (
                *DEFAULT_PROTECTED_SUFFIXES,
                *tuple(protected_suffixes),
                *tuple(user_protected_suffixes()),
            )
        )
    )
    for suffix in effective_suffixes:
        protected = group + normalize(suffix)
        if kw == protected:
            return True, f"core:{suffix}"

    # Exact whitelist can also be managed in two places:
    # .env EXTRA_PROTECTED_KEYWORDS and config/protected_keywords.txt.
    extra = {
        normalize(value)
        for value in (
            *tuple(extra_exact_keywords),
            *tuple(user_exact_protected_keywords()),
        )
        if value.strip()
    }
    if kw in extra:
        return True, "extra_whitelist"

    return False, ""


def classify_keyword(
    *,
    adgroup_name: str,
    keyword: str,
    age_days: int | None,
    recent_impressions: int,
    recent_clicks: int,
    history_impressions: int,
    history_clicks: int,
    min_age_days: int,
    recent_days: int,
    history_days: int,
    protected_suffixes: Iterable[str] = DEFAULT_PROTECTED_SUFFIXES,
    extra_exact_keywords: Iterable[str] = (),
) -> Decision:
    """Classify one keyword conservatively.

    Deletion is allowed only when ALL of these are true:
    - not a protected/core keyword
    - registration age is known and >= min_age_days
    - no impressions/clicks in the recent window
    - no impressions/clicks in the longer history window

    This deliberately prefers WATCH/KEEP when information is uncertain.
    """
    protected, reason = is_protected_keyword(
        adgroup_name=adgroup_name,
        keyword=keyword,
        protected_suffixes=protected_suffixes,
        extra_exact_keywords=extra_exact_keywords,
    )
    if protected:
        return Decision("KEEP", reason, True)

    if age_days is None:
        return Decision("WATCH", "safety:registration_age_unknown", False)

    if age_days < min_age_days:
        return Decision(
            "KEEP",
            f"young_keyword:age={age_days}d<min={min_age_days}d",
            False,
        )

    if recent_clicks > 0:
        return Decision(
            "KEEP",
            f"recent_activity:{recent_days}d_clicks={recent_clicks}",
            False,
        )

    if recent_impressions > 0:
        return Decision(
            "WATCH",
            f"recent_activity:{recent_days}d_impressions={recent_impressions},clicks=0",
            False,
        )

    if history_clicks > 0 or history_impressions > 0:
        return Decision(
            "WATCH",
            (
                f"historical_activity:{history_days}d_impressions="
                f"{history_impressions},clicks={history_clicks}"
            ),
            False,
        )

    return Decision(
        "DELETE_CANDIDATE",
        (
            f"safe_zero_activity:age={age_days}d,recent={recent_days}d_0/0,"
            f"history={history_days}d_0/0"
        ),
        False,
    )
