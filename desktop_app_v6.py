from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from desktop_app_v4 import APP_ROOT
from desktop_app_v5 import DesktopAppV5, PRIMARY, PRIMARY_HOVER, TEXT, MUTED, BORDER


class DesktopAppV6(DesktopAppV5):
    """v11 shell: streamed progress + safe-fast bulk read mode."""

    def __init__(self) -> None:
        self._job_started_at = 0.0
        self._job_phase = ""
        self._job_percent = 0.0
        self._fast_workers = 1
        self._fast_batch_size = 100
        super().__init__()
        self._retag_version(self.sidebar)
        self._build_progress_overlay()

    def _retag_version(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
                if isinstance(text, str):
                    if "v9" in text:
                        text = text.replace("v9", "v11")
                    if "v10" in text:
                        text = text.replace("v10", "v11")
                    child.configure(text=text)
            except Exception:
                pass
            self._retag_version(child)

    def _build_progress_overlay(self) -> None:
        self.progress_panel = ctk.CTkFrame(
            self.content,
            fg_color="#FFFFFF",
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        top = ctk.CTkFrame(self.progress_panel, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(11, 5))
        self.progress_title = ctk.CTkLabel(
            top,
            text="작업 준비 중",
            text_color=TEXT,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.progress_title.pack(side="left")
        self.progress_meta = ctk.CTkLabel(
            top,
            text="0%",
            text_color=MUTED,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.progress_meta.pack(side="right")
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_panel,
            height=9,
            corner_radius=5,
            progress_color=PRIMARY,
            fg_color="#E8EEF7",
        )
        self.progress_bar.pack(fill="x", padx=16, pady=(0, 7))
        self.progress_bar.set(0)
        self.progress_detail = ctk.CTkLabel(
            self.progress_panel,
            text="",
            text_color=MUTED,
            font=ctk.CTkFont(size=10),
            anchor="w",
        )
        self.progress_detail.pack(fill="x", padx=16, pady=(0, 10))
        self.progress_panel.place_forget()

    @staticmethod
    def _fmt_seconds(value: float) -> str:
        seconds = max(int(value), 0)
        minutes, seconds = divmod(seconds, 60)
        if minutes:
            return f"{minutes}분 {seconds:02d}초"
        return f"{seconds}초"

    def _show_progress(self, label: str) -> None:
        self._job_started_at = time.monotonic()
        self._job_percent = 2.0
        self._job_phase = "start"
        self._fast_workers = 1
        self._fast_batch_size = 100
        self.progress_title.configure(text=f"{label} · 시작 중")
        self.progress_meta.configure(text="2%")
        self.progress_detail.configure(text="네이버 광고 데이터를 준비하고 있습니다.")
        self.progress_bar.set(0.02)
        self.progress_panel.place(relx=0.5, rely=0.985, anchor="s", relwidth=0.92)
        self.progress_panel.lift()

    def _set_progress(self, percent: float, title: str, detail: str = "") -> None:
        percent = max(self._job_percent, min(float(percent), 100.0))
        self._job_percent = percent
        elapsed = max(time.monotonic() - self._job_started_at, 0.0)
        if 4 <= percent < 100:
            remaining = elapsed * (100.0 - percent) / percent
            meta = f"{percent:.0f}% · 약 {self._fmt_seconds(remaining)} 남음"
        elif percent >= 100:
            meta = f"100% · {self._fmt_seconds(elapsed)}"
        else:
            meta = f"{percent:.0f}% · 계산 중"
        self.progress_title.configure(text=title)
        self.progress_meta.configure(text=meta)
        self.progress_detail.configure(text=detail)
        self.progress_bar.set(percent / 100.0)
        self.progress_panel.lift()

    def _consume_progress_line(self, line: str, label: str, job_kind: str) -> None:
        text = line.strip()
        if not text:
            return

        fast_match = re.match(
            r"@@FAST_STATS\|workers=(\d+)\|batch_size=(\d+)\|batches=(\d+)",
            text,
        )
        if fast_match:
            self._fast_workers = int(fast_match.group(1))
            self._fast_batch_size = int(fast_match.group(2))
            batches = int(fast_match.group(3))
            self.progress_detail.configure(
                text=(
                    f"안전 고속 조회 · {self._fast_workers}개 동시 · "
                    f"{self._fast_batch_size}개/배치 · 총 {batches:,}배치"
                )
            )
            return

        if job_kind == "scan":
            if "[1/6]" in text:
                self._job_phase = "scan_structure"
                self._set_progress(5, f"{label} · 계정 구조 조회", "캠페인과 광고그룹/키워드를 불러오는 중입니다.")
                return
            if "[2/6]" in text:
                self._job_phase = "general_stats"
                self._set_progress(16, f"{label} · 일반 키워드 통계", text)
                return
            if "[3/6]" in text:
                self._job_phase = "type_stats"
                self._set_progress(44, f"{label} · 유형 핵심 통계", text)
                return
            if "[4/6]" in text:
                self._job_phase = "click_stats"
                self._set_progress(58, f"{label} · 클릭 보호 이력", text)
                return
            if "[5/6]" in text:
                self._job_phase = "classify"
                self._set_progress(84, f"{label} · 판정 적용", "보호/유지/관찰/삭제대기 상태를 계산 중입니다.")
                return
            if "[6/6]" in text or "SAFE STOP" in text:
                self._job_phase = "complete"
                self._set_progress(100, f"{label} · 완료", "대시보드를 갱신하고 있습니다.")
                return

            classified = re.search(r"classified\s+([\d,]+)/([\d,]+)", text, re.IGNORECASE)
            if classified:
                current = int(classified.group(1).replace(",", ""))
                total = max(int(classified.group(2).replace(",", "")), 1)
                pct = 84 + (current / total) * 14
                self._set_progress(pct, f"{label} · 판정 적용", f"{current:,} / {total:,}개 분류 완료")
                return

        if job_kind == "stats":
            if "Fetching keyword stats" in text:
                self._job_phase = "query_stats"
                self._set_progress(20, f"{label} · 키워드 통계 조회", "기간별 노출·클릭 데이터를 불러오는 중입니다.")
                return
            if text == "RESULT":
                self._set_progress(95, f"{label} · 결과 정리", "조회 결과 파일과 화면 데이터를 정리하고 있습니다.")
                return
            if "Read-only query complete" in text:
                self._set_progress(100, f"{label} · 완료", "통계 조회가 완료되었습니다.")
                return

        stat_match = re.match(r"@@STATS_PROGRESS\|(\d+)\|(\d+)", text)
        if stat_match:
            done = int(stat_match.group(1))
            total = max(int(stat_match.group(2)), 1)
            ratio = done / total
            ranges = {
                "general_stats": (16, 43),
                "type_stats": (44, 57),
                "click_stats": (58, 82),
                "query_stats": (20, 92),
            }
            start, end = ranges.get(self._job_phase, (10, 90))
            pct = start + ratio * (end - start)
            speed = (
                f" · {self._fast_workers}개 동시"
                if self._fast_workers > 1
                else ""
            )
            self._set_progress(
                pct,
                self.progress_title.cget("text"),
                f"통계 요청 {done:,} / {total:,} 배치 완료{speed}",
            )
            return

        if "running total keywords" in text or "running keywords" in text:
            self.progress_detail.configure(text=text)

    def _start_stream_job(
        self,
        *,
        command: list[str],
        label: str,
        job_kind: str,
        success_message: str = "",
        on_success=None,
        on_failure=None,
    ) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "이미 작업이 실행 중입니다. 완료될 때까지 기다려주세요.")
            return

        self.backend_running = True
        self._show_progress(label)

        def worker() -> None:
            lines: list[str] = []
            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                process = subprocess.Popen(
                    command,
                    cwd=str(APP_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
                assert process.stdout is not None
                for raw in process.stdout:
                    line = raw.rstrip("\r\n")
                    lines.append(line)
                    if len(lines) > 800:
                        lines = lines[-800:]
                    self.after(0, lambda value=line: self._consume_progress_line(value, label, job_kind))
                returncode = process.wait()
                output = "\n".join(lines)
                self.after(
                    0,
                    lambda: self._finish_stream_job(
                        returncode=returncode,
                        output=output,
                        label=label,
                        job_kind=job_kind,
                        success_message=success_message,
                        on_success=on_success,
                        on_failure=on_failure,
                    ),
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda: self._finish_stream_job(
                        returncode=1,
                        output=str(exc),
                        label=label,
                        job_kind=job_kind,
                        success_message=success_message,
                        on_success=on_success,
                        on_failure=on_failure,
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_stream_job(
        self,
        *,
        returncode: int,
        output: str,
        label: str,
        job_kind: str,
        success_message: str,
        on_success,
        on_failure,
    ) -> None:
        self.backend_running = False
        if returncode != 0:
            self._set_progress(max(self._job_percent, 5), f"{label} · 실패", "오류 내용을 확인해주세요.")
            detail = (output or "알 수 없는 오류")[-3500:]
            if callable(on_failure):
                on_failure(detail)
            else:
                messagebox.showerror(f"{label} 실패", detail)
            return

        self._set_progress(100, f"{label} · 완료", "화면을 최신 결과로 갱신했습니다.")
        self.refresh_dashboard()
        if hasattr(self, "refresh_delete_queue"):
            self.refresh_delete_queue()
        if callable(on_success):
            on_success()
        elif success_message:
            messagebox.showinfo("완료", success_message)
        self.after(1800, self.progress_panel.place_forget)

    def run_script(self, filename: str) -> None:
        labels = {
            "run_v2_scan.py": ("V2 전체 스캔", "scan", "V2 전체 스캔이 완료되었습니다."),
            "build_v2_delete_plan.py": ("안전 삭제 계획 생성", "generic", "안전 삭제 계획 생성이 완료되었습니다."),
            "execute_v2_delete.py": ("삭제 실행기 DRY RUN", "generic", "DRY RUN 검증이 완료되었습니다."),
        }
        label, kind, message = labels.get(filename, (filename, "generic", "작업이 완료되었습니다."))
        self._start_stream_job(
            command=self.backend_command(filename),
            label=label,
            job_kind=kind,
            success_message=message,
        )

    def run_stats_query(self) -> None:
        if getattr(self, "stats_running", False) or self.backend_running:
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
        self.stats_status_label.configure(text="실시간 진행률은 화면 아래 작업 진행 바에서 확인할 수 있습니다.")

        def success() -> None:
            self.stats_running = False
            self.finish_stats_query(True, "조회 완료")

        def failure(detail: str) -> None:
            self.stats_running = False
            self.finish_stats_query(False, detail)

        self._start_stream_job(
            command=command,
            label="통계 조회",
            job_kind="stats",
            on_success=success,
            on_failure=failure,
        )
