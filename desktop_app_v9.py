from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from desktop_app_v4 import APP_ROOT
from desktop_app_v8 import DesktopAppV8
from src.keyword_cleaner.lifecycle_store import DEFAULT_DB_PATH
from src.keyword_cleaner.policy_v2 import CleanerPolicy


class DesktopAppV9(DesktopAppV8):
    """v15 production shell: one-click full scan and checkpointed safe cleanup."""

    def __init__(self) -> None:
        self._auto_stage_started_at = 0.0
        self._auto_delete_started_at = 0.0
        self._auto_stats_stage = ""
        super().__init__()
        self._retag_v15(self.sidebar)
        self._add_production_cleanup_buttons()
        if hasattr(self, "history_tree"):
            try:
                self.history_tree.heading("state", text="삭제 / 복원 상태")
            except Exception:
                pass

    def _retag_v15(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
                if isinstance(text, str) and "v14" in text:
                    child.configure(text=text.replace("v14", "v15"))
            except Exception:
                pass
            self._retag_v15(child)

    def _add_production_cleanup_buttons(self) -> None:
        # Dashboard: make the production flow the primary action.
        if hasattr(self, "status_text"):
            card = self.status_text.master
            self.production_dashboard_button = ctk.CTkButton(
                card,
                text="전체 안전 정리  ·  스캔부터 82,000개까지 자동",
                height=50,
                corner_radius=10,
                fg_color="#B91C1C",
                hover_color="#991B1B",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=self.run_production_cleanup,
            )
            try:
                self.production_dashboard_button.pack(
                    fill="x",
                    padx=20,
                    pady=5,
                    before=self.status_text,
                )
            except Exception:
                self.production_dashboard_button.pack(fill="x", padx=20, pady=5)

            self.production_dashboard_stop = ctk.CTkButton(
                card,
                text="안전 중지 요청",
                height=38,
                corner_radius=9,
                fg_color="#475569",
                hover_color="#334155",
                state="disabled",
                command=self._request_auto_stop,
            )
            try:
                self.production_dashboard_stop.pack(
                    fill="x",
                    padx=20,
                    pady=(2, 5),
                    before=self.status_text,
                )
            except Exception:
                self.production_dashboard_stop.pack(fill="x", padx=20, pady=(2, 5))

        # Delete queue: keep the legacy/test controls available, but put one
        # production button beside them so normal operation is a single action.
        if hasattr(self, "live_delete_button"):
            bar = self.live_delete_button.master
            # v15 is the production path. Keep legacy functions in code for
            # recovery/debugging, but remove the old 20-item test controls from
            # the normal operator screen so there is one obvious delete action.
            for child in list(bar.winfo_children()):
                try:
                    text = str(child.cget("text") or "")
                except Exception:
                    continue
                if (
                    text.startswith("즉시 20개")
                    or text.startswith("노출제한 20개")
                    or text.startswith("노출제한 실제삭제")
                    or text.startswith("실제 삭제 · 최대 20개")
                    or text == "삭제 실행기 DRY RUN"
                ):
                    try:
                        child.pack_forget()
                    except Exception:
                        pass

            self.production_queue_button = ctk.CTkButton(
                bar,
                text="전체 안전 정리",
                width=145,
                fg_color="#7F1D1D",
                hover_color="#991B1B",
                font=ctk.CTkFont(size=12, weight="bold"),
                command=self.run_production_cleanup,
            )
            self.production_queue_button.pack(side="right", padx=(8, 0))
            self.production_queue_stop = ctk.CTkButton(
                bar,
                text="안전 중지",
                width=100,
                fg_color="#475569",
                hover_color="#334155",
                state="disabled",
                command=self._request_auto_stop,
            )
            self.production_queue_stop.pack(side="right", padx=(8, 0))

    def _set_auto_stop_state(self, state: str) -> None:
        for name in ("production_dashboard_stop", "production_queue_stop"):
            button = getattr(self, name, None)
            if button is not None:
                try:
                    button.configure(state=state)
                except Exception:
                    pass

    def _request_auto_stop(self) -> None:
        stop_file = APP_ROOT / "data" / "state" / "auto_cleanup.stop"
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop\n", encoding="utf-8")
        self._set_auto_stop_state("disabled")
        messagebox.showinfo(
            "안전 중지 요청",
            "중지 요청을 전달했습니다.\n현재 DELETE가 진행 중이면 현재 항목/체크포인트를 안전하게 마무리한 뒤 중단합니다.",
        )

    @staticmethod
    def _latest_auto_summary(max_age_seconds: int = 24 * 3600) -> dict[str, str] | None:
        folder = APP_ROOT / "data" / "delete_audit"
        files = sorted(folder.glob("auto_cleanup_summary_*.csv"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        latest = files[-1]
        if time.time() - latest.stat().st_mtime > max_age_seconds:
            return None
        try:
            with latest.open("r", encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
            return rows[0] if rows else None
        except (OSError, csv.Error):
            return None

    @staticmethod
    def _num(value: object) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def _auto_summary_text(self) -> str:
        row = self._latest_auto_summary()
        if not row:
            return "자동 정리 결과 요약 파일을 찾지 못했습니다."

        stop_map = {
            "cleanup_stop_reached": "82,000개 정리 목표에 도달",
            "cleanup_stop_reached_after_recount": "최종 실시간 재확인 후 82,000개 목표에 도달",
            "completed_ready_subset": "이번 실행의 안전 통과 후보를 모두 처리",
            "delete_error_limit_reached": "삭제 API 오류가 3건 발생해 안전 중단",
            "post_delete_verification_failed": "삭제 후 확인 실패가 발견되어 안전 중단",
            "user_stop_requested": "사용자 안전 중지 요청으로 종료",
        }
        reason = row.get("stop_reason", "")
        return (
            f"실제 삭제 {self._num(row.get('deleted')):,}개\n"
            f"삭제 후 사라짐 확인 {self._num(row.get('verified')):,}개\n"
            f"삭제 오류 {self._num(row.get('delete_errors')):,}개 · "
            f"사후검증 오류 {self._num(row.get('verify_failures')):,}개\n"
            f"정리 후 예상 키워드 {self._num(row.get('estimated_keywords_after')):,}개\n"
            f"종료 사유: {stop_map.get(reason, reason or '완료')}"
        )

    def _consume_progress_line(self, line: str, label: str, job_kind: str) -> None:
        text = line.strip()
        if job_kind != "auto_cleanup":
            super()._consume_progress_line(line, label, job_kind)
            return

        now = time.monotonic()

        stage = re.match(r"@@AUTO_STAGE\|([^|]+)\|(\d+)", text)
        if stage:
            name = stage.group(1)
            value = int(stage.group(2))
            self._auto_stage_started_at = now
            stages = {
                "SCAN": (3, "전체 계정 스캔", "최신 키워드와 활동 통계를 다시 확인합니다."),
                "SCAN_DONE": (31, "전체 계정 스캔 완료", f"{value:,}개 키워드 분류가 완료되었습니다."),
                "MASTER": (33, "노출제한 목록 확인", "네이버 키워드 마스터 보고서를 생성하고 있습니다."),
                "MASTER_DONE": (37, "노출제한 목록 확인 완료", f"노출제한 키워드 {value:,}개를 확인했습니다."),
                "ACCOUNT_SNAPSHOT": (39, "실시간 계정 재확인", "현재 광고그룹/키워드 상태를 다시 읽습니다."),
                "CANDIDATES": (49, "삭제 후보 선별", f"안전검증 대상 {value:,}개를 선별했습니다."),
                "FINAL_SNAPSHOT": (63, "삭제 직전 계정 재확인", "삭제 직전 현재 상태를 다시 읽고 있습니다."),
                "FINAL_MASTER": (67, "노출제한 최종 재확인", "노출제한 목록을 삭제 직전에 다시 확인합니다."),
            }
            pct, title, detail = stages.get(name, (self._job_percent, label, text))
            self._set_progress(pct, f"{label} · {title}", detail)
            return

        account = re.match(r"@@AUTO_ACCOUNT_PROGRESS\|([^|]+)\|(\d+)\|(\d+)\|(\d+)", text)
        if account:
            phase, done_s, total_s, keywords_s = account.groups()
            done = int(done_s)
            total = max(int(total_s), 1)
            keywords = int(keywords_s)
            ratio = done / total
            if phase == "PRECHECK":
                start, end = 39, 48
                title = "실시간 계정 구조 확인"
            elif phase == "FINAL":
                start, end = 63, 66
                title = "삭제 직전 계정 구조 확인"
            else:
                start, end = 96, 98
                title = "82,000개 하한선 최종 재확인"
            pct = start + ratio * (end - start)
            elapsed = max(now - self._auto_stage_started_at, 0.1)
            remaining = elapsed * max(total - done, 0) / max(done, 1)
            self._set_progress(
                pct,
                f"{label} · {title}",
                f"광고그룹 {done:,}/{total:,}개 · 현재까지 키워드 {keywords:,}개",
            )
            self.progress_meta.configure(
                text=f"{pct:.0f}% · 약 {self._fmt_seconds(remaining)} 남음"
            )
            return

        master = re.match(r"@@RESTRICTED_MASTER_PROGRESS\|([^|]+)\|(\d+)", text)
        if master:
            status, elapsed = master.groups()
            ko = {
                "REGIST": "생성 요청",
                "RUNNING": "생성 중",
                "BUILT": "생성 완료",
                "NONE": "데이터 없음",
                "ERROR": "오류",
                "WAITING": "대기",
            }.get(status, status)
            self.progress_detail.configure(
                text=f"네이버 키워드 마스터 보고서 {ko} · {int(elapsed):,}초 경과"
            )
            return

        phase = re.match(r"@@AUTO_PHASE\|([^|]+)\|([^|]+)\|(\d+)", text)
        if phase:
            round_name, window, count_s = phase.groups()
            count = int(count_s)
            self._auto_stats_stage = f"{round_name}:{window}"
            base = {
                "PRECHECK:30d": 50,
                "PRECHECK:60d": 54,
                "PRECHECK:90d": 58,
                "FINAL:30d": 68,
                "FINAL:60d": 70,
                "FINAL:90d": 72,
            }.get(self._auto_stats_stage, 50)
            self._set_progress(
                base,
                f"{label} · {'1차' if round_name == 'PRECHECK' else '최종'} 활동 재검증",
                f"{count:,}개 후보의 {window} 통계를 안전 고속 조회합니다.",
            )
            return

        stats = re.match(r"@@STATS_PROGRESS\|(\d+)\|(\d+)", text)
        if stats:
            done = int(stats.group(1))
            total = max(int(stats.group(2)), 1)
            ranges = {
                "PRECHECK:30d": (50, 53.5),
                "PRECHECK:60d": (54, 57.5),
                "PRECHECK:90d": (58, 62),
                "FINAL:30d": (68, 69.5),
                "FINAL:60d": (70, 71.5),
                "FINAL:90d": (72, 74),
            }
            start, end = ranges.get(self._auto_stats_stage, (50, 74))
            pct = start + (done / total) * (end - start)
            self._set_progress(
                pct,
                self.progress_title.cget("text"),
                f"통계 배치 {done:,}/{total:,} 완료",
            )
            return

        deletion = re.match(r"@@AUTO_DELETE_PROGRESS\|(\d+)\|(\d+)\|(\d+)", text)
        if deletion:
            done = int(deletion.group(1))
            total = max(int(deletion.group(2)), 1)
            live_count = int(deletion.group(3))
            if self._auto_delete_started_at <= 0:
                self._auto_delete_started_at = now
            pct = 75 + (done / total) * 22
            elapsed = max(now - self._auto_delete_started_at, 0.1)
            remaining = elapsed * max(total - done, 0) / max(done, 1)
            self._set_progress(
                pct,
                f"{label} · 안전 삭제 진행",
                f"삭제 {done:,}/{total:,}개 · 현재 예상 키워드 {live_count:,}개 · 50개 단위 사후검증",
            )
            self.progress_meta.configure(
                text=f"{pct:.0f}% · 약 {self._fmt_seconds(remaining)} 남음"
            )
            return

        checkpoint = re.match(
            r"@@AUTO_CHECKPOINT\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)",
            text,
        )
        if checkpoint:
            deleted, verified, errors, verify_errors, live_count = map(int, checkpoint.groups())
            self.progress_detail.configure(
                text=(
                    f"체크포인트 완료 · 삭제 {deleted:,} · 확인 {verified:,} · "
                    f"삭제오류 {errors:,} · 검증오류 {verify_errors:,} · 예상 {live_count:,}개"
                )
            )
            return

        # Reuse the familiar scan phase messages while the one-click backend is
        # running the read-only V2 scan internally.
        if "[1/6]" in text:
            self._set_progress(4, f"{label} · 전체 계정 구조 조회", "캠페인/광고그룹/키워드를 불러옵니다.")
            return
        if "[2/6]" in text:
            self._set_progress(10, f"{label} · 일반 키워드 통계", text)
            return
        if "[3/6]" in text:
            self._set_progress(17, f"{label} · 유형 핵심 통계", text)
            return
        if "[4/6]" in text:
            self._set_progress(22, f"{label} · 클릭 보호 이력", text)
            return
        if "[5/6]" in text:
            self._set_progress(28, f"{label} · 안전등급 판정", "보호/유지/관찰/삭제대기를 계산합니다.")
            return
        if "[6/6]" in text:
            self._set_progress(31, f"{label} · 전체 스캔 완료", "대량 삭제 안전검증으로 넘어갑니다.")
            return

        fast = re.match(r"@@FAST_STATS\|workers=(\d+)\|batch_size=(\d+)\|batches=(\d+)", text)
        if fast:
            self.progress_detail.configure(
                text=f"안전 고속 통계 · {fast.group(1)}개 동시 · {fast.group(2)}개/배치 · {int(fast.group(3)):,}배치"
            )
            return

    def refresh_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        self.history_rows = []
        if not DEFAULT_DB_PATH.exists():
            self.history_summary.configure(text="삭제 완료 0개")
            return

        conn = sqlite3.connect(DEFAULT_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM keyword_archive ORDER BY archive_id DESC LIMIT 5000"
            ).fetchall()
        finally:
            conn.close()

        state_ko = {
            "PREPARED": "삭제 전 백업",
            "DELETE_REQUESTED": "삭제 요청 중",
            "DELETE_API_OK": "삭제 응답 완료 · 검증대기",
            "DELETED_VERIFIED": "삭제 완료",
            "DELETE_FAILED": "삭제 실패",
            "VERIFY_FAILED": "삭제 후 확인 실패",
        }

        for dbrow in rows:
            row = dict(dbrow)
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            plan = payload.get("plan") or {}
            delete_status = str(row.get("delete_status") or "DELETED_VERIFIED")
            restored_at = str(row.get("restored_at") or "")
            state = state_ko.get(delete_status, delete_status)
            if restored_at:
                state += " · 복원 완료"
            elif delete_status == "DELETED_VERIFIED":
                state += " · 미복원"

            item = {
                "archive_id": str(row.get("archive_id", "")),
                "deleted_at": str(row.get("deleted_at", "")),
                "campaign_name": str(plan.get("campaign_name") or row.get("campaign_id") or ""),
                "adgroup_name": str(plan.get("adgroup_name") or row.get("adgroup_id") or ""),
                "keyword": str(row.get("keyword") or ""),
                "restored_at": restored_at,
                "delete_status": delete_status,
            }
            self.history_rows.append(item)
            self.history_tree.insert(
                "",
                "end",
                iid=item["archive_id"],
                values=(
                    item["archive_id"],
                    item["deleted_at"],
                    item["campaign_name"],
                    item["adgroup_name"],
                    item["keyword"],
                    state,
                ),
            )

        deleted = sum(1 for row in self.history_rows if row["delete_status"] == "DELETED_VERIFIED")
        attention = sum(
            1
            for row in self.history_rows
            if row["delete_status"] in {"DELETE_FAILED", "VERIFY_FAILED", "DELETE_REQUESTED", "DELETE_API_OK"}
        )
        restored = sum(1 for row in self.history_rows if row["restored_at"])
        self.history_summary.configure(
            text=f"삭제 완료 {deleted:,}개 · 확인 필요 {attention:,}개 · 복원 완료 {restored:,}개"
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
        if row.get("delete_status") != "DELETED_VERIFIED":
            messagebox.showwarning(
                "복원 대상 아님",
                "삭제 완료와 사후검증까지 확인된 항목만 복원할 수 있습니다.",
            )
            return
        if row.get("restored_at"):
            messagebox.showinfo("복원 완료", "이미 복원된 기록입니다.")
            return

        args = ["--archive-id", archive_id]
        if live:
            if not messagebox.askyesno(
                "실제 복원 확인",
                f"[{row.get('keyword', '')}] 키워드를 실제로 다시 생성하고 검증합니다.\n계속하시겠습니까?",
            ):
                return
            args.extend(["--restore", "--confirm", "RESTORE"])

        def success() -> None:
            self.refresh_history()
            messagebox.showinfo(
                "복원 완료" if live else "복원 DRY RUN 완료",
                "키워드 복원과 검증이 완료되었습니다." if live else "복원 가능 여부 확인이 완료되었습니다.",
            )

        def failure(detail: str) -> None:
            self.refresh_history()
            messagebox.showwarning("복원 실패", detail[-1500:] if detail else "복원 작업이 중단되었습니다.")

        self._start_stream_job(
            command=self.backend_command("restore_deleted_keyword.py", *args),
            label="키워드 실제 복원" if live else "키워드 복원 DRY RUN",
            job_kind="generic",
            on_success=success,
            on_failure=failure,
        )

    def run_production_cleanup(self) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 다시 실행해주세요.")
            return

        policy = CleanerPolicy.load()
        if not messagebox.askyesno(
            "전체 안전 정리",
            "테스트 모드가 아닌 실제 전체 정리를 시작합니다.\n\n"
            "프로그램이 자동으로:\n"
            "• 계정 전체를 새로 스캔\n"
            "• PERMANENT / TYPE_CORE / 보호키워드 제외\n"
            "• 일반 무활동 + 노출제한 GENERAL 후보 선별\n"
            "• 30일/60일/90일 활동을 대량 재검증\n"
            "• 삭제 직전에 계정 상태와 노출제한 목록을 한 번 더 재검증\n"
            f"• 계정이 {policy.cleanup_stop:,}개에 도달하면 자동 중단\n\n"
            "계속하시겠습니까?",
        ):
            return

        if not messagebox.askyesno(
            "최종 실제 삭제 확인",
            "네이버 광고계정의 불필요 키워드를 실제로 삭제합니다.\n\n"
            "• 50개 단위 내부 체크포인트\n"
            "• 광고그룹 최대 50% 삭제 / 최소 4개 유지\n"
            "• 모든 DELETE 전에 복원 원본 저장\n"
            "• 각 체크포인트 삭제 후 실제 제거 여부 확인\n"
            "• 통계/API 확인이 불완전하면 해당 키워드는 삭제하지 않음\n"
            "• 삭제 오류 3건 또는 사후검증 실패 시 자동 안전중단\n\n"
            "전체 안전 정리를 실행할까요?",
        ):
            return

        self._auto_delete_started_at = 0.0
        self._auto_stage_started_at = time.monotonic()
        self._set_auto_stop_state("normal")
        self._auto_stats_stage = ""

        def success() -> None:
            self._set_auto_stop_state("disabled")
            self.refresh_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            messagebox.showinfo(
                "전체 안전 정리 완료",
                self._auto_summary_text()
                + "\n\n삭제된 키워드는 '삭제 기록 / 복원'에서 복원 원본을 확인할 수 있습니다.",
            )

        def failure(detail: str) -> None:
            self._set_auto_stop_state("disabled")
            self.refresh_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            summary = self._auto_summary_text()
            messagebox.showwarning(
                "전체 안전 정리 안전중단",
                summary
                + "\n\n일부 키워드는 이미 정상 삭제됐을 수 있습니다. "
                "삭제 기록 / 복원과 감사 로그를 확인해주세요.\n\n"
                + (detail[-1000:] if detail else ""),
            )

        self._start_stream_job(
            command=self.backend_command(
                "auto_cleanup.py",
                "--delete",
                "--confirm",
                "AUTO_CLEAN",
            ),
            label="전체 안전 정리",
            job_kind="auto_cleanup",
            on_success=success,
            on_failure=failure,
        )


def main() -> int:
    app = DesktopAppV9()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
