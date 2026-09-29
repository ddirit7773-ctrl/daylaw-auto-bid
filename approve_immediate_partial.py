from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import approve_immediate_20


KST = ZoneInfo("Asia/Seoul")
BACKUP_DIR = Path("data/backups")
AUDIT_DIR = Path("data/delete_audit")
MAX_AUDIT_AGE_SECONDS = 180


def latest_file(folder: Path, pattern: str) -> Path:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"No file found for {pattern}")
    return files[-1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise SystemExit(f"Refusing to write empty CSV: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    result = int(approve_immediate_20.main() or 0)
    if result == 0:
        return 0
    if result != 2:
        return result

    audit_path = latest_file(AUDIT_DIR, "v2_immediate_approval_*.csv")
    audit_age = time.time() - audit_path.stat().st_mtime
    if audit_age > MAX_AUDIT_AGE_SECONDS:
        print(f"[SAFE STOP] Latest immediate-review audit is too old: {audit_age:.0f}s")
        return 2

    audit_rows = read_csv(audit_path)
    ready_ids = {
        row.get("keyword_id", "")
        for row in audit_rows
        if row.get("gate_result") == "READY" and row.get("keyword_id")
    }
    blocked_rows = [row for row in audit_rows if row.get("gate_result") != "READY" and row.get("keyword_id")]
    blocked_ids = {row.get("keyword_id", "") for row in blocked_rows}
    blocked_reason_by_id = {
        row.get("keyword_id", ""): (row.get("gate_reason") or "live_gate_failed")
        for row in blocked_rows
    }
    if not ready_ids:
        print("[SAFE STOP] No candidate passed every live gate. Nothing was approved.")
        return 2

    scan_path = latest_file(BACKUP_DIR, "v2_scan_*.csv")
    plan_path = latest_file(BACKUP_DIR, "v2_target_delete_plan_*.csv")
    scan_rows = read_csv(scan_path)
    plan_rows = read_csv(plan_path)

    valid_scan_ids = {
        row.get("keyword_id", "")
        for row in scan_rows
        if row.get("keyword_id") in ready_ids
        and row.get("tier") == "GENERAL"
        and row.get("status") == "DELETE_PENDING"
    }
    valid_plan_ids = {
        row.get("keyword_id", "")
        for row in plan_rows
        if row.get("keyword_id") in ready_ids
        and row.get("tier") == "GENERAL"
        and row.get("plan_action") == "TARGET_DELETE_AFTER_APPROVAL"
    }
    approved_ids = ready_ids & valid_scan_ids & valid_plan_ids
    if not approved_ids:
        print("[SAFE STOP] READY audit rows no longer match the current scan/plan.")
        return 2

    new_scan_rows: list[dict[str, object]] = []
    for row in scan_rows:
        out = dict(row)
        kid = row.get("keyword_id", "")
        if kid in approved_ids:
            out["status"] = "DELETE_APPROVED"
            out["reason"] = "immediate_live_revalidated_partial_approval"
        elif kid in blocked_ids and row.get("status") == "DELETE_PENDING":
            # Keep failed rows out of the very next 20-candidate batch. A future
            # full V2 scan may classify them again from fresh stats.
            out["status"] = "WATCH"
            out["reason"] = "immediate_review_hold:" + blocked_reason_by_id.get(kid, "live_gate_failed")
        new_scan_rows.append(out)

    new_plan_rows: list[dict[str, object]] = []
    for row in plan_rows:
        out = dict(row)
        kid = row.get("keyword_id", "")
        if kid in approved_ids:
            out["status"] = "DELETE_APPROVED"
            out["plan_reason"] = str(out.get("plan_reason", "")) + "; immediate live-safe partial approval"
        elif kid in blocked_ids:
            out["status"] = "WATCH"
            out["plan_action"] = "LIVE_REVIEW_HOLD"
            out["plan_reason"] = (
                str(out.get("plan_reason", ""))
                + "; immediate review hold: "
                + blocked_reason_by_id.get(kid, "live_gate_failed")
            )
        new_plan_rows.append(out)

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    scan_out = BACKUP_DIR / f"v2_scan_{stamp}.csv"
    plan_out = BACKUP_DIR / f"v2_target_delete_plan_{stamp}.csv"
    write_csv(scan_out, new_scan_rows)
    write_csv(plan_out, new_plan_rows)

    print("")
    print(f"[PARTIAL APPROVAL] Approved safe rows : {len(approved_ids):,}")
    print(f"[PARTIAL APPROVAL] Held blocked rows  : {len(blocked_ids):,}")
    print(f"Updated scan                         : {scan_out}")
    print(f"Updated plan                         : {plan_out}")
    print("[OK] Safe rows were approved; blocked rows were moved to WATCH hold.")
    print("Blocked rows will not be selected again in the next immediate batch.")
    print("A future full V2 scan can reconsider them using fresh statistics.")
    print("Nothing was deleted. The live delete executor will revalidate approved rows again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
