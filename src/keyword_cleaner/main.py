from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .naver_api import NaverConfig, NaverSearchAdsClient, NaverSearchAdsError
from .rules import (
    DEFAULT_PROTECTED_SUFFIXES,
    classify_keyword,
    is_protected_keyword,
)
from .storage import append_delete_log, write_scan_backups


KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Naver Search Ads keyword cleaner (safe dry-run by default)"
    )
    parser.add_argument(
        "--campaign",
        default=None,
        help="Optional exact campaign name filter. If omitted, ALL campaigns are scanned.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Recent activity window. Default: RECENT_DAYS or 21.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=None,
        help="Long activity history window. Default: HISTORY_DAYS or 90.",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=None,
        help="Minimum keyword age before deletion can be considered. Default: 21.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify API credentials and list campaigns/ad groups.",
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


def _select_campaigns(campaigns: list[dict], exact_name: str | None) -> list[dict]:
    if exact_name:
        target = exact_name.strip()
        matched = [
            c for c in campaigns if str(c.get("name", "")).strip() == target
        ]
        if not matched:
            raise NaverSearchAdsError(f'Campaign "{target}" not found.')
        return matched

    return sorted(
        campaigns,
        key=lambda c: (
            str(c.get("name", "")).casefold(),
            str(c.get("nccCampaignId", "")),
        ),
    )


def _stats_range(days: int) -> tuple[str, str]:
    if days < 1 or days > 90:
        raise ValueError("stats days must be between 1 and 90")
    today = datetime.now(KST).date()
    until = today - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since.isoformat(), until.isoformat()


def _parse_reg_tm(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _keyword_age_days(reg_tm: object) -> int | None:
    registered = _parse_reg_tm(reg_tm)
    if registered is None:
        return None
    age = (datetime.now(KST).date() - registered.date()).days
    return max(age, 0)


def main() -> int:
    load_dotenv()
    args = parse_args()

    # RECENT_DAYS intentionally replaces the old STATS_DAYS setting so an older
    # local .env containing STATS_DAYS=14 cannot accidentally weaken this rule.
    recent_days = args.days or int(os.getenv("RECENT_DAYS", "21"))
    history_days = args.history_days or int(os.getenv("HISTORY_DAYS", "90"))
    min_age_days = args.min_age_days or int(
        os.getenv("MIN_KEYWORD_AGE_DAYS", "21")
    )
    if history_days < recent_days:
        raise ValueError("HISTORY_DAYS must be greater than or equal to RECENT_DAYS")
    if min_age_days < 1:
        raise ValueError("MIN_KEYWORD_AGE_DAYS must be at least 1")

    protected_suffixes = _split_env("PROTECTED_SUFFIXES") or list(
        DEFAULT_PROTECTED_SUFFIXES
    )
    extra_exact_keywords = _split_env("EXTRA_PROTECTED_KEYWORDS")
    backup_dir = os.getenv("BACKUP_DIR", "data/backups")
    delete_log_path = os.getenv("DELETE_LOG_PATH", "logs/deleted_keywords.csv")
    batch_size = int(os.getenv("STATS_BATCH_SIZE", "100"))

    if args.delete and args.confirm_delete != "DELETE":
        print(
            '[STOP] Live deletion requires: --delete --confirm-delete DELETE',
            file=sys.stderr,
        )
        return 2

    try:
        config = NaverConfig.from_env()
        client = NaverSearchAdsClient(config)
        recent_since, until = _stats_range(recent_days)
        history_since, history_until = _stats_range(history_days)

        print("=" * 76)
        print("DAYLAW NAVER KEYWORD CLEANER")
        print(
            f"Campaign scope    : "
            f"{('EXACT: ' + args.campaign) if args.campaign else 'ALL CAMPAIGNS'}"
        )
        print(
            f"Recent window     : {recent_since} ~ {until} "
            f"({recent_days} complete days)"
        )
        print(
            f"History guard     : {history_since} ~ {history_until} "
            f"({history_days} complete days)"
        )
        print(f"Minimum age       : {min_age_days} days")
        print(
            f"Mode              : "
            f"{'CHECK' if args.check else ('LIVE DELETE' if args.delete else 'DRY RUN')}"
        )
        print(f"Protected suffix  : {', '.join(protected_suffixes)}")
        print("=" * 76)

        print("[1/7] Campaigns loading...")
        campaigns = _select_campaigns(client.get_campaigns(), args.campaign)
        if not campaigns:
            print("[DONE] No campaigns found.")
            return 0

        print(f"      Matched campaigns: {len(campaigns):,}")
        for campaign in campaigns:
            print(
                f"      - {campaign.get('name')} "
                f"({campaign.get('nccCampaignId')})"
            )

        print("[2/7] Ad groups loading...")
        campaign_groups: list[tuple[dict, list[dict]]] = []
        total_adgroups = 0
        for campaign in campaigns:
            campaign_id = str(campaign["nccCampaignId"])
            adgroups = client.get_adgroups(campaign_id)
            campaign_groups.append((campaign, adgroups))
            total_adgroups += len(adgroups)
            print(f"      {campaign.get('name')}: {len(adgroups):,} groups")
        print(f"      Total ad groups: {total_adgroups:,}")

        if args.check:
            print("")
            print("[CHECK OK] API credentials and all-campaign lookup are working.")
            print("Nothing was changed or deleted.")
            return 0

        print("[3/7] Keywords loading...")
        collected: list[dict] = []
        raw_records: list[dict] = []
        processed_groups = 0

        for campaign, adgroups in campaign_groups:
            campaign_name = str(campaign.get("name", ""))
            campaign_id = str(campaign["nccCampaignId"])
            for adgroup in adgroups:
                processed_groups += 1
                adgroup_id = str(adgroup["nccAdgroupId"])
                adgroup_name = str(adgroup.get("name", ""))
                keywords = client.get_keywords(adgroup_id)

                for keyword in keywords:
                    keyword_id = str(keyword.get("nccKeywordId", "")).strip()
                    if not keyword_id:
                        continue
                    reg_tm = keyword.get("regTm")
                    collected.append(
                        {
                            "campaign_name": campaign_name,
                            "campaign_id": campaign_id,
                            "adgroup_name": adgroup_name,
                            "adgroup_id": adgroup_id,
                            "keyword": str(keyword.get("keyword", "")),
                            "keyword_id": keyword_id,
                            "bid_amt": keyword.get("bidAmt"),
                            "use_group_bid_amt": keyword.get("useGroupBidAmt"),
                            "reg_tm": reg_tm,
                            "age_days": _keyword_age_days(reg_tm),
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

                if (
                    processed_groups == 1
                    or processed_groups % 25 == 0
                    or processed_groups == total_adgroups
                ):
                    print(
                        f"      {processed_groups:,}/{total_adgroups:,} groups, "
                        f"{len(collected):,} keywords"
                    )

        print(f"      Total keywords: {len(collected):,}")
        if not collected:
            print("[DONE] No keywords found.")
            return 0

        keyword_ids = [row["keyword_id"] for row in collected]
        print(f"[4/7] Recent {recent_days}-day stats loading...")
        recent_stats = client.get_keyword_stats(
            keyword_ids,
            since=recent_since,
            until=until,
            batch_size=batch_size,
        )

        # Fetch the heavier 90-day history only for items that could otherwise
        # become delete candidates. Core, young, unknown-age, or recently active
        # keywords never need this second query.
        history_candidate_ids: list[str] = []
        for item in collected:
            protected, _ = is_protected_keyword(
                adgroup_name=item["adgroup_name"],
                keyword=item["keyword"],
                protected_suffixes=protected_suffixes,
                extra_exact_keywords=extra_exact_keywords,
            )
            recent = recent_stats.get(
                item["keyword_id"], {"impCnt": 0, "clkCnt": 0}
            )
            if protected:
                continue
            if item["age_days"] is None or item["age_days"] < min_age_days:
                continue
            if recent["impCnt"] > 0 or recent["clkCnt"] > 0:
                continue
            history_candidate_ids.append(item["keyword_id"])

        print(
            f"[5/7] {history_days}-day history guard loading for "
            f"{len(history_candidate_ids):,} zero-activity eligible keywords..."
        )
        if history_candidate_ids:
            history_stats = client.get_keyword_stats(
                history_candidate_ids,
                since=history_since,
                until=history_until,
                batch_size=batch_size,
            )
        else:
            history_stats = {}

        print("[6/7] Classifying and backing up...")
        output_rows: list[dict] = []
        candidates: list[dict] = []

        for item in collected:
            recent = recent_stats.get(
                item["keyword_id"], {"impCnt": 0, "clkCnt": 0}
            )
            history = history_stats.get(
                item["keyword_id"], {"impCnt": 0, "clkCnt": 0}
            )
            decision = classify_keyword(
                adgroup_name=item["adgroup_name"],
                keyword=item["keyword"],
                age_days=item["age_days"],
                recent_impressions=recent["impCnt"],
                recent_clicks=recent["clkCnt"],
                history_impressions=history["impCnt"],
                history_clicks=history["clkCnt"],
                min_age_days=min_age_days,
                recent_days=recent_days,
                history_days=history_days,
                protected_suffixes=protected_suffixes,
                extra_exact_keywords=extra_exact_keywords,
            )

            row = {
                **{key: value for key, value in item.items() if key != "raw"},
                "recent_impressions": recent["impCnt"],
                "recent_clicks": recent["clkCnt"],
                "history_impressions": history["impCnt"],
                "history_clicks": history["clkCnt"],
                "status": decision.status,
                "reason": decision.reason,
                "protected": decision.protected,
                "recent_stats_since": recent_since,
                "recent_stats_until": until,
                "history_stats_since": history_since,
                "history_stats_until": history_until,
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
        reason_counts = Counter(
            str(row["reason"]).split(":", 1)[0] for row in output_rows
        )
        print("")
        print("RESULT")
        print(f"  CAMPAIGNS         : {len(campaigns):,}")
        print(f"  AD GROUPS         : {total_adgroups:,}")
        print(f"  KEYWORDS          : {len(output_rows):,}")
        print(f"  KEEP              : {counts['KEEP']:,}")
        print(f"  WATCH             : {counts['WATCH']:,}")
        print(f"  DELETE_CANDIDATE  : {counts['DELETE_CANDIDATE']:,}")
        print(f"  CORE/WHITELIST    : {reason_counts['core'] + reason_counts['extra_whitelist']:,}")
        print(f"  YOUNG < {min_age_days}d       : {reason_counts['young_keyword']:,}")
        print(f"  AGE UNKNOWN       : {sum(1 for r in output_rows if r['reason'] == 'safety:registration_age_unknown'):,}")
        print(f"  RECENT ACTIVITY   : {reason_counts['recent_activity']:,}")
        print(f"  90D HISTORY GUARD : {reason_counts['historical_activity']:,}")
        print(f"  CSV backup        : {csv_path}")
        print(f"  JSON raw backup   : {json_path}")

        if not args.delete:
            print("")
            print("[SAFE STOP] Nothing was deleted. This was a dry run.")
            print(
                "Delete candidates passed ALL guards: not core, old enough, "
                "recent 0/0, and history 0/0."
            )
            return 0

        if not candidates:
            print("[DONE] No delete candidates.")
            return 0

        max_delete = args.max_delete
        if max_delete > 0:
            candidates = candidates[:max_delete]
            print(f"[SAFETY CAP] Live deletion limited to first {max_delete} candidates.")

        print("[7/7] Revalidating candidates immediately before deletion...")
        recheck_ids = [row["keyword_id"] for row in candidates]
        recent_recheck = client.get_keyword_stats(
            recheck_ids,
            since=recent_since,
            until=until,
            batch_size=batch_size,
        )
        history_recheck = client.get_keyword_stats(
            recheck_ids,
            since=history_since,
            until=history_until,
            batch_size=batch_size,
        )

        deleted = 0
        skipped = 0
        failed = 0

        for index, row in enumerate(candidates, start=1):
            recent = recent_recheck.get(
                row["keyword_id"], {"impCnt": 0, "clkCnt": 0}
            )
            history = history_recheck.get(
                row["keyword_id"], {"impCnt": 0, "clkCnt": 0}
            )
            current_age = _keyword_age_days(row.get("reg_tm"))
            decision = classify_keyword(
                adgroup_name=row["adgroup_name"],
                keyword=row["keyword"],
                age_days=current_age,
                recent_impressions=recent["impCnt"],
                recent_clicks=recent["clkCnt"],
                history_impressions=history["impCnt"],
                history_clicks=history["clkCnt"],
                min_age_days=min_age_days,
                recent_days=recent_days,
                history_days=history_days,
                protected_suffixes=protected_suffixes,
                extra_exact_keywords=extra_exact_keywords,
            )

            now = datetime.now(KST).isoformat(timespec="seconds")
            base_log = {
                "deleted_at": now,
                "campaign_name": row["campaign_name"],
                "campaign_id": row["campaign_id"],
                "adgroup_name": row["adgroup_name"],
                "adgroup_id": row["adgroup_id"],
                "keyword": row["keyword"],
                "keyword_id": row["keyword_id"],
                "reg_tm": row.get("reg_tm"),
                "age_days": current_age,
                "recent_impressions": recent["impCnt"],
                "recent_clicks": recent["clkCnt"],
                "history_impressions": history["impCnt"],
                "history_clicks": history["clkCnt"],
            }

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
                        "message": "backup_created_and_all_guards_revalidated",
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
