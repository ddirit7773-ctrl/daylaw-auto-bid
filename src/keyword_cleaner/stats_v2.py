from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from .naver_api import NaverSearchAdsClient, NaverSearchAdsError


@dataclass(frozen=True)
class VerifiedKeywordStat:
    keyword_id: str
    impressions: int | None
    clicks: int | None
    complete: bool
    source: str
    error: str | None = None


def _parse_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _to_int(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _chunks(values: Sequence[str], size: int) -> list[Sequence[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def get_verified_keyword_stats(
    client: NaverSearchAdsClient,
    keyword_ids: Sequence[str],
    *,
    since: str,
    until: str,
    batch_size: int = 100,
) -> dict[str, VerifiedKeywordStat]:
    """Fetch keyword stats without converting request failures into zero.

    Naver multi-id /stats may omit rows with no activity. Our diagnostic showed
    returned rows match singular totals when timeIncrement=allDays is used.
    Because these ids were freshly fetched from /ncc/keywords, a successful
    batch request plus an omitted id is treated as a zero-total observation.

    A failed batch is never treated as zero: every id in that failed batch is
    marked incomplete so the deletion policy cannot approve it.
    """
    clean_ids = [str(value).strip() for value in keyword_ids if str(value).strip()]
    result: dict[str, VerifiedKeywordStat] = {}

    if not clean_ids:
        return result

    fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))

    for batch in _chunks(clean_ids, batch_size):
        params = {
            "ids": json.dumps(list(batch), separators=(",", ":")),
            "fields": fields,
            "timeRange": time_range,
            "timeIncrement": "allDays",
        }
        try:
            payload = client._request("GET", "/stats", params=params)
        except NaverSearchAdsError as exc:
            for keyword_id in batch:
                result[keyword_id] = VerifiedKeywordStat(
                    keyword_id=keyword_id,
                    impressions=None,
                    clicks=None,
                    complete=False,
                    source="batch_error",
                    error=str(exc),
                )
            continue

        rows = _parse_rows(payload)
        returned: dict[str, tuple[int, int]] = {}
        for row in rows:
            keyword_id = str(row.get("id", "")).strip()
            if not keyword_id or keyword_id not in batch:
                continue
            returned[keyword_id] = (
                _to_int(row.get("impCnt")),
                _to_int(row.get("clkCnt")),
            )

        for keyword_id in batch:
            if keyword_id in returned:
                imp, clk = returned[keyword_id]
                result[keyword_id] = VerifiedKeywordStat(
                    keyword_id=keyword_id,
                    impressions=imp,
                    clicks=clk,
                    complete=True,
                    source="multi_returned",
                )
            else:
                result[keyword_id] = VerifiedKeywordStat(
                    keyword_id=keyword_id,
                    impressions=0,
                    clicks=0,
                    complete=True,
                    source="multi_omitted_zero",
                )

    return result


def get_singular_verified_keyword_stat(
    client: NaverSearchAdsClient,
    keyword_id: str,
    *,
    since: str,
    until: str,
) -> VerifiedKeywordStat:
    """Single-keyword verification for the final delete gate.

    A successful singular /stats request can also return no row for a true 0/0
    keyword. In that case we separately fetch the keyword object itself. Only
    when the keyword still exists do we accept the empty stats response as 0/0.
    This distinguishes a zero-total keyword from request failure or a stale id.
    """
    keyword_id = str(keyword_id).strip()
    if not keyword_id:
        return VerifiedKeywordStat(
            keyword_id="",
            impressions=None,
            clicks=None,
            complete=False,
            source="invalid_id",
            error="empty keyword id",
        )

    fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    params = {
        "id": keyword_id,
        "fields": fields,
        "timeRange": time_range,
        "timeIncrement": "allDays",
    }

    try:
        payload = client._request("GET", "/stats", params=params)
    except NaverSearchAdsError as exc:
        return VerifiedKeywordStat(
            keyword_id=keyword_id,
            impressions=None,
            clicks=None,
            complete=False,
            source="singular_error",
            error=str(exc),
        )

    rows = _parse_rows(payload)
    if not rows:
        try:
            current = client.get_keyword(keyword_id)
        except NaverSearchAdsError as exc:
            return VerifiedKeywordStat(
                keyword_id=keyword_id,
                impressions=None,
                clicks=None,
                complete=False,
                source="singular_empty_keyword_unverified",
                error=str(exc),
            )

        current_id = str(current.get("nccKeywordId", "")).strip()
        if current_id != keyword_id:
            return VerifiedKeywordStat(
                keyword_id=keyword_id,
                impressions=None,
                clicks=None,
                complete=False,
                source="singular_empty_keyword_mismatch",
                error=f"keyword lookup returned {current_id!r}",
            )

        return VerifiedKeywordStat(
            keyword_id=keyword_id,
            impressions=0,
            clicks=0,
            complete=True,
            source="singular_empty_zero_keyword_exists",
        )

    impressions = sum(_to_int(row.get("impCnt")) for row in rows)
    clicks = sum(_to_int(row.get("clkCnt")) for row in rows)
    return VerifiedKeywordStat(
        keyword_id=keyword_id,
        impressions=impressions,
        clicks=clicks,
        complete=True,
        source="singular_verified",
    )
