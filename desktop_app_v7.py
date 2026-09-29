from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from desktop_app_v4 import APP_ROOT
from desktop_app_v6 import DesktopAppV6
from src.keyword_cleaner.policy_v2 import CleanerPolicy


class DesktopAppV7(DesktopAppV6):
    """v13 shell: hardened first-test approval and live-delete safety UI."""

    def __init__(self) -> None:
        super().__init__()
        self._retag_v13(self.sidebar)
        self._add_immediate_first_test_button()
        if hasattr(self, "refresh_delete_queue"):
            self.refresh_delete_queue()

    def _retag_v13(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
                if isinstance(text, str):
                    text = text.replace("v11", "v13").replace("v12", "v13")
                    child.configure(text=text)
            except Exception:
                pass
            self._retag_v13(child)

    def _add_immediate_first_test_button(self) -> None:
        if not hasattr(self, "live_delete_button"):
            return
        bar = self.live_delete_button.master
        self.immediate_test_button = ctk.CTkButton(
            bar,
            text="즉시 20개 안전검토",
            width=170,
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            command=self.run_immediate_first_test,
        )
        self.immediate_test_button.pack(side="right", padx=(8, 0))

    def render_queue_rows(self) -> None:
        super().render_queue_rows()
        approved = sum(1 for row in self.queue_rows if row.get("status") == "DELETE_APPROVED")
        pending_general = sum(
            1
            for row in self.queue_rows
            if row.get("status") == "DELETE_PENDING" and row.get("tier") == "GENERAL"
        )
        if hasattr(self, "immediate_test_button"):
            self.immediate_test_button.configure(
                state="normal" if approved == 0 and pending_general >= 20 else "disabled"
            )
        if hasattr(self, "live_delete_button"):
            self.live_delete_button.configure(
                text="실제 삭제 · 최대 20개",
                state="normal" if approved > 0 else "disabled",
            )

    def _consume_progress_line(self, line: str, label: str, job_kind: str) -> None:
        text = line.strip()

        account_match = re.match(r"@@ACCOUNT_COUNT_PROGRESS\|(\d+)\|(\d+)\|(\d+)", text)
        if account_match:
            done = int(account_match.group(1))
            total = max(int(account_match.group(2)), 1)
            keywords = int(account_match.group(3))
            pct = 95 + (done / total) * 3
            self._set_progress(
                pct,
                f"{label} · 실시간 계정 하한선 확인",
                f"광고그룹 {done:,} / {total:,}개 확인 · 현재까지 키워드 {keywords:,}개",
            )
            return

        match = re.match(r"@@IMMEDIATE_PROGRESS\|(\d+)\|(\d+)", text)
        if match:
            done = int(match.group(1))
            total = max(int(match.group(2)), 1)
            pct = 12 + (done / total) * 82
            self._set_progress(
                pct,
                f"{label} · 실시간 재검증",
                f"후보 {done:,} / {total:,}개 · 30일/60일/90일 통계와 보호 규칙 확인 중",
            )
            return
        super()._consume_progress_line(line, label, job_kind)

    @staticmethod
    def _audit_int(value: object) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _latest_csv(folder: Path, pattern: str, max_age_seconds: int = 600) -> list[dict[str, str]]:
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
        if not files:
            return []
        latest = files[-1]
        if time.time() - latest.stat().st_mtime > max_age_seconds:
            return []
        try:
            with latest.open("r", encoding="utf-8-sig", newline="") as fp:
                return list(csv.DictReader(fp))
        except (OSError, csv.Error):
            return []

    def _latest_immediate_audit(self) -> list[dict[str, str]]:
        return self._latest_csv(APP_ROOT / "data" / "delete_audit", "v2_immediate_approval_*.csv")

    def _latest_delete_manifest(self) -> list[dict[str, str]]:
        return self._latest_csv(APP_ROOT / "data" / "delete_audit", "v2_delete_manifest_*.csv")

    def _gate_reason_ko(self, row: dict[str, str]) -> str:
        reason = (row.get("gate_reason") or "").strip()
        imp30 = self._audit_int(row.get("30d_impressions", row.get("inactivity_impressions")))
        clk30 = self._audit_int(row.get("30d_clicks", row.get("inactivity_clicks")))
        clk60 = self._audit_int(row.get("60d_clicks", row.get("click_window_clicks")))
        imp90 = self._audit_int(row.get("90d_impressions", row.get("history_impressions")))
        clk90 = self._audit_int(row.get("90d_clicks", row.get("history_clicks")))

        if row.get("gate_result") == "READY":
            return "모든 안전조건 통과"
        if reason.startswith("keyword_lookup_failed"):
            return "네이버에서 현재 키워드 상태를 다시 조회하지 못함"
        if reason == "keyword_text_changed":
            return "스캔 이후 키워드 문구가 변경되어 안전하게 제외"
        if reason == "adgroup_changed":
            return "스캔 이후 광고그룹이 변경되어 안전하게 제외"
        if reason == "parent_not_found":
            return "현재 캠페인 또는 광고그룹 상태를 확인하지 못함"
        if reason == "current_exposure_not_eligible":
            return "현재 캠페인/광고그룹/키워드가 정상 노출 가능 상태가 아님"
        if reason.startswith("tier_changed"):
            return "보호 규칙 재확인 결과 일반 삭제 대상이 아니어서 제외"
        if reason == "stats_revalidation_incomplete":
            return "30일/60일/90일 통계를 완전하게 재검증하지 못함"
        if reason in {"recent_30d_activity_detected", "recent_activity_detected"}:
            return f"최근 30일 활동 발견: 노출 {imp30:,}회, 클릭 {clk30:,}회"
        if reason in {"click_within_60d", "click_protection_activity_detected"}:
            return f"최근 60일 안에 클릭 {clk60:,}회가 있어 제외"
        if reason == "history_within_90d":
            return f"최근 90일 활동 발견: 노출 {imp90:,}회, 클릭 {clk90:,}회"
        if reason:
            return f"안전조건 미통과 ({reason})"
        return "안전조건 미통과"

    def _immediate_result_text(self, rows: list[dict[str, str]]) -> str:
        ready = [row for row in rows if row.get("gate_result") == "READY"]
        blocked = [row for row in rows if row.get("gate_result") != "READY"]
        lines = [f"검토 {len(rows)}개 · 승인 {len(ready)}개 · 보류 {len(blocked)}개"]
        if blocked:
            lines.append("")
            lines.append("보류된 키워드와 이유:")
            for row in blocked[:10]:
                group = (row.get("adgroup_name") or "광고그룹 미확인").strip()
                keyword = (row.get("keyword") or "키워드 미확인").strip()
                lines.append(f"• {group} / {keyword}: {self._gate_reason_ko(row)}")
            if len(blocked) > 10:
                lines.append(f"• 외 {len(blocked) - 10}개는 감사 로그에 기록되어 있습니다.")
        return "\n".join(lines)

    def _delete_result_text(self, rows: list[dict[str, str]]) -> str:
        if not rows:
            return "삭제 결과 로그를 읽지 못했습니다."
        deleted = [row for row in rows if row.get("delete_result") == "DELETED"]
        blocked = [row for row in rows if row.get("gate_result") != "READY"]
        failed = [
            row for row in rows
            if (row.get("delete_result") or "").startswith("ERROR:")
            or row.get("verify_result") == "STILL_PRESENT"
        ]
        lines = [
            f"최종검토 {len(rows)}개 · 실제 삭제 {len(deleted)}개 · 보류 {len(blocked)}개 · 오류 {len(failed)}개"
        ]
        if blocked:
            lines.append("")
            lines.append("최종 삭제에서 보류된 키워드:")
            for row in blocked[:8]:
                group = (row.get("adgroup_name") or "광고그룹 미확인").strip()
                keyword = (row.get("keyword") or "키워드 미확인").strip()
                lines.append(f"• {group} / {keyword}: {self._gate_reason_ko(row)}")
        if failed:
            lines.append("")
            lines.append("확인이 필요한 오류 항목이 있습니다. 삭제 기록/복원 화면을 확인해주세요.")
        return "\n".join(lines)

    def _translate_immediate_failure(self, detail: str) -> str:
        rows = self._latest_immediate_audit()
        if rows:
            return (
                "즉시 안전검토가 완료되었지만 삭제 승인 조건을 충족하지 못한 항목이 있습니다.\n\n"
                + self._immediate_result_text(rows)
                + "\n\n실제 삭제는 실행되지 않았습니다."
            )

        text = detail or ""
        mappings = [
            ("No file found for v2_target_delete_plan_", "안전 삭제 계획 파일이 없습니다. 대시보드에서 '안전 삭제 계획 생성'을 먼저 실행해주세요."),
            ("No file found for v2_scan_", "V2 스캔 결과가 없습니다. 먼저 'V2 전체 스캔'을 실행해주세요."),
            ("Latest V2 scan is", "최신 V2 스캔이 30분을 초과했습니다. V2 전체 스캔 후 안전 삭제 계획을 다시 생성해주세요."),
            ("Delete plan is older than the latest scan", "삭제 계획이 최신 스캔보다 오래되었습니다. 안전 삭제 계획을 다시 생성해주세요."),
            ("would exceed 50% delete cap", "해당 광고그룹에서 삭제 비율이 50%를 넘게 되어 안전장치가 중단했습니다."),
            ("would fall below 4 survivors", "해당 광고그룹에 최소 4개 키워드를 남길 수 없어 안전장치가 중단했습니다."),
            ("No candidate passed every live gate", "이번 후보 중 모든 실시간 안전검증을 통과한 키워드가 없습니다."),
            ("READY audit rows no longer match", "검증 직후 스캔/삭제계획 상태가 달라져 승인하지 않았습니다. 새로 스캔 후 다시 시도해주세요."),
        ]
        for needle, korean in mappings:
            if needle in text:
                return korean + "\n\n실제 삭제는 실행되지 않았습니다."
        return "즉시 안전검토 중 오류가 발생했습니다. 실제 삭제는 실행되지 않았습니다.\n\n" + text[-1200:]

    def run_immediate_first_test(self) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 다시 실행해주세요.")
            return

        policy = CleanerPolicy.load()
        batch = policy.first_test_batch
        if not messagebox.askyesno(
            "즉시 20개 안전검토",
            f"삭제 대기 GENERAL 후보 중 최대 {batch}개를 지금 다시 검증합니다.\n\n"
            "이 단계에서는 삭제하지 않습니다.\n"
            "30일 0노출·0클릭 / 60일 0클릭 / 90일 0활동 / 보호규칙 / 그룹 안전한도를 모두 다시 확인합니다.\n\n"
            "계속하시겠습니까?",
        ):
            return

        def success() -> None:
            self.refresh_dashboard()
            self.refresh_delete_queue()
            rows = self._latest_immediate_audit()
            approved = sum(1 for row in self.queue_rows if row.get("status") == "DELETE_APPROVED")
            if approved > 0:
                detail = self._immediate_result_text(rows) if rows else f"삭제 승인 {approved}개"
                messagebox.showinfo(
                    "안전검토 완료",
                    detail
                    + "\n\n승인된 키워드만 '실제 삭제 · 최대 20개' 버튼으로 진행할 수 있습니다."
                    + "\n실제 삭제 직전에 동일한 30일/60일/90일 조건을 다시 확인합니다.",
                )
            else:
                detail = self._immediate_result_text(rows) if rows else "승인된 키워드가 없습니다."
                messagebox.showwarning("승인 없음", detail + "\n\n실제 삭제는 실행되지 않았습니다.")

        def failure(detail: str) -> None:
            messagebox.showwarning("즉시 안전검토 결과", self._translate_immediate_failure(detail))

        self._start_stream_job(
            command=self.backend_command("approve_immediate_20.py", "--max-approve", str(batch)),
            label="즉시 20개 안전검토",
            job_kind="immediate",
            on_success=success,
            on_failure=failure,
        )

    def run_live_delete(self) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 다시 실행해주세요.")
            return
        policy = CleanerPolicy.load()
        batch = policy.first_test_batch
        approved = sum(1 for row in self.queue_rows if row.get("status") == "DELETE_APPROVED")
        if approved <= 0:
            messagebox.showwarning("삭제 승인 없음", "먼저 '즉시 20개 안전검토'를 실행해주세요.")
            return
        if not messagebox.askyesno(
            "실제 삭제 확인",
            f"삭제 승인된 키워드 중 최대 {min(batch, approved)}개를 실제 삭제 대상으로 다시 검증합니다.\n\n"
            "삭제 직전 30일 0활동 / 60일 클릭 0 / 90일 0활동 / 보호등급을 동일하게 다시 확인합니다.\n"
            "실패한 키워드는 보류하고, 독립적으로 모든 조건을 통과한 키워드만 진행합니다.\n\n"
            "계속하시겠습니까?",
        ):
            return
        if not messagebox.askyesno(
            "최종 확인",
            f"네이버 광고계정의 키워드를 실제로 삭제합니다.\n\n"
            f"• 삭제 전 복원 원본을 먼저 저장\n"
            f"• 실시간 계정 키워드 수 재조회\n"
            f"• {policy.cleanup_stop:,}개 아래로 내려가지 않도록 자동 중단\n"
            f"• 삭제 후 실제 제거 여부 재검증\n\n"
            "최대 20개 테스트 삭제를 실행할까요?",
        ):
            return

        def success() -> None:
            self.refresh_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            rows = self._latest_delete_manifest()
            messagebox.showinfo(
                "삭제 테스트 완료",
                self._delete_result_text(rows)
                + "\n\n삭제 전에 저장한 복원 자료는 '삭제 기록 / 복원'에서 확인할 수 있습니다.",
            )

        def failure(detail: str) -> None:
            self.refresh_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            rows = self._latest_delete_manifest()
            summary = self._delete_result_text(rows) if rows else "삭제 작업이 안전장치에 의해 중단되었습니다."
            messagebox.showwarning(
                "삭제 작업 결과",
                summary + "\n\n" + (detail[-900:] if detail else "자세한 내용은 감사 로그를 확인해주세요."),
            )

        self._start_stream_job(
            command=self.backend_command(
                "execute_v2_delete.py",
                "--delete",
                "--confirm",
                "DELETE",
                "--max-delete",
                str(batch),
            ),
            label="실제 삭제 · 최대 20개",
            job_kind="delete",
            on_success=success,
            on_failure=failure,
        )


def main() -> int:
    app = DesktopAppV7()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
