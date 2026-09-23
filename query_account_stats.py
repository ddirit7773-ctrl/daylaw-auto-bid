from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.keyword_cleaner.naver_api import NaverConfig, NaverSearchAdsClient
from src.keyword_cleaner.stats_v2 import get_verified_keyword_stats


KST = ZoneInfo("Asia/Seoul")
OUT_DIR = Path("data/stats")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def default_range() -> tuple[date, date]:
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=6)
    return since, until


def parse_args() -> argparse.Namespace:
    since_default, until_default = default_range()
    parser = argparse.ArgumentParser(description="Read-only account statistics query")
    parser.add_argument("--since", type=parse_date, default=since_default)
    parser.add_argument("--until", type=parse_date, default=until_default)
    parser.add_argument(
        "--campaign",
        default="",
        help="Optional campaign name filter (case-insensitive substring).",
    )
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if args.until < args.since:
        raise SystemExit("종료일은 시작일보다 빠를 수 없습니다.")
    days = (args.until - args.since).days + 1
    if days < 1 or days > 90:
        raise SystemExit("현재 직접 통계 조회는 1~90일 범위만 지원합니다.")

    load_dotenv()
    client = NaverSearchAdsClient(NaverConfig.from_env())

    campaign_filter = args.campaign.strip().casefold()
    campaigns = client.get_campaigns()
    if campaign_filter:
        campaigns = [
            row for row in campaigns
            if campaign_filter in str(row.get("name", "")).casefold()
        ]

    structure: list[dict[str, str]] = []
    adgroup_count = 0
    print("=" * 72)
    print("ACCOUNT STATS QUERY — READ ONLY")
    print(f"Range      : {args.since} ~ {args.until} ({days} days)")
    print(f"Campaigns  : {len(campaigns):,}")

    for campaign in campaigns:
        campaign_id = str(campaign.get("nccCampaignId", ""))
        campaign_name = str(campaign.get("name", ""))
        groups = client.get_adgroups(campaign_id)
        adgroup_count += len(groups)
        for group in groups:
            adgroup_id = str(group.get("nccAdgroupId", ""))
            adgroup_name = str(group.get("name", ""))
            keywords = client.get_keywords(adgroup_id)
            for keyword in keywords:
                keyword_id = str(keyword.get("nccKeywordId", "")).strip()
                if not keyword_id:
                    continue
                structure.append(
                    {
                        "campaign_name": campaign_name,
                        "campaign_id": campaign_id,
                        "adgroup_name": adgroup_name,
                        "adgroup_id": adgroup_id,
                        "keyword": str(keyword.get("keyword", "")),
                        "keyword_id": keyword_id,
                    }
                )
        print(f"  {campaign_name}: {len(groups):,} ad groups, running keywords {len(structure):,}")

    keyword_ids = [row["keyword_id"] for row in structure]
    print(f"Ad groups  : {adgroup_count:,}")
    print(f"Keywords   : {len(keyword_ids):,}")
    print("Fetching keyword stats...")

    stats = get_verified_keyword_stats(
        client,
        keyword_ids,
        since=args.since.isoformat(),
        until=args.until.isoformat(),
    )

    keyword_rows: list[dict[str, object]] = []
    grouped: dict[tuple[str, str], dict[str, object]] = defaultdict(dict)

    for row in structure:
        stat = stats.get(row["keyword_id"])
        impressions = stat.impressions if stat and stat.complete else None
        clicks = stat.clicks if stat and stat.complete else None
        ctr = (clicks / impressions * 100.0) if impressions and clicks is not None else 0.0
        keyword_rows.append(
            {
                **row,
                "since": args.since.isoformat(),
                "until": args.until.isoformat(),
                "impressions": impressions,
                "clicks": clicks,
                "ctr_pct": round(ctr, 4),
                "stats_complete": bool(stat and stat.complete),
                "stats_source": stat.source if stat else "missing",
            }
        )

        key = (row["campaign_id"], row["adgroup_id"])
        bucket = grouped.setdefault(
            key,
            {
                "campaign_name": row["campaign_name"],
                "campaign_id": row["campaign_id"],
                "adgroup_name": row["adgroup_name"],
                "adgroup_id": row["adgroup_id"],
                "keyword_count": 0,
                "impressions": 0,
                "clicks": 0,
                "incomplete_keywords": 0,
            },
        )
        bucket["keyword_count"] = int(bucket["keyword_count"]) + 1
        if impressions is None or clicks is None:
            bucket["incomplete_keywords"] = int(bucket["incomplete_keywords"]) + 1
        else:
            bucket["impressions"] = int(bucket["impressions"]) + impressions
            bucket["clicks"] = int(bucket["clicks"]) + clicks

    adgroup_rows: list[dict[str, object]] = []
    for bucket in grouped.values():
        imp = int(bucket["impressions"])
        clk = int(bucket["clicks"])
        bucket["ctr_pct"] = round((clk / imp * 100.0) if imp else 0.0, 4)
        bucket["since"] = args.since.isoformat()
        bucket["until"] = args.until.isoformat()
        adgroup_rows.append(bucket)

    adgroup_rows.sort(
        key=lambda row: (
            -int(row["clicks"]),
            -int(row["impressions"]),
            str(row["adgroup_name"]).casefold(),
        )
    )
    keyword_rows.sort(
        key=lambda row: (
            -int(row["clicks"] or 0),
            -int(row["impressions"] or 0),
            str(row["keyword"]).casefold(),
        )
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    group_path = OUT_DIR / f"adgroup_stats_{stamp}.csv"
    keyword_path = OUT_DIR / f"keyword_stats_{stamp}.csv"

    write_csv(
        group_path,
        adgroup_rows,
        [
            "campaign_name",
            "campaign_id",
            "adgroup_name",
            "adgroup_id",
            "keyword_count",
            "impressions",
            "clicks",
            "ctr_pct",
            "incomplete_keywords",
            "since",
            "until",
        ],
    )
    write_csv(
        keyword_path,
        keyword_rows,
        [
            "campaign_name",
            "campaign_id",
            "adgroup_name",
            "adgroup_id",
            "keyword",
            "keyword_id",
            "impressions",
            "clicks",
            "ctr_pct",
            "stats_complete",
            "stats_source",
            "since",
            "until",
        ],
    )

    total_imp = sum(int(row["impressions"]) for row in adgroup_rows)
    total_clk = sum(int(row["clicks"]) for row in adgroup_rows)
    total_ctr = (total_clk / total_imp * 100.0) if total_imp else 0.0
    incomplete = sum(int(row["incomplete_keywords"]) for row in adgroup_rows)

    print("")
    print("RESULT")
    print(f"Campaigns             : {len(campaigns):,}")
    print(f"Ad groups             : {adgroup_count:,}")
    print(f"Keywords              : {len(keyword_ids):,}")
    print(f"Impressions           : {total_imp:,}")
    print(f"Clicks                : {total_clk:,}")
    print(f"CTR                   : {total_ctr:.2f}%")
    print(f"Incomplete keywords   : {incomplete:,}")
    print(f"Ad-group CSV          : {group_path}")
    print(f"Keyword CSV           : {keyword_path}")
    print("")
    print("Read-only query complete. Nothing was modified or deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
