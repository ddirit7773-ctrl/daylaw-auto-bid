from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from tkinter import StringVar, ttk, messagebox

import customtkinter as ctk

from desktop_app_v2 import DesktopAppV2, ROOT_DIR
from src.keyword_cleaner.lifecycle_store import DEFAULT_DB_PATH
from src.keyword_cleaner.policy_v2 import CleanerPolicy


BACKUP_DIR = ROOT_DIR / "data" / "backups"


class DesktopAppV3(DesktopAppV2):
    """Desktop app with stats + searchable delete queue + archive restore UI."""

    def __init__(self) -> None:
        self.queue_rows: list[dict[str, str]] = []
        self.queue_filtered_rows: list[dict[str, str]] = []
        self.queue_sort_key = "age_days"
        self.queue_sort_reverse = True
        self.history_rows: list[dict[str, str]] = []
        super().__init__()
        self.title("DAYLAW Keyword Cleaner")

    def show_page(self, key: str) -> None:
        super().show_page(key)
        if key == "history":
            self.refresh_history()

    # ------------------------------------------------------------------
    # Delete queue
    # ------------------------------------------------------------------
    def _build_delete_queue_page(self) -> None:
        page = self._page(
            "delete_queue",
            "삭제 대기",
            "DELETE_PENDING / DELETE_APPROVED를 검색·필터·정렬하고 승인된 항목만 실행합니다.",
        )

        filters = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        filters.pack(fill="x", padx=28, pady=(6, 10))

        self.queue_search_var = StringVar(value="")
        self.queue_status_var = StringVar(value="전체")
        self.queue_tier_var = StringVar(value="전체")

        ctk.CTkLabel(filters, text="업체/키워드 검색").grid(row=0, column=0, sticky="w", padx=(16, 6), pady=14)
        entry = ctk.CTkEntry(
            filters,
            textvariable=self.queue_search_var,
            width=260,
            placeholder_text="업체명 또는 키워드",
        )
        entry.grid(row=0, column=1, sticky="w", padx=6, pady=14)
        entry.bind("<KeyRelease>", lambda _event: self.apply_queue_filter())

        ctk.CTkLabel(filters, text="상태").grid(row=0, column=2, sticky="w", padx=(16, 6), pady=14)
        ctk.CTkOptionMenu(
            filters,
            variable=self.queue_status_var,
            values=["전체", "DELETE_PENDING", "DELETE_APPROVED"],
            width=160,
            command=lambda _value: self.apply_queue_filter(),
        ).grid(row=0, column=3, sticky="w", padx=6, pady=14)

        ctk.CTkLabel(filters, text="구분").grid(row=0, column=4, sticky="w", padx=(16, 6), pady=14)
        ctk.CTkOptionMenu(
            filters,
            variable=self.queue_tier_var,
            values=["전체", "GENERAL", "TYPE_CORE", "PERMANENT"],
            width=130,
            command=lambda _value: self.apply_queue_filter(),
        ).grid(row=0, column=5, sticky="w", padx=6, pady=14)

        ctk.CTkButton(filters, text="새로고침", width=100, command=self.refresh_delete_queue).grid(
            row=0, column=6, sticky="w", padx=(14, 6), pady=14
        )

        action_bar = ctk.CTkFrame(page, fg_color="transparent")
        action_bar.pack(fill="x", padx=28, pady=(0, 8))
        self.delete_queue_summary = ctk.CTkLabel(
            action_bar,
            text="-",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.delete_queue_summary.pack(side="left")

        ctk.CTkButton(
            action_bar,
            text="삭제 실행기 DRY RUN",
            width=150,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.run_script("execute_v2_delete.py"),
        ).pack(side="right", padx=(8, 0))
        self.live_delete_button = ctk.CTkButton(
            action_bar,
            text="실제 삭제 (최초 배치)",
            width=160,
            fg_color="#B91C1C",
            hover_color="#991B1B",
            state="disabled",
            command=self.run_live_delete,
        )
        self.live_delete_button.pack(side="right")

        table_wrap = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        table_wrap.pack(fill="both", expand=True, padx=28, pady=(0, 28))

        columns = ("campaign", "adgroup", "keyword", "tier", "status", "age", "reason")
        self.queue_tree = ttk.Treeview(table_wrap, columns=columns, show="headings", height=22)
        headings = {
            "campaign": "캠페인",
            "adgroup": "업체명 / 광고그룹",
            "keyword": "키워드",
            "tier": "구분",
            "status": "상태",
            "age": "등록일수",
            "reason": "판정 사유",
        }
        widths = {
            "campaign": 105,
            "adgroup": 180,
            "keyword": 210,
            "tier": 95,
            "status": 130,
            "age": 80,
            "reason": 410,
        }
        sort_map = {
            "campaign": "campaign_name",
            "adgroup": "adgroup_name",
            "keyword": "keyword",
            "tier": "tier",
            "status": "status",
            "age": "age_days",
            "reason": "reason",
        }
        for column in columns:
            self.queue_tree.heading(
                column,
                text=headings[column],
                command=lambda key=sort_map[column]: self.sort_queue(key),
            )
            self.queue_tree.column(column, width=widths[column], anchor="w")

        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.queue_tree.yview)
        xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.queue_tree.xview)
        self.queue_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.queue_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        yscroll.pack(side="right", fill="y", pady=12, padx=(0, 6))
        xscroll.pack(side="bottom", fill="x", padx=12, pady=(0, 6))

    def refresh_delete_queue(self) -> None:
        path = self._latest("v2_scan_*.csv")
        rows = self._read_csv(path)
        self.queue_rows = [
            row for row in rows
            if row.get("status") in {"DELETE_PENDING", "DELETE_APPROVED"}
        ]
        self.apply_queue_filter()

    @staticmethod
    def _queue_number(row: dict[str, str], key: str) -> float:
        try:
            return float(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def apply_queue_filter(self) -> None:
        query = self.queue_search_var.get().strip().casefold()
        status = self.queue_status_var.get().strip()
        tier = self.queue_tier_var.get().strip()
        rows = list(self.queue_rows)
        if query:
            rows = [
                row for row in rows
                if query in row.get("adgroup_name", "").casefold()
                or query in row.get("keyword", "").casefold()
                or query in row.get("campaign_name", "").casefold()
            ]
        if status != "전체":
            rows = [row for row in rows if row.get("status") == status]
        if tier != "전체":
            rows = [row for row in rows if row.get("tier") == tier]
        self.queue_filtered_rows = rows
        self._sort_queue_rows()
        self.render_queue_rows()

    def sort_queue(self, key: str) -> None:
        if self.queue_sort_key == key:
            self.queue_sort_reverse = not self.queue_sort_reverse
        else:
            self.queue_sort_key = key
            self.queue_sort_reverse = key == "age_days"
        self._sort_queue_rows()
        self.render_queue_rows()

    def _sort_queue_rows(self) -> None:
        if self.queue_sort_key == "age_days":
            self.queue_filtered_rows.sort(
                key=lambda row: self._queue_number(row, "age_days"),
                reverse=self.queue_sort_reverse,
            )
        else:
            self.queue_filtered_rows.sort(
                key=lambda row: row.get(self.queue_sort_key, "").casefold(),
                reverse=self.queue_sort_reverse,
            )

    def render_queue_rows(self) -> None:
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for row in self.queue_filtered_rows[:5000]:
            self.queue_tree.insert(
                "",
                "end",
                values=(
                    row.get("campaign_name", ""),
                    row.get("adgroup_name", ""),
                    row.get("keyword", ""),
                    row.get("tier", ""),
                    row.get("status", ""),
                    row.get("age_days", ""),
                    row.get("reason", ""),
                ),
            )

        approved_all = sum(1 for row in self.queue_rows if row.get("status") == "DELETE_APPROVED")
        pending_all = len(self.queue_rows) - approved_all
        self.delete_queue_summary.configure(
            text=(
                f"전체 삭제대기 {pending_all:,}개 · 삭제승인 {approved_all:,}개 · "
                f"현재 표시 {len(self.queue_filtered_rows):,}개"
            )
        )
        self.live_delete_button.configure(state="normal" if approved_all > 0 else "disabled")

    def run_live_delete(self) -> None:
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
            [
                sys.executable,
                str(ROOT_DIR / "execute_v2_delete.py"),
                "--delete",
                "--confirm",
                "DELETE",
                "--max-delete",
                str(batch),
            ],
            "실제 삭제 실행기를 시작했습니다. 결과는 터미널/감사 로그에서 확인하세요.",
        )

    # ------------------------------------------------------------------
    # Delete history / restore
    # ------------------------------------------------------------------
    def _build_history_page(self) -> None:
        page = self._page(
            "history",
            "삭제 기록 / 복원",
            "실제 삭제된 키워드의 archive를 확인하고 선택한 항목을 안전하게 복원합니다.",
        )
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.pack(fill="x", padx=28, pady=(6, 8))
        self.history_summary = ctk.CTkLabel(top, text="삭제 기록 0개", font=ctk.CTkFont(size=14, weight="bold"))
        self.history_summary.pack(side="left")
        ctk.CTkButton(top, text="새로고침", width=100, command=self.refresh_history).pack(side="right")

        actions = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        actions.pack(fill="x", padx=28, pady=(0, 10))
        ctk.CTkLabel(
            actions,
            text="복원은 삭제 당시 저장한 원본 키워드 payload를 사용하며, 동일 키워드가 이미 있으면 안전하게 중단됩니다.",
            text_color="#64748B",
        ).pack(side="left", padx=16, pady=14)
        ctk.CTkButton(
            actions,
            text="선택 항목 복원 DRY RUN",
            width=180,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.restore_selected(False),
        ).pack(side="right", padx=(6, 16), pady=10)
        ctk.CTkButton(
            actions,
            text="선택 항목 실제 복원",
            width=160,
            fg_color="#0F766E",
            hover_color="#115E59",
            command=lambda: self.restore_selected(True),
        ).pack(side="right", padx=6, pady=10)

        wrap = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        wrap.pack(fill="both", expand=True, padx=28, pady=(0, 28))
        columns = ("id", "deleted", "campaign", "adgroup", "keyword", "state")
        self.history_tree = ttk.Treeview(wrap, columns=columns, show="headings", height=23)
        heads = {
            "id": "Archive ID",
            "deleted": "삭제일시",
            "campaign": "캠페인",
            "adgroup": "업체명 / 광고그룹",
            "keyword": "키워드",
            "state": "복원 상태",
        }
        widths = {"id": 80, "deleted": 180, "campaign": 110, "adgroup": 200, "keyword": 260, "state": 110}
        for col in columns:
            self.history_tree.heading(col, text=heads[col])
            self.history_tree.column(col, width=widths[col], anchor="w")
        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.history_tree.yview)
        xscroll = ttk.Scrollbar(wrap, orient="horizontal", command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.history_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        yscroll.pack(side="right", fill="y", padx=(0, 6), pady=12)
        xscroll.pack(side="bottom", fill="x", padx=12, pady=(0, 6))

    def refresh_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        self.history_rows = []
        if not DEFAULT_DB_PATH.exists():
            self.history_summary.configure(text="삭제 기록 0개")
            return

        conn = sqlite3.connect(DEFAULT_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM keyword_archive ORDER BY archive_id DESC LIMIT 5000"
            ).fetchall()
        finally:
            conn.close()

        for dbrow in rows:
            row = dict(dbrow)
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            plan = payload.get("plan") or {}
            item = {
                "archive_id": str(row.get("archive_id", "")),
                "deleted_at": str(row.get("deleted_at", "")),
                "campaign_name": str(plan.get("campaign_name") or row.get("campaign_id") or ""),
                "adgroup_name": str(plan.get("adgroup_name") or row.get("adgroup_id") or ""),
                "keyword": str(row.get("keyword") or ""),
                "restored_at": str(row.get("restored_at") or ""),
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
                    "복원 완료" if item["restored_at"] else "미복원",
                ),
            )

        restored = sum(1 for row in self.history_rows if row["restored_at"])
        self.history_summary.configure(
            text=f"삭제 기록 {len(self.history_rows):,}개 · 복원 완료 {restored:,}개"
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

        command = [
            sys.executable,
            str(ROOT_DIR / "restore_deleted_keyword.py"),
            "--archive-id",
            archive_id,
        ]
        if live:
            if not messagebox.askyesno(
                "실제 복원 확인",
                f"[{row['keyword']}] 키워드를 실제로 다시 생성하고 검증합니다.\n계속하시겠습니까?",
            ):
                return
            command.extend(["--restore", "--confirm", "RESTORE"])
        self._spawn_command(
            command,
            "복원 실행을 시작했습니다." if live else "복원 DRY RUN을 시작했습니다.",
        )

    def _spawn_command(self, command: list[str], notice: str) -> None:
        try:
            subprocess.Popen(command, cwd=str(ROOT_DIR))
        except Exception as exc:
            messagebox.showerror("실행 실패", str(exc))
            return
        messagebox.showinfo("실행 시작", notice)


def main() -> int:
    app = DesktopAppV3()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
