from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


DEFAULT_PROTECTED_SUFFIXES = (
    "사기",
    "피해",
    "피해금",
    "팀미션",
    "부업",
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s\-_./·ㆍ()\[\]{}]+", "", text)


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

    for suffix in protected_suffixes:
        protected = group + normalize(suffix)
        if kw == protected:
            return True, f"core:{suffix}"

    extra = {normalize(value) for value in extra_exact_keywords if value.strip()}
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
