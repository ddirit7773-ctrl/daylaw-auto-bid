from __future__ import annotations

import re
from tkinter import messagebox

import customtkinter as ctk

from desktop_app_v6 import DesktopAppV6
from src.keyword_cleaner.policy_v2 import CleanerPolicy


class DesktopAppV7(DesktopAppV6):
    """v12 shell: safe immediate first-test approval for up to 20 GENERAL keywords."""

    def __init__(self) -> None:
        super().__init__()
        self._retag_v12(self.sidebar)
        self._add_immediate_first_test_button()
        if hasattr(self, "refresh_delete_queue"):
            self.refresh_delete_queue()

    def _retag_v12(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
                if isinstance(text, str) and "v11" in text:
                    child.configure(text=text.replace("v11", "v12"))
            except Exception:
                pass
            self._retag_v12(child)

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
            approved = sum(1 for row in self.queue_rows if row.get("status") == "DELETE_APPROVED")
            if approved > 0:
                messagebox.showinfo(
                    "안전검토 완료",
                    f"실시간 재검증을 통과한 {approved}개가 삭제 승인 상태가 되었습니다.\n\n"
                    "삭제 대기 화면에서 목록을 확인한 뒤 '실제 삭제 · 최대 20개'를 누르세요.\n"
                    "실제 삭제 직전 동일 조건을 한 번 더 재검증합니다.",
                )
            else:
                messagebox.showwarning(
                    "승인 없음",
                    "20개 중 하나라도 안전조건을 통과하지 못하면 이번 즉시 승인 배치는 전부 중단됩니다.\n"
                    "감사 로그를 확인하거나 새 스캔 후 다시 시도해주세요.",
                )

        self._start_stream_job(
            command=self.backend_command("approve_immediate_20.py", "--max-approve", str(batch)),
            label="즉시 20개 안전검토",
            job_kind="immediate",
            on_success=success,
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
            f"삭제 승인된 키워드 중 최대 {min(batch, approved)}개를 실제 삭제합니다.\n\n"
            "삭제 직전 상태, 보호등급, 30일 통계, 60일 클릭을 다시 확인하며\n"
            "하나라도 안전조건을 통과하지 못하면 전체 배치를 중단합니다.\n\n"
            "계속하시겠습니까?",
        ):
            return
        if not messagebox.askyesno(
            "최종 확인",
            "이 작업은 네이버 광고계정의 키워드를 실제로 삭제합니다.\n"
            "삭제 원본은 복원용 archive에 저장되고 삭제 후 존재 여부도 재검증합니다.\n\n"
            "최대 20개 테스트 삭제를 실행할까요?",
        ):
            return

        def success() -> None:
            self.refresh_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            messagebox.showinfo(
                "삭제 테스트 완료",
                "최대 20개 테스트 배치의 삭제 및 사후검증이 완료되었습니다.\n"
                "삭제 기록 / 복원 화면에서 archive를 확인할 수 있습니다.",
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
        )


def main() -> int:
    app = DesktopAppV7()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
