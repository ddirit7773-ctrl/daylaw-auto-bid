from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient


KST = ZoneInfo("Asia/Seoul")
SAMPLE_SIZE = 100


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
    print("This diagnostic did not modify or delete anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
