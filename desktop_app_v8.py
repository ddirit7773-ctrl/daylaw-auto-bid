from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from desktop_app_v4 import APP_ROOT
from desktop_app_v7 import DesktopAppV7


class DesktopAppV8(DesktopAppV7):
    """v14: dedicated safe cleanup lane for keyword-level exposure restrictions."""

    def __init__(self) -> None:
        self._account_count_started_at = 0.0
        self._restricted_stage_started_at = 0.0
        self._account_count_last_done = 0
        super().__init__()
        self._retag_v14(self.sidebar)
        self._add_restricted_buttons()
        if hasattr(self, "refresh_delete_queue"):
            self.refresh_delete_queue()

    def _retag_v14(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
                if isinstance(text, str):
                    updated = text
                    for old in ("v11", "v12", "v13"):
                        updated = updated.replace(old, "v14")
                    if updated != text:
                        child.configure(text=updated)
            except Exception:
                pass
            self._retag_v14(child)

    def _add_restricted_buttons(self) -> None:
        if not hasattr(self, "live_delete_button"):
            return
        bar = self.live_delete_button.master
        self.restricted_delete_button = ctk.CTkButton(
            bar,
            text="노출제한 실제삭제",
            width=145,
            fg_color="#B45309",
            hover_color="#92400E",
            state="disabled",
            command=self.run_restricted_delete,
        )
        self.restricted_delete_button.pack(side="right", padx=(8, 0))

        self.restricted_review_button = ctk.CTkButton(
            bar,
            text="노출제한 20개 검토",
            width=155,
            fg_color="#0F766E",
            hover_color="#115E59",
            command=self.run_restricted_review,
        )
        self.restricted_review_button.pack(side="right", padx=(8, 0))

    @staticmethod
    def _read_csv(path: Path) -> list[dict[str, str]]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fp:
                return list(csv.DictReader(fp))
        except (OSError, csv.Error):
            return []

    def _latest_restricted_file(self, pattern: str, max_age_minutes: float | None = None) -> Path | None:
        folder = APP_ROOT / "data" / "delete_audit"
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        latest = files[-1]
        if max_age_minutes is not None:
            age_minutes = (time.time() - latest.stat().st_mtime) / 60
            if age_minutes > max_age_minutes:
                return None
        return latest

    def _restricted_ready_count(self) -> int:
        path = self._latest_restricted_file("restricted_review_*.csv", 30)
        if path is None:
            return 0
        return sum(1 for row in self._read_csv(path) if row.get("gate_result") == "READY")

    def render_queue_rows(self) -> None:
        super().render_queue_rows()
        if hasattr(self, "restricted_delete_button"):
            ready = self._restricted_ready_count()
            self.restricted_delete_button.configure(
                text=f"노출제한 실제삭제 {ready}개" if ready else "노출제한 실제삭제",
                state="normal" if ready > 0 else "disabled",
            )

    @staticmethod
    def _reason_ko(reason: str) -> str:
        reason = (reason or "").strip()
        if reason == "restricted_general_90d_zero":
            return "삭제 안전조건 통과"
        if reason == "group_safety_cap":
            return "광고그룹 50% 제한 또는 최소 4개 유지 규칙으로 보류"
        if reason.startswith("keyword_lookup_failed"):
            return "현재 키워드 상태를 다시 조회하지 못함"
        if reason == "keyword_text_changed":
            return "스캔 이후 키워드 문구가 변경됨"
        if reason == "adgroup_changed":
            return "스캔 이후 광고그룹이 변경됨"
        if reason in {"parent_not_found", "parent_not_eligible"}:
            return "캠페인/광고그룹이 현재 정상 노출 상태가 아님"
        if reason == "keyword_user_locked":
            return "사용자가 직접 OFF한 키워드라 자동삭제에서 제외"
        if reason == "restriction_cleared":
            return "현재는 노출제한이 풀려 삭제 대상에서 제외"
        if reason.startswith("inspect_not_approved"):
            return "키워드 검수가 승인 완료 상태가 아니라 제외"
        if reason.startswith("tier_changed"):
            return "보호규칙 재확인 결과 GENERAL이 아니어서 제외"
        if reason == "age_under_90d":
            return "등록 후 90일이 지나지 않아 제외"
        if reason == "history_stats_incomplete":
            return "90일 통계를 완전하게 확인하지 못해 제외"
        if reason == "history_90d_activity_detected":
            return "최근 90일 안에 노출 또는 클릭이 있어 제외"
        return f"안전조건 미통과 ({reason})" if reason else "안전조건 미통과"

    def _restricted_review_summary(self) -> str:
        path = self._latest_restricted_file("restricted_review_*.csv", 30)
        if path is None:
            return "검토 결과 파일을 찾지 못했습니다."
        rows = self._read_csv(path)
        ready = [row for row in rows if row.get("gate_result") == "READY"]
        held = [row for row in rows if row.get("gate_result") != "READY"]
        lines = [
            f"노출제한 후보 {len(rows)}개 검토",
            f"삭제 가능 {len(ready)}개 · 보류 {len(held)}개",
        ]
        if held:
            lines.extend(["", "보류 사유:"])
            for row in held[:8]:
                lines.append(
                    f"• {row.get('adgroup_name', '')} / {row.get('keyword', '')}: "
                    f"{self._reason_ko(row.get('gate_reason', ''))}"
                )
            if len(held) > 8:
                lines.append(f"• 외 {len(held) - 8}개")
        return "\n".join(lines)

    def _restricted_delete_summary(self) -> str:
        path = self._latest_restricted_file("restricted_delete_*.csv", 30)
        if path is None:
            return "삭제 결과 파일을 찾지 못했습니다."
        rows = self._read_csv(path)
        deleted = [row for row in rows if row.get("delete_result") == "DELETED"]
        verified = [row for row in rows if row.get("verify_result") == "ABSENT_OK"]
        failed = [
            row for row in rows
            if row.get("delete_result", "").startswith("ERROR")
            or row.get("verify_result") == "STILL_PRESENT"
        ]
        return (
            f"노출제한 키워드 실제 삭제 {len(deleted)}개\n"
            f"삭제 후 사라짐 확인 {len(verified)}개\n"
            f"문제 발생 {len(failed)}개"
        )

    def _consume_progress_line(self, line: str, label: str, job_kind: str) -> None:
        text = line.strip()

        count_match = re.match(r"@@ACCOUNT_COUNT_PROGRESS\|(\d+)\|(\d+)\|(\d+)", text)
        if count_match:
            done = int(count_match.group(1))
            total = max(int(count_match.group(2)), 1)
            keywords = int(count_match.group(3))
            now = time.monotonic()
            if self._account_count_started_at <= 0 or done <= self._account_count_last_done:
                self._account_count_started_at = now
            self._account_count_last_done = done
            elapsed = max(now - self._account_count_started_at, 0.1)
            remaining = elapsed * max(total - done, 0) / max(done, 1)
            ratio = done / total
            pct = 45 + ratio * 35
            self._set_progress(
                pct,
                f"{label} · 실시간 계정 하한선 확인",
                f"광고그룹 {done:,} / {total:,}개 확인 · 현재까지 키워드 {keywords:,}개",
            )
            self.progress_meta.configure(
                text=f"{pct:.0f}% · 약 {self._fmt_seconds(remaining)} 남음"
            )
            return

        restricted_match = re.match(r"@@RESTRICTED_PROGRESS\|(\d+)\|(\d+)", text)
        if restricted_match:
            done = int(restricted_match.group(1))
            total = max(int(restricted_match.group(2)), 1)
            now = time.monotonic()
            if done == 1 or self._restricted_stage_started_at <= 0:
                self._restricted_stage_started_at = now
            elapsed = max(now - self._restricted_stage_started_at, 0.1)
            remaining = elapsed * max(total - done, 0) / max(done, 1)
            ratio = done / total
            pct = 12 + ratio * 30
            self._set_progress(
                pct,
                f"{label} · 노출제한 안전검토",
                f"후보 {done:,} / {total:,}개 · GENERAL/90일 무활동/현재 제한상태 재확인",
            )
            self.progress_meta.configure(
                text=f"{pct:.0f}% · 약 {self._fmt_seconds(remaining)} 남음"
            )
            return

        delete_match = re.match(r"@@RESTRICTED_DELETE_PROGRESS\|(\d+)\|(\d+)", text)
        if delete_match:
            done = int(delete_match.group(1))
            total = max(int(delete_match.group(2)), 1)
            ratio = done / total
            pct = 82 + ratio * 14
            self._set_progress(
                pct,
                f"{label} · 실제 삭제",
                f"안전검증 통과 항목 {done:,} / {total:,}개 처리",
            )
            return

        super()._consume_progress_line(line, label, job_kind)

    def run_restricted_review(self) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 다시 실행해주세요.")
            return
        if not messagebox.askyesno(
            "노출제한 키워드 안전검토",
            "키워드 노출제한 상태를 별도 기준으로 최대 20개 검토합니다.\n\n"
            "첫 단계에서는 GENERAL만 대상으로 하며,\n"
            "• 캠페인/광고그룹 정상\n"
            "• 키워드 자체는 ON\n"
            "• 키워드 상태는 비노출, 검수는 APPROVED\n"
            "• 등록 90일 이상\n"
            "• 최근 90일 노출 0 / 클릭 0\n"
            "• 보호키워드·TYPE_CORE 제외\n"
            "조건을 모두 확인합니다.\n\n"
            "이 단계에서는 실제 삭제하지 않습니다. 계속할까요?",
        ):
            return

        def success() -> None:
            self.refresh_dashboard()
            self.refresh_delete_queue()
            messagebox.showinfo(
                "노출제한 검토 완료",
                self._restricted_review_summary()
                + "\n\n삭제 가능으로 나온 항목만 별도의 '노출제한 실제삭제' 버튼으로 진행할 수 있습니다.",
            )

        def failure(detail: str) -> None:
            if "Latest V2 scan is" in detail:
                text = "최신 스캔이 오래되었습니다. V2 전체 스캔을 새로 실행한 뒤 다시 시도해주세요."
            elif "No file found for v2_scan_" in detail:
                text = "V2 스캔 결과가 없습니다. 먼저 V2 전체 스캔을 실행해주세요."
            else:
                text = "노출제한 안전검토 중 오류가 발생했습니다.\n\n" + detail[-1200:]
            messagebox.showwarning("노출제한 검토 실패", text)

        self._restricted_stage_started_at = 0.0
        self._start_stream_job(
            command=self.backend_command(
                "restricted_cleanup.py",
                "--review",
                "--max-items",
                "20",
            ),
            label="노출제한 20개 안전검토",
            job_kind="restricted",
            on_success=success,
            on_failure=failure,
        )

    def run_restricted_delete(self) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 다시 실행해주세요.")
            return
        ready = self._restricted_ready_count()
        if ready <= 0:
            messagebox.showwarning(
                "삭제 가능 항목 없음",
                "먼저 '노출제한 20개 검토'를 실행해주세요.",
            )
            return

        if not messagebox.askyesno(
            "노출제한 실제 삭제 확인",
            f"검토를 통과한 노출제한 키워드 최대 {ready}개를 실제 삭제 대상으로 다시 검증합니다.\n\n"
            "삭제 직전에 동일 조건을 다시 확인하고 82,000개 하한선과 광고그룹 안전한도를 다시 검사합니다.\n"
            "계속할까요?",
        ):
            return
        if not messagebox.askyesno(
            "최종 확인",
            "실제 네이버 광고계정의 키워드를 삭제합니다.\n"
            "복원 원본은 DELETE 요청 전에 먼저 archive에 저장됩니다.\n\n"
            "진행할까요?",
        ):
            return

        def success() -> None:
            self.refresh_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            messagebox.showinfo(
                "노출제한 삭제 완료",
                self._restricted_delete_summary()
                + "\n\n삭제 기록 / 복원 화면에서 원본 archive를 확인할 수 있습니다.",
            )

        def failure(detail: str) -> None:
            messagebox.showwarning(
                "노출제한 삭제 결과",
                "실제 삭제 과정에서 안전장치 또는 사후검증이 중단했습니다.\n\n"
                + detail[-1500:],
            )

        self._account_count_started_at = 0.0
        self._account_count_last_done = 0
        self._restricted_stage_started_at = 0.0
        self._start_stream_job(
            command=self.backend_command(
                "restricted_cleanup.py",
                "--delete",
                "--confirm",
                "DELETE_RESTRICTED",
                "--max-items",
                "20",
            ),
            label="노출제한 실제삭제",
            job_kind="restricted_delete",
            on_success=success,
            on_failure=failure,
        )


def main() -> int:
    app = DesktopAppV8()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
