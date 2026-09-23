from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient


KST = ZoneInfo("Asia/Seoul")
SAMPLE_SIZE = 100
SINGULAR_MISSING_PROBE = 10
SINGULAR_RETURNED_PROBE = 3


def stats_range(days: int) -> tuple[str, str]:
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since.isoformat(), until.isoformat()


def short_status(row: dict) -> dict[str, object]:
    keys = (
        "userLock",
        "status",
        "statusReason",
        "inspectStatus",
        "delFlag",
        "useGroupBidAmt",
    )
    return {key: row.get(key) for key in keys if key in row}


def extract_stat_values(payload: object) -> tuple[int | None, int | None]:
    """Normalize singular /stats response to (impCnt, clkCnt)."""
    if not isinstance(payload, dict):
        return None, None

    # Singular `id` requests usually return the fields directly.
    if "impCnt" in payload or "clkCnt" in payload:
        try:
            imp = int(float(payload.get("impCnt", 0) or 0))
            clk = int(float(payload.get("clkCnt", 0) or 0))
            return imp, clk
        except (TypeError, ValueError):
            return None, None

    # Be defensive in case the API wraps the result.
    rows = payload.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        try:
            imp = int(float(rows[0].get("impCnt", 0) or 0))
            clk = int(float(rows[0].get("clkCnt", 0) or 0))
            return imp, clk
        except (TypeError, ValueError):
            return None, None

    return None, None


def singular_stats(
    client: NaverSearchAdsClient,
    keyword_id: str,
    *,
    since: str,
    until: str,
    fields: str,
) -> tuple[int | None, int | None]:
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    payload = client._request(
        "GET",
        "/stats",
        params={
            "id": keyword_id,
            "fields": fields,
            "timeRange": time_range,
        },
    )
    return extract_stat_values(payload)


def main() -> int:
    load_dotenv()
    client = NaverSearchAdsClient(NaverConfig.from_env())

    print("=" * 76)
    print("V2 SAFETY DIAGNOSTIC — READ ONLY / NO DELETION")
    print("=" * 76)

    campaigns = client.get_campaigns()
    print(f"Campaigns: {len(campaigns):,}")
    for campaign in campaigns[:10]:
        print(
            "  CAMPAIGN",
            campaign.get("name"),
            short_status(campaign),
        )

    sample_keywords: list[dict] = []
    sampled_adgroups: list[dict] = []
    for campaign in campaigns:
        groups = client.get_adgroups(str(campaign["nccCampaignId"]))
        for group in groups:
            if len(sampled_adgroups) < 10:
                sampled_adgroups.append(group)
            keywords = client.get_keywords(str(group["nccAdgroupId"]))
            for keyword in keywords:
                sample_keywords.append(keyword)
                if len(sample_keywords) >= SAMPLE_SIZE:
                    break
            if len(sample_keywords) >= SAMPLE_SIZE:
                break
        if len(sample_keywords) >= SAMPLE_SIZE:
            break

    print("")
    print("Ad-group status samples:")
    for group in sampled_adgroups:
        print("  ADGROUP", group.get("name"), short_status(group))

    print("")
    print(f"Keyword status sample size: {len(sample_keywords):,}")
    status_counter = Counter(
        (
            str(keyword.get("userLock")),
            str(keyword.get("status")),
            str(keyword.get("statusReason")),
            str(keyword.get("inspectStatus")),
        )
        for keyword in sample_keywords
    )
    for values, count in status_counter.most_common(20):
        print(
            "  KEYWORD STATUS",
            f"count={count}",
            {
                "userLock": values[0],
                "status": values[1],
                "statusReason": values[2],
                "inspectStatus": values[3],
            },
        )

    keyword_ids = [
        str(keyword.get("nccKeywordId", "")).strip()
        for keyword in sample_keywords
        if str(keyword.get("nccKeywordId", "")).strip()
    ]
    since, until = stats_range(30)
    fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    params: list[tuple[str, object]] = [("ids", kid) for kid in keyword_ids]
    params.extend((("fields", fields), ("timeRange", time_range)))
    payload = client._request("GET", "/stats", params=params)  # diagnostic only
    if isinstance(payload, dict):
        rows = payload.get("data", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    returned_ids = {
        str(row.get("id", "")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("id", "")).strip()
    }
    missing = [kid for kid in keyword_ids if kid not in returned_ids]

    print("")
    print("Stats coverage check (30 complete days)")
    print(f"  Range          : {since} ~ {until}")
    print(f"  Requested IDs  : {len(keyword_ids):,}")
    print(f"  Returned rows  : {len(rows):,}")
    print(f"  Returned IDs   : {len(returned_ids):,}")
    print(f"  Missing IDs    : {len(missing):,}")

    print("")
    print("Singular verification for IDs omitted from multi-id /stats")
    missing_probe = missing[:SINGULAR_MISSING_PROBE]
    confirmed_zero = 0
    nonzero = 0
    unknown = 0
    for index, kid in enumerate(missing_probe, start=1):
        imp, clk = singular_stats(
            client,
            kid,
            since=since,
            until=until,
            fields=fields,
        )
        if imp is None or clk is None:
            label = "UNKNOWN"
            unknown += 1
        elif imp == 0 and clk == 0:
            label = "ZERO_CONFIRMED"
            confirmed_zero += 1
        else:
            label = "NONZERO"
            nonzero += 1
        print(f"  MISSING PROBE {index:02d}: imp={imp}, clk={clk}, {label}")

    print("")
    print("Cross-check for IDs returned by multi-id /stats")
    returned_probe = list(returned_ids)[:SINGULAR_RETURNED_PROBE]
    row_by_id = {
        str(row.get("id", "")).strip(): row
        for row in rows
        if isinstance(row, dict) and str(row.get("id", "")).strip()
    }
    matches = 0
    for index, kid in enumerate(returned_probe, start=1):
        row = row_by_id[kid]
        multi_imp = int(float(row.get("impCnt", 0) or 0))
        multi_clk = int(float(row.get("clkCnt", 0) or 0))
        single_imp, single_clk = singular_stats(
            client,
            kid,
            since=since,
            until=until,
            fields=fields,
        )
        matched = (multi_imp, multi_clk) == (single_imp, single_clk)
        matches += int(matched)
        print(
            f"  RETURNED PROBE {index:02d}: multi={multi_imp}/{multi_clk}, "
            f"single={single_imp}/{single_clk}, match={matched}"
        )

    print("")
    print("Diagnostic summary")
    print(f"  Missing probed        : {len(missing_probe):,}")
    print(f"  Confirmed 0/0         : {confirmed_zero:,}")
    print(f"  Unexpected non-zero   : {nonzero:,}")
    print(f"  Unknown singular data : {unknown:,}")
    print(f"  Returned cross-checks : {matches:,}/{len(returned_probe):,} matched")
    print("")
    print("This diagnostic did not modify or delete anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
