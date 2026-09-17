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
    impressions: int,
    clicks: int,
    protected_suffixes: Iterable[str] = DEFAULT_PROTECTED_SUFFIXES,
    extra_exact_keywords: Iterable[str] = (),
) -> Decision:
    protected, reason = is_protected_keyword(
        adgroup_name=adgroup_name,
        keyword=keyword,
        protected_suffixes=protected_suffixes,
        extra_exact_keywords=extra_exact_keywords,
    )
    if protected:
        return Decision("KEEP", reason, True)

    if clicks > 0:
        return Decision("KEEP", f"activity:clicks={clicks}", False)

    if impressions > 0:
        return Decision(
            "WATCH", f"activity:impressions={impressions},clicks=0", False
        )

    return Decision(
        "DELETE_CANDIDATE", "14d_zero_impressions_and_clicks", False
    )
