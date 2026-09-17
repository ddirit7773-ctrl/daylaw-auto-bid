from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from .rules import DEFAULT_PROTECTED_SUFFIXES, classify_keyword
from .storage import append_delete_log, write_scan_backups


KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Naver Search Ads keyword cleaner (safe dry-run by default)"
    )
    parser.add_argument(
        "--campaign",
        default=None,
        help="Exact campaign name. Default: TARGET_CAMPAIGN_NAME or 서원데이",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Stats window in complete days. Default: STATS_DAYS or 14",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete candidates after backup and revalidation.",
    )
    parser.add_argument(
        "--confirm-delete",
        default="",
        help='Required with --delete. Must equal "DELETE".',
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=0,
        help="Safety cap. 0 means no cap. Recommended for first live run: 20.",
    )
    return parser.parse_args()


def _split_env(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _find_campaign(campaigns: list[dict], target_name: str) -> dict:
    exact = [c for c in campaigns if str(c.get("name", "")).strip() == target_name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        ids = ", ".join(str(c.get("nccCampaignId")) for c in exact)
        raise NaverSearchAdsError(
            f'Campaign name "{target_name}" is duplicated. IDs: {ids}'
        )

    partial = [
        str(c.get("name", ""))
        for c in campaigns
        if target_name.casefold() in str(c.get("name", "")).casefold()
    ][:10]
    hint = f" Similar names: {partial}" if partial else ""
    raise NaverSearchAdsError(f'Campaign "{target_name}" not found.{hint}')


def _stats_range(days: int) -> tuple[str, str]:
    if days < 1 or days > 90:
        raise ValueError("--days must be between 1 and 90")
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since.isoformat(), until.isoformat()


def main() -> int:
    load_dotenv()
    args = parse_args()

    target_campaign_name = (
        args.campaign
        or os.getenv("TARGET_CAMPAIGN_NAME", "").strip()
        or "서원데이"
    )
    days = args.days or int(os.getenv("STATS_DAYS", "14"))
    protected_suffixes = _split_env("PROTECTED_SUFFIXES") or list(
        DEFAULT_PROTECTED_SUFFIXES
    )
    extra_exact_keywords = _split_env("EXTRA_PROTECTED_KEYWORDS")
    backup_dir = os.getenv("BACKUP_DIR", "data/backups")
    delete_log_path = os.getenv("DELETE_LOG_PATH", "logs/deleted_keywords.csv")

    if args.delete and args.confirm_delete != "DELETE":
        print(
            '[STOP] Live deletion requires: --delete --confirm-delete DELETE',
            file=sys.stderr,
        )
        return 2

    try:
        config = NaverConfig.from_env()
        client = NaverSearchAdsClient(config)
        since, until = _stats_range(days)

        print("=" * 72)
        print("DAYLAW NAVER KEYWORD CLEANER")
        print(f"Campaign        : {target_campaign_name}")
        print(f"Stats range     : {since} ~ {until} ({days} complete days)")
        print(f"Mode            : {'LIVE DELETE' if args.delete else 'DRY RUN'}")
        print(f"Protected suffix: {', '.join(protected_suffixes)}")
        print("=" * 72)

        print("[1/6] Campaigns loading...")
        campaign = _find_campaign(client.get_campaigns(), target_campaign_name)
        campaign_id = str(campaign["nccCampaignId"])
        print(f"      Found: {campaign.get('name')} ({campaign_id})")

        print("[2/6] Ad groups loading...")
        adgroups = client.get_adgroups(campaign_id)
        print(f"      Ad groups: {len(adgroups):,}")

        print("[3/6] Keywords loading...")
        collected: list[dict] = []
        raw_records: list[dict] = []

        for index, adgroup in enumerate(adgroups, start=1):
            adgroup_id = str(adgroup["nccAdgroupId"])
            adgroup_name = str(adgroup.get("name", ""))
            keywords = client.get_keywords(adgroup_id)

            for keyword in keywords:
                keyword_id = str(keyword.get("nccKeywordId", "")).strip()
                if not keyword_id:
                    continue
                collected.append(
                    {
                        "campaign_name": target_campaign_name,
                        "campaign_id": campaign_id,
                        "adgroup_name": adgroup_name,
                        "adgroup_id": adgroup_id,
                        "keyword": str(keyword.get("keyword", "")),
                        "keyword_id": keyword_id,
                        "bid_amt": keyword.get("bidAmt"),
                        "use_group_bid_amt": keyword.get("useGroupBidAmt"),
                        "raw": keyword,
                    }
                )
                raw_records.append(
                    {
                        "campaign": campaign,
                        "adgroup": adgroup,
                        "keyword": keyword,
                    }
                )

            if index == 1 or index % 25 == 0 or index == len(adgroups):
                print(
                    f"      {index:,}/{len(adgroups):,} groups, "
                    f"{len(collected):,} keywords"
                )

        print(f"      Total keywords: {len(collected):,}")
        if not collected:
            print("[DONE] No keywords found.")
            return 0

        print("[4/6] Stats loading...")
        keyword_ids = [row["keyword_id"] for row in collected]
        stats = client.get_keyword_stats(
            keyword_ids,
            since=since,
            until=until,
            batch_size=int(os.getenv("STATS_BATCH_SIZE", "100")),
        )

        print("[5/6] Classifying and backing up...")
        output_rows: list[dict] = []
        candidates: list[dict] = []

        for item in collected:
            stat = stats.get(item["keyword_id"], {"impCnt": 0, "clkCnt": 0})
            decision = classify_keyword(
                adgroup_name=item["adgroup_name"],
                keyword=item["keyword"],
                impressions=stat["impCnt"],
                clicks=stat["clkCnt"],
                protected_suffixes=protected_suffixes,
                extra_exact_keywords=extra_exact_keywords,
            )

            row = {
                **{key: value for key, value in item.items() if key != "raw"},
                "impressions": stat["impCnt"],
                "clicks": stat["clkCnt"],
                "status": decision.status,
                "reason": decision.reason,
                "protected": decision.protected,
                "stats_since": since,
                "stats_until": until,
            }
            output_rows.append(row)
            if decision.status == "DELETE_CANDIDATE":
                candidates.append({**row, "raw": item["raw"]})

        csv_path, json_path = write_scan_backups(
            output_rows,
            raw_records,
            backup_dir=backup_dir,
            prefix="keyword_scan",
        )

        counts = Counter(row["status"] for row in output_rows)
        print("")
        print("RESULT")
        print(f"  KEEP             : {counts['KEEP']:,}")
        print(f"  WATCH            : {counts['WATCH']:,}")
        print(f"  DELETE_CANDIDATE : {counts['DELETE_CANDIDATE']:,}")
        print(f"  CSV backup       : {csv_path}")
        print(f"  JSON raw backup  : {json_path}")

        if not args.delete:
            print("")
            print("[SAFE STOP] Nothing was deleted. This was a dry run.")
            print(
                "Review DELETE_CANDIDATE rows first. "
                "Live deletion is disabled unless --delete is explicitly supplied."
            )
            return 0

        if not candidates:
            print("[DONE] No delete candidates.")
            return 0

        max_delete = args.max_delete
        if max_delete > 0:
            candidates = candidates[:max_delete]
            print(f"[SAFETY CAP] Live deletion limited to first {max_delete} candidates.")

        print("[6/6] Revalidating candidates immediately before deletion...")
        recheck_ids = [row["keyword_id"] for row in candidates]
        recheck = client.get_keyword_stats(
            recheck_ids,
            since=since,
            until=until,
            batch_size=int(os.getenv("STATS_BATCH_SIZE", "100")),
        )

        deleted = 0
        skipped = 0
        failed = 0

        for index, row in enumerate(candidates, start=1):
            latest = recheck.get(row["keyword_id"], {"impCnt": 0, "clkCnt": 0})
            now = datetime.now(KST).isoformat(timespec="seconds")
            base_log = {
                "deleted_at": now,
                "campaign_name": row["campaign_name"],
                "campaign_id": row["campaign_id"],
                "adgroup_name": row["adgroup_name"],
                "adgroup_id": row["adgroup_id"],
                "keyword": row["keyword"],
                "keyword_id": row["keyword_id"],
                "impressions": latest["impCnt"],
                "clicks": latest["clkCnt"],
            }

            decision = classify_keyword(
                adgroup_name=row["adgroup_name"],
                keyword=row["keyword"],
                impressions=latest["impCnt"],
                clicks=latest["clkCnt"],
                protected_suffixes=protected_suffixes,
                extra_exact_keywords=extra_exact_keywords,
            )

            if decision.status != "DELETE_CANDIDATE":
                skipped += 1
                append_delete_log(
                    {
                        **base_log,
                        "result": "SKIPPED_REVALIDATION",
                        "message": decision.reason,
                    },
                    log_path=delete_log_path,
                )
                continue

            try:
                client.delete_keyword(row["keyword_id"])
                deleted += 1
                append_delete_log(
                    {
                        **base_log,
                        "result": "DELETED",
                        "message": "backup_created_before_delete",
                    },
                    log_path=delete_log_path,
                )
            except Exception as exc:
                failed += 1
                append_delete_log(
                    {
                        **base_log,
                        "result": "DELETE_FAILED",
                        "message": str(exc)[:500],
                    },
                    log_path=delete_log_path,
                )

            if index % 20 == 0 or index == len(candidates):
                print(
                    f"      {index:,}/{len(candidates):,} checked | "
                    f"deleted={deleted:,}, skipped={skipped:,}, failed={failed:,}"
                )

        print("")
        print(f"[DONE] deleted={deleted:,}, skipped={skipped:,}, failed={failed:,}")
        print(f"Delete log: {delete_log_path}")
        return 0

    except (NaverSearchAdsError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
