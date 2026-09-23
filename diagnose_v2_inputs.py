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


def extract_stat_values(payload: object) -> tuple[int | None, int | None, int]:
    """Normalize a singular /stats response to aggregate (impCnt, clkCnt, row_count)."""
    if not isinstance(payload, dict):
        return None, None, 0

    if "impCnt" in payload or "clkCnt" in payload:
        try:
            imp = int(float(payload.get("impCnt", 0) or 0))
            clk = int(float(payload.get("clkCnt", 0) or 0))
            return imp, clk, 1
        except (TypeError, ValueError):
            return None, None, 0

    rows = payload.get("data")
    if isinstance(rows, list):
        imp_total = 0
        clk_total = 0
        valid_rows = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                imp_total += int(float(row.get("impCnt", 0) or 0))
                clk_total += int(float(row.get("clkCnt", 0) or 0))
                valid_rows += 1
            except (TypeError, ValueError):
                return None, None, valid_rows
        if valid_rows:
            return imp_total, clk_total, valid_rows

    return None, None, 0


def singular_stats(
    client: NaverSearchAdsClient,
    keyword_id: str,
    *,
    since: str,
    until: str,
    fields: str,
    all_days: bool,
) -> tuple[int | None, int | None, int]:
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    params: dict[str, object] = {
        "id": keyword_id,
        "fields": fields,
        "timeRange": time_range,
    }
    if all_days:
        params["timeIncrement"] = "allDays"

    payload = client._request(
        "GET",
        "/stats",
        params=params,
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
        print("  CAMPAIGN", campaign.get("name"), short_status(campaign))

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

    payload = client._request(
        "GET",
        "/stats",
        params={
            "ids": keyword_ids,
            "fields": fields,
            "timeRange": time_range,
            "timeIncrement": "allDays",
        },
    )

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
    print("Stats coverage check (30 complete days, timeIncrement=allDays)")
    print(f"  Range          : {since} ~ {until}")
    print(f"  Requested IDs  : {len(keyword_ids):,}")
    print(f"  Returned rows  : {len(rows):,}")
    print(f"  Returned IDs   : {len(returned_ids):,}")
    print(f"  Missing IDs    : {len(missing):,}")

    print("")
    print("Dual singular verification for IDs omitted from multi-id /stats")
    print("  allDays may omit true-zero rows, so each probe is also checked without timeIncrement.")
    missing_probe = missing[:SINGULAR_MISSING_PROBE]
    confirmed_zero = 0
    nonzero = 0
    unknown = 0
    omission_confirmed = 0

    for index, kid in enumerate(missing_probe, start=1):
        all_imp, all_clk, all_rows = singular_stats(
            client,
            kid,
            since=since,
            until=until,
            fields=fields,
            all_days=True,
        )
        legacy_imp, legacy_clk, legacy_rows = singular_stats(
            client,
            kid,
            since=since,
            until=until,
            fields=fields,
            all_days=False,
        )

        if legacy_imp == 0 and legacy_clk == 0:
            label = "ZERO_CONFIRMED"
            confirmed_zero += 1
            if all_rows == 0 and all_imp is None and all_clk is None:
                omission_confirmed += 1
        elif legacy_imp is None or legacy_clk is None:
            label = "UNKNOWN"
            unknown += 1
        else:
            label = "NONZERO"
            nonzero += 1

        print(
            f"  MISSING PROBE {index:02d}: "
            f"allDays={all_imp}/{all_clk} rows={all_rows}; "
            f"legacy={legacy_imp}/{legacy_clk} rows={legacy_rows}; {label}"
        )

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
        single_imp, single_clk, row_count = singular_stats(
            client,
            kid,
            since=since,
            until=until,
            fields=fields,
            all_days=True,
        )
        matched = (multi_imp, multi_clk) == (single_imp, single_clk)
        matches += int(matched)
        print(
            f"  RETURNED PROBE {index:02d}: multi={multi_imp}/{multi_clk}, "
            f"single={single_imp}/{single_clk}, rows={row_count}, match={matched}"
        )

    print("")
    print("Diagnostic summary")
    print(f"  Missing probed          : {len(missing_probe):,}")
    print(f"  Confirmed 0/0           : {confirmed_zero:,}")
    print(f"  allDays zero omissions  : {omission_confirmed:,}")
    print(f"  Unexpected non-zero     : {nonzero:,}")
    print(f"  Unknown singular data   : {unknown:,}")
    print(f"  Returned cross-checks   : {matches:,}/{len(returned_probe):,} matched")
    print("")
    print("This diagnostic did not modify or delete anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
