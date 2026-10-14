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
    """v16 production shell: 5k/10k/20k checkpointed cleanup presets."""

    def __init__(self) -> None:
        self._auto_stage_started_at = 0.0
        self._auto_delete_started_at = 0.0
        self._auto_stats_stage = ""
        self._auto_run_started_wall = 0.0
        super().__init__()
        self._retag_v16(self.sidebar)
        self._add_production_cleanup_buttons()
        if hasattr(self, "history_tree"):
            try:
                self.history_tree.heading("state", text="삭제 / 복원 상태")
            except Exception:
                pass

    def _retag_v16(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
                if isinstance(text, str):
                    text = text.replace("v14", "v16").replace("v15", "v16")
                    child.configure(text=text)
            except Exception:
                pass
            self._retag_v16(child)

    def _add_production_cleanup_buttons(self) -> None:
        # Dashboard: three production presets. The policy settings still define
        # the actual safety windows; these buttons control how many verified
        # candidates may be removed in one run and whether old PAUSED adgroups
        # may be considered.
        if hasattr(self, "status_text"):
            card = self.status_text.master
            preset = ctk.CTkFrame(card, fg_color="transparent")
            try:
                preset.pack(fill="x", padx=20, pady=5, before=self.status_text)
            except Exception:
                preset.pack(fill="x", padx=20, pady=5)

            for column in range(3):
                preset.grid_columnconfigure(column, weight=1)

            specs = [
                (
                    "안전 · 최대 5,000개",
                    "safe",
                    5000,
                    "#0F766E",
                    "#115E59",
                ),
                (
                    "공격적 · 최대 10,000개",
                    "aggressive",
                    10000,
                    "#D97706",
                    "#B45309",
                ),
                (
                    "매우 공격적 · 최대 20,000개",
                    "very_aggressive",
                    20000,
                    "#B91C1C",
                    "#991B1B",
                ),
            ]
            self.production_dashboard_buttons = []
            for column, (label, mode, cap, color, hover) in enumerate(specs):
                button = ctk.CTkButton(
                    preset,
                    text=label,
                    height=48,
                    corner_radius=10,
                    fg_color=color,
                    hover_color=hover,
                    font=ctk.CTkFont(size=12, weight="bold"),
                    command=lambda m=mode, n=cap, t=label: self.run_production_cleanup(
                        mode=m,
                        max_delete=n,
                        mode_label=t,
                    ),
                )
                button.grid(row=0, column=column, sticky="ew", padx=4)
                self.production_dashboard_buttons.append(button)

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

        if hasattr(self, "live_delete_button"):
            bar = self.live_delete_button.master
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

            self.production_queue_stop = ctk.CTkButton(
                bar,
                text="안전 중지",
                width=95,
                fg_color="#475569",
                hover_color="#334155",
                state="disabled",
                command=self._request_auto_stop,
            )
            self.production_queue_stop.pack(side="right", padx=(8, 0))

            queue_specs = [
                ("5천 안전", "safe", 5000, "#0F766E", "#115E59"),
                ("1만 공격", "aggressive", 10000, "#D97706", "#B45309"),
                ("2만 최대", "very_aggressive", 20000, "#B91C1C", "#991B1B"),
            ]
            self.production_queue_buttons = []
            for label, mode, cap, color, hover in queue_specs:
                button = ctk.CTkButton(
                    bar,
                    text=label,
                    width=92,
                    fg_color=color,
                    hover_color=hover,
                    font=ctk.CTkFont(size=11, weight="bold"),
                    command=lambda m=mode, n=cap, t=label: self.run_production_cleanup(
                        mode=m,
                        max_delete=n,
                        mode_label=t,
                    ),
                )
                button.pack(side="right", padx=(5, 0))
                self.production_queue_buttons.append(button)

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

    def refresh_dashboard(self) -> None:
        super().refresh_dashboard()

    @staticmethod
    def _latest_auto_summary(
        max_age_seconds: int = 24 * 3600,
        min_mtime: float = 0.0,
    ) -> dict[str, str] | None:
        folder = APP_ROOT / "data" / "delete_audit"
        files = sorted(folder.glob("auto_cleanup_summary_*.csv"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        latest = files[-1]
        if time.time() - latest.stat().st_mtime > max_age_seconds:
            return None
        if min_mtime and latest.stat().st_mtime < min_mtime:
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

    def _apply_cleanup_summary_to_dashboard(self) -> None:
        """Keep headline account totals current immediately after auto cleanup.

        Normally auto_cleanup publishes a filtered post-cleanup scan, so the base
        dashboard refresh is exact for all status cards. If that snapshot could
        not be published because another process changed the account
        concurrently, the verified live counter from the cleanup summary still
        updates the headline keyword/capacity figures instead of leaving an old
        pre-delete number on screen.
        """
        row = self._latest_auto_summary(min_mtime=self._auto_run_started_wall)
        if not row:
            return
        total = self._num(row.get("estimated_keywords_after"))
        if total <= 0:
            return
        try:
            policy = CleanerPolicy.load()
            self.metric_labels["keywords"].configure(text=f"{total:,}")
            ratio = min(total / max(policy.hard_limit_reference, 1), 1.0)
            self.capacity_bar.set(ratio)
            if total >= policy.cleanup_start:
                target_remove = max(total - policy.cleanup_stop, 0)
                self.capacity_label.configure(
                    text=f"{total:,} / {policy.hard_limit_reference:,}  ·  정리 목표 {target_remove:,}개"
                )
            else:
                self.capacity_label.configure(
                    text=f"{total:,} / {policy.hard_limit_reference:,}  ·  정리 불필요"
                )
        except Exception:
            pass

    def _auto_summary_text(self) -> str:
        row = self._latest_auto_summary(min_mtime=self._auto_run_started_wall)
        if not row:
            return "자동 정리 결과 요약 파일을 찾지 못했습니다."

        cleanup_stop = self._num(row.get("cleanup_stop"))
        stop_map = {
            "cleanup_stop_reached": f"{cleanup_stop:,}개 정리 목표에 도달",
            "cleanup_stop_reached_after_recount": f"최종 실시간 재확인 후 {cleanup_stop:,}개 목표에 도달",
            "completed_ready_subset": "이번 실행의 안전 통과 후보를 모두 처리",
            "delete_error_limit_reached": "삭제 API 오류가 3건 발생해 안전 중단",
            "post_delete_verification_failed": "삭제 후 확인 실패가 발견되어 안전 중단",
            "user_stop_requested": "사용자 안전 중지 요청으로 종료",
            "scan_data_quality_failed": "네이버 조회 데이터가 비정상적으로 불완전해 삭제 전 안전중단",
            "scan_failed": "전체 스캔 단계 오류로 삭제 전 안전중단",
            "no_structural_candidates": "현재 상태에서 구조 안전조건을 통과한 삭제 후보가 없음",
            "no_stats_safe_candidates": "설정된 무활동/클릭/전체이력 재검증을 통과한 후보가 없음",
            "no_final_ready_candidates": "삭제 직전 최종 재검증을 통과한 후보가 없음",
        }
        reason = row.get("stop_reason", "")
        initial_master = row.get("master_initial_status", "")
        final_master = row.get("master_final_status", "")
        if initial_master == "NOT_RUN":
            master_text = "노출제한 목록: 해당 단계 전에 작업 종료"
        elif initial_master == "SKIPPED":
            master_text = "노출제한 목록: Master 서버 오류 · 직접 API BLOCKED 안전판정 사용"
        elif final_master == "SKIPPED":
            master_text = "노출제한 목록: 최종 Master 확인 실패 · Master 전용 후보만 보류, BLOCKED 라인은 계속"
        else:
            master_text = "노출제한 목록: Master 정상 확인 + 직접 API BLOCKED 보조판정"

        diagnostic = str(row.get("exposure_diag_top") or "").strip()
        diagnostic_text = ""
        if diagnostic and reason == "no_structural_candidates":
            parts = [part.strip() for part in diagnostic.split(" || ") if part.strip()]
            diagnostic_text = "\n\n실제 비노출 API 상태 상위 조합:\n" + "\n".join(
                f"• {part}" for part in parts[:5]
            )

        mode_map = {
            "safe": "안전",
            "aggressive": "공격적",
            "very_aggressive": "매우 공격적",
        }
        mode_text = mode_map.get(str(row.get("cleanup_mode") or ""), str(row.get("cleanup_mode") or ""))
        cap = self._num(row.get("max_delete"))

        return (
            f"정리 모드 {mode_text or '-'} · 실행 상한 {cap:,}개\n"
            f"실제 삭제 {self._num(row.get('deleted')):,}개\n"
            f"삭제 후 사라짐 확인 {self._num(row.get('verified')):,}개\n"
            f"삭제 오류 {self._num(row.get('delete_errors')):,}개 · "
            f"사후검증 오류 {self._num(row.get('verify_failures')):,}개\n"
            f"정리 후 예상 키워드 {self._num(row.get('estimated_keywords_after')):,}개\n"
            f"{master_text}\n"
            f"종료 사유: {stop_map.get(reason, reason or '완료')}"
            f"{diagnostic_text}"
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
                try:
                    floor = CleanerPolicy.load().cleanup_stop
                    title = f"{floor:,}개 하한선 최종 재확인"
                except Exception:
                    title = "정리 하한선 최종 재확인"
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

        auto_master = re.match(r"@@AUTO_MASTER_STATUS\|([^|]+)\|([^|]+)\|(\d+)", text)
        if auto_master:
            phase, status, value = auto_master.groups()
            phase_ko = "1차" if phase == "INITIAL" else "최종"
            if status == "OK":
                self.progress_detail.configure(
                    text=f"{phase_ko} 노출제한 목록 확인 완료 · {int(value):,}개"
                )
            elif status == "RETRY":
                self.progress_detail.configure(
                    text=f"{phase_ko} 노출제한 목록 서버 오류 · 자동 재시도 {value}회차"
                )
            elif status == "SKIP":
                self.progress_detail.configure(
                    text=(
                        f"{phase_ko} Keyword Master 서버 오류 · Master 전용 후보는 보류하지만 "
                        "직접 API에서 확인되는 오래된 BLOCKED 키워드는 계속 안전검증합니다."
                    )
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

        exposure_blocked = re.match(
            r"@@SCAN_EXPOSURE_BLOCKED\|(\d+)\|(\d+)",
            text,
        )
        if exposure_blocked:
            blocked, total = map(int, exposure_blocked.groups())
            self.progress_detail.configure(
                text=(
                    f"현재 노출불가/노출제한 상태 {blocked:,}개 확인 · 오류가 아니며 "
                    "다음 단계에서 노출제한 목록과 매칭해 삭제 가능 여부를 계속 판단합니다."
                )
            )
            return

        quality_fail = re.match(
            r"@@SCAN_QUALITY_FAIL\|(\d+)\|(\d+)\|(\d+)",
            text,
        )
        if quality_fail:
            insufficient, total, actionable = map(int, quality_fail.groups())
            self._set_progress(
                31,
                f"{label} · 스캔 데이터 이상 감지",
                (
                    f"전체 {total:,}개 중 데이터 부족 {insufficient:,}개 · "
                    f"판정가능 {actionable:,}개뿐이라 실제 삭제 전 안전중단합니다."
                ),
            )
            return

        stats_retry = re.match(
            r"@@STATS_RETRY\|ids=(\d+)\|batch_size=(\d+)\|batches=(\d+)",
            text,
        )
        if stats_retry:
            ids, batch_size, batches = map(int, stats_retry.groups())
            self.progress_detail.configure(
                text=f"네이버 통계 실패 {ids:,}개를 {batch_size}개씩 {batches:,}배치로 자동 재조회합니다."
            )
            return

        if text.startswith("[MASTER SAFE HOLD]") or text.startswith("[FALLBACK]"):
            self.progress_detail.configure(
                text=(
                    "네이버 Keyword Master 일시 오류 · 직접 API의 BLOCKED 상태를 보조판정해 "
                    "설정된 전체 활동이력 기준으로 안전정리를 계속합니다."
                )
            )
            return

        exposure_diag = re.match(
            r"@@AUTO_EXPOSURE_DIAG\|(\d+)\|(.*)",
            text,
        )
        if exposure_diag:
            count, shape = exposure_diag.groups()
            self.progress_detail.configure(
                text=f"비노출 API 상태 진단 · {int(count):,}개 · {shape}"
            )
            return

        blocked_fallback = re.match(
            r"@@AUTO_BLOCKED_FALLBACK\|(\d+)\|(\d+)\|([^|]+)",
            text,
        )
        if blocked_fallback:
            blocked, master_ids, master_state = blocked_fallback.groups()
            self.progress_detail.configure(
                text=(
                    f"직접 API BLOCKED 후보 {int(blocked):,}개 · "
                    f"Master 확인 후보 {int(master_ids):,}개 · "
                    f"{'Master 정상' if master_state == 'MASTER_OK' else 'Master 서버 오류 fallback'}"
                )
            )
            return

        paused_group = re.match(r"@@AUTO_PAUSED_GROUP\|(\d+)\|([^|]+)", text)
        if paused_group:
            count, mode = paused_group.groups()
            if mode == "safe":
                self.progress_detail.configure(
                    text="안전 모드 · 중지 광고그룹은 삭제 후보에서 제외합니다."
                )
            else:
                self.progress_detail.configure(
                    text=(
                        f"중지 광고그룹의 오래된 GENERAL 후보 {int(count):,}개 확인 · "
                        "활동이력 0 및 잠금/보호조건을 다시 검증합니다."
                    )
                )
            return

        phase = re.match(r"@@AUTO_PHASE\|([^|]+)\|([^|]+)\|(\d+)", text)
        if phase:
            round_name, window, count_s = phase.groups()
            count = int(count_s)
            self._auto_stats_stage = f"{round_name}:{window}"
            try:
                policy = CleanerPolicy.load()
                windows = [
                    f"{policy.general_inactivity_days}d",
                    f"{policy.click_protection_days}d",
                    f"{policy.reference_history_days}d",
                ]
                index = windows.index(window) if window in windows else 0
            except Exception:
                index = 0
            if round_name == "PRECHECK":
                base = (50, 54, 58)[min(index, 2)]
            else:
                base = (68, 70, 72)[min(index, 2)]
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
            try:
                policy = CleanerPolicy.load()
                pre = {
                    f"PRECHECK:{policy.general_inactivity_days}d": (50, 53.5),
                    f"PRECHECK:{policy.click_protection_days}d": (54, 57.5),
                    f"PRECHECK:{policy.reference_history_days}d": (58, 62),
                    f"FINAL:{policy.general_inactivity_days}d": (68, 69.5),
                    f"FINAL:{policy.click_protection_days}d": (70, 71.5),
                    f"FINAL:{policy.reference_history_days}d": (72, 74),
                }
                start, end = pre.get(self._auto_stats_stage, (50, 74))
            except Exception:
                start, end = (50, 74)
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

        dashboard_refresh = re.match(
            r"@@AUTO_DASHBOARD_REFRESH\|([^|]+)\|(\d+)\|(\d+)",
            text,
        )
        if dashboard_refresh:
            status, shown, expected = dashboard_refresh.groups()
            if status == "OK":
                self._set_progress(
                    99,
                    f"{label} · 화면 최신화",
                    f"삭제 후 {int(shown):,}개 기준으로 대시보드/삭제대기 목록을 갱신합니다.",
                )
            else:
                self._set_progress(
                    99,
                    f"{label} · 화면 최신화",
                    f"계정 동시 변경 감지 · 요약의 실시간 카운트 {int(expected):,}개를 표시합니다.",
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

    def run_production_cleanup(
        self,
        *,
        mode: str = "safe",
        max_delete: int = 5000,
        mode_label: str = "안전 · 최대 5,000개",
    ) -> None:
        if self.backend_running:
            messagebox.showinfo("작업 진행 중", "현재 작업이 끝난 뒤 다시 실행해주세요.")
            return

        policy = CleanerPolicy.load()
        paused_group_text = (
            "제외"
            if mode == "safe"
            else "포함 가능 · 단, 사용자 잠금이 아니고 오래된 GENERAL + 활동 0인 경우만"
        )
        theoretical = max(
            min(max_delete, max(self._dashboard_keyword_count() - policy.cleanup_stop, 0)),
            0,
        )

        if not messagebox.askyesno(
            f"{mode_label} 실행",
            f"{mode_label} 모드로 실제 전체 정리를 시작합니다.\n\n"
            f"이번 실행 최대 삭제량: {max_delete:,}개\n"
            f"현재 설정상 정리 중단 기준: {policy.cleanup_stop:,}개\n"
            f"현재 화면 기준 최대 가능량: 약 {theoretical:,}개\n\n"
            "공통 보호:\n"
            "• PERMANENT / TYPE_CORE / 수동 보호키워드 제외\n"
            f"• 일반 무활동 {policy.general_inactivity_days}일 재검증\n"
            f"• 클릭 보호 {policy.click_protection_days}일 재검증\n"
            f"• 전체 활동이력 {policy.reference_history_days}일 재검증\n"
            "• 통계가 불완전하면 삭제 안 함\n"
            f"• 중지 광고그룹 키워드: {paused_group_text}\n\n"
            "계속하시겠습니까?",
        ):
            return

        if not messagebox.askyesno(
            "최종 실제 삭제 확인",
            "네이버 광고계정의 키워드를 실제로 삭제합니다.\n\n"
            "• 50개 단위 내부 체크포인트\n"
            "• 광고그룹 최대 50% 삭제 / 최소 4개 유지\n"
            "• 모든 DELETE 전에 복원 원본 저장\n"
            "• 각 체크포인트 삭제 후 실제 제거 여부 확인\n"
            "• 삭제 오류 3건 또는 사후검증 실패 시 자동 안전중단\n"
            f"• 이번 실행은 최대 {max_delete:,}개까지만 삭제\n\n"
            "실행할까요?",
        ):
            return

        self._auto_delete_started_at = 0.0
        self._auto_stage_started_at = time.monotonic()
        self._auto_run_started_wall = time.time()
        self._set_auto_stop_state("normal")
        self._auto_stats_stage = ""

        job_label = f"{mode_label} 정리"

        def success() -> None:
            self._set_auto_stop_state("disabled")
            self.refresh_dashboard()
            self._apply_cleanup_summary_to_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            messagebox.showinfo(
                f"{mode_label} 완료",
                self._auto_summary_text()
                + "\n\n삭제된 키워드는 '삭제 기록 / 복원'에서 복원 원본을 확인할 수 있습니다.",
            )

        def failure(detail: str) -> None:
            self._set_auto_stop_state("disabled")
            self.refresh_dashboard()
            self._apply_cleanup_summary_to_dashboard()
            self.refresh_delete_queue()
            if hasattr(self, "refresh_history"):
                self.refresh_history()
            row = self._latest_auto_summary(min_mtime=self._auto_run_started_wall)
            if row:
                summary = self._auto_summary_text()
                deleted = self._num(row.get("deleted"))
                extra = (
                    "\n\n이번 실행에서 실제 삭제된 항목이 있으므로 삭제 기록 / 복원을 확인해주세요."
                    if deleted > 0
                    else "\n\n이번 실행에서는 실제 삭제가 기록되지 않았습니다."
                )
            else:
                summary = "이번 실행의 최종 요약이 생성되기 전에 안전중단되었습니다."
                extra = "\n\n삭제 기록 / 복원에서 실제 삭제 여부를 확인해주세요."
            messagebox.showwarning(
                f"{mode_label} 안전중단",
                summary + extra + "\n\n" + (detail[-1000:] if detail else ""),
            )

        self._start_stream_job(
            command=self.backend_command(
                "auto_cleanup.py",
                "--delete",
                "--confirm",
                "AUTO_CLEAN",
                "--mode",
                mode,
                "--max-delete",
                str(max_delete),
            ),
            label=job_label,
            job_kind="auto_cleanup",
            on_success=success,
            on_failure=failure,
        )

    def _dashboard_keyword_count(self) -> int:
        try:
            text = str(self.metric_labels["keywords"].cget("text") or "")
            return self._num(text.replace(",", ""))
        except Exception:
            return 0


def main() -> int:
    app = DesktopAppV9()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
