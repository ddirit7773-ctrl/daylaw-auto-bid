from __future__ import annotations

import restricted_cleanup as base


_original_text_upper = base.text_upper


def _text_upper_compat(value: object) -> str:
    """Normalize Naver keyword inspection LIMITED_APPROVED to the v14 gate token.

    Naver's Ad Keyword master uses 30 / LIMITED_APPROVED for exposure-limited
    keywords. v14 originally expected APPROVED, which made the review pool zero.
    We intentionally remap only the inspection-state token used by the existing
    restricted-cleanup gate; keyword/campaign/adgroup status values are unchanged.
    """
    text = _original_text_upper(value)
    if text in {"LIMITED_APPROVED", "30"}:
        return "APPROVED"
    return text


base.text_upper = _text_upper_compat


def main() -> int:
    return int(base.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
