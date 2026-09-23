from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

from desktop_app_v3 import DesktopAppV3


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = app_root()


class DesktopAppV4(DesktopAppV3):
    """Packaging-safe desktop app.

    In development it launches the existing Python scripts. In a frozen Windows
    build it relaunches the same executable in hidden backend mode, so the GUI
    does not require a separate Python installation.
    """

    @staticmethod
    def backend_command(filename: str, *args: str) -> list[str]:
        stem = Path(filename).stem
        if getattr(sys, "frozen", False):
            return [sys.executable, "--backend", stem, *args]
        return [sys.executable, str(APP_ROOT / filename), *args]

    def run_script(self, filename: str) -> None:
        try:
            subprocess.Popen(self.backend_command(filename), cwd=str(APP_ROOT))
        except Exception as exc:
            messagebox.showerror("실행 실패", str(exc))
            return
        messagebox.showinfo(
            "실행 시작",
            f"{filename} 실행을 시작했습니다.\n완료 후 화면을 새로고침하면 결과가 반영됩니다.",
        )

    def run_stats_query(self) -> None:
        if self.stats_running:
            return
        try:
            since = datetime.strptime(self.stats_since_var.get().strip(), "%Y-%m-%d").date()
            until = datetime.strptime(self.stats_until_var.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("날짜 오류", "날짜를 YYYY-MM-DD 형식으로 입력해주세요.")
            return
        days = (until - since).days + 1
        if days < 1:
            messagebox.showerror("날짜 오류", "종료일은 시작일보다 빠를 수 없습니다.")
            return
        if days > 90:
            messagebox.showerror("날짜 오류", "현재 직접 통계 조회는 최대 90일까지 지원합니다.")
            return

        args = ["--since", since.isoformat(), "--until", until.isoformat()]
        campaign = self.stats_campaign_var.get().strip()
        if campaign:
            args.extend(["--campaign", campaign])
        command = self.backend_command("query_account_stats.py", *args)

        self.stats_running = True
        self.stats_query_button.configure(state="disabled", text="조회 중...")
        self.stats_status_label.configure(text="네이버 광고 통계를 조회하고 있습니다. 잠시 기다려주세요.")

        def worker() -> None:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(APP_ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "통계 조회 실패").strip()
                    self.after(0, lambda: self.finish_stats_query(False, detail))
                    return
                self.after(0, lambda: self.finish_stats_query(True, "조회 완료"))
            except Exception as exc:
                self.after(0, lambda: self.finish_stats_query(False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def run_live_delete(self) -> None:
        from src.keyword_cleaner.policy_v2 import CleanerPolicy

        policy = CleanerPolicy.load()
        batch = policy.first_test_batch
        if not messagebox.askyesno(
            "실제 삭제 확인",
            f"DELETE_APPROVED 중 최대 {batch}개를 실제 삭제 대상으로 재검증합니다.\n계속하시겠습니까?",
        ):
            return
        if not messagebox.askyesno(
            "최종 확인",
            "실삭제 직전 상태/통계를 다시 확인하며 하나라도 안전조건을 통과하지 못하면 중단됩니다.\n실행할까요?",
        ):
            return
        self._spawn_command(
            self.backend_command(
                "execute_v2_delete.py",
                "--delete",
                "--confirm",
                "DELETE",
                "--max-delete",
                str(batch),
            ),
            "실제 삭제 실행기를 시작했습니다. 결과는 감사 로그에서 확인하세요.",
        )

    def restore_selected(self, live: bool) -> None:
        selected = self.history_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning("선택 필요", "복원할 기록 1개를 선택해주세요.")
            return
        archive_id = selected[0]
        row = next((item for item in self.history_rows if item["archive_id"] == archive_id), None)
        if not row:
            messagebox.showerror("복원 실패", "선택한 archive 정보를 찾을 수 없습니다.")
            return
        if row["restored_at"]:
            messagebox.showinfo("복원 완료", "이미 복원된 기록입니다.")
            return

        args = ["--archive-id", archive_id]
        if live:
            if not messagebox.askyesno(
                "실제 복원 확인",
                f"[{row['keyword']}] 키워드를 실제로 다시 생성하고 검증합니다.\n계속하시겠습니까?",
            ):
                return
            args.extend(["--restore", "--confirm", "RESTORE"])
        self._spawn_command(
            self.backend_command("restore_deleted_keyword.py", *args),
            "복원 실행을 시작했습니다." if live else "복원 DRY RUN을 시작했습니다.",
        )

    def _spawn_command(self, command: list[str], notice: str) -> None:
        try:
            subprocess.Popen(command, cwd=str(APP_ROOT))
        except Exception as exc:
            messagebox.showerror("실행 실패", str(exc))
            return
        messagebox.showinfo("실행 시작", notice)


def main() -> int:
    app = DesktopAppV4()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
