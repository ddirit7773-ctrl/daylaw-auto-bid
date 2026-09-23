from __future__ import annotations

import csv
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import StringVar, ttk, messagebox
from zoneinfo import ZoneInfo

import customtkinter as ctk

from desktop_app import DesktopApp


ROOT_DIR = Path(__file__).resolve().parent
STATS_DIR = ROOT_DIR / "data" / "stats"
KST = ZoneInfo("Asia/Seoul")


class DesktopAppV2(DesktopApp):
    """Desktop app with the real statistics screen wired in.

    All statistics actions are read-only. The page launches query_account_stats.py,
    then reads the generated ad-group CSV and renders it with search/sort controls.
    """

    def __init__(self) -> None:
        self.stats_rows: list[dict[str, str]] = []
        self.stats_filtered_rows: list[dict[str, str]] = []
        self.stats_sort_key = "clicks"
        self.stats_sort_reverse = True
        self.stats_running = False
        super().__init__()

    def show_page(self, key: str) -> None:
        super().show_page(key)
        if key == "stats":
            self.refresh_stats_from_latest()

    # ------------------------------------------------------------------
    # Statistics screen
    # ------------------------------------------------------------------
    def _build_stats_page(self) -> None:
        page = self._page(
            "stats",
            "통계 조회",
            "원하는 기간의 광고그룹별 노출수·클릭수·CTR을 조회하고 업체명으로 검색/정렬합니다.",
        )

        today = datetime.now(KST).date()
        until = today - timedelta(days=1)
        since = until - timedelta(days=6)

        filter_card = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        filter_card.pack(fill="x", padx=28, pady=(6, 10))

        self.stats_since_var = StringVar(value=since.isoformat())
        self.stats_until_var = StringVar(value=until.isoformat())
        self.stats_campaign_var = StringVar(value="")
        self.stats_search_var = StringVar(value="")

        ctk.CTkLabel(filter_card, text="시작일").grid(row=0, column=0, sticky="w", padx=(18, 6), pady=(16, 6))
        ctk.CTkEntry(filter_card, textvariable=self.stats_since_var, width=130).grid(
            row=0, column=1, sticky="w", padx=6, pady=(16, 6)
        )
        ctk.CTkLabel(filter_card, text="종료일").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=(16, 6))
        ctk.CTkEntry(filter_card, textvariable=self.stats_until_var, width=130).grid(
            row=0, column=3, sticky="w", padx=6, pady=(16, 6)
        )
        ctk.CTkLabel(filter_card, text="캠페인").grid(row=0, column=4, sticky="w", padx=(12, 6), pady=(16, 6))
        ctk.CTkEntry(
            filter_card,
            textvariable=self.stats_campaign_var,
            width=160,
            placeholder_text="전체",
        ).grid(row=0, column=5, sticky="w", padx=6, pady=(16, 6))

        self.stats_query_button = ctk.CTkButton(
            filter_card,
            text="조회 실행",
            width=110,
            command=self.run_stats_query,
        )
        self.stats_query_button.grid(row=0, column=6, sticky="w", padx=(12, 6), pady=(16, 6))

        ctk.CTkButton(
            filter_card,
            text="최근 7일",
            width=90,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.set_quick_range(7),
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=18, pady=(4, 14))
        ctk.CTkButton(
            filter_card,
            text="최근 30일",
            width=100,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.set_quick_range(30),
        ).grid(row=1, column=2, columnspan=2, sticky="w", padx=12, pady=(4, 14))

        self.stats_status_label = ctk.CTkLabel(
            filter_card,
            text="날짜는 YYYY-MM-DD 형식 · 통계 조회는 읽기 전용",
            text_color="#64748B",
        )
        self.stats_status_label.grid(row=1, column=4, columnspan=3, sticky="w", padx=12, pady=(4, 14))

        metric_wrap = ctk.CTkFrame(page, fg_color="transparent")
        metric_wrap.pack(fill="x", padx=28, pady=(0, 10))
        for col in range(6):
            metric_wrap.grid_columnconfigure(col, weight=1)

        self.stats_metric_labels: dict[str, ctk.CTkLabel] = {}
        metrics = [
            ("campaigns", "캠페인"),
            ("adgroups", "광고그룹"),
            ("keywords", "키워드"),
            ("impressions", "노출수"),
            ("clicks", "클릭수"),
            ("ctr", "CTR"),
        ]
        for idx, (key, label) in enumerate(metrics):
            card = ctk.CTkFrame(metric_wrap, fg_color="white", corner_radius=12)
            card.grid(row=0, column=idx, sticky="nsew", padx=4)
            ctk.CTkLabel(card, text=label, text_color="#64748B").pack(anchor="w", padx=14, pady=(12, 2))
            value = ctk.CTkLabel(card, text="-", font=ctk.CTkFont(size=22, weight="bold"))
            value.pack(anchor="w", padx=14, pady=(0, 12))
            self.stats_metric_labels[key] = value

        table_card = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        table_card.pack(fill="both", expand=True, padx=28, pady=(0, 28))

        search_bar = ctk.CTkFrame(table_card, fg_color="transparent")
        search_bar.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(search_bar, text="업체명 검색").pack(side="left", padx=(0, 8))
        search_entry = ctk.CTkEntry(
            search_bar,
            textvariable=self.stats_search_var,
            width=280,
            placeholder_text="광고그룹 이름 입력",
        )
        search_entry.pack(side="left")
        search_entry.bind("<KeyRelease>", lambda _event: self.apply_stats_filter())
        ctk.CTkButton(
            search_bar,
            text="검색 초기화",
            width=100,
            fg_color="#64748B",
            hover_color="#475569",
            command=self.reset_stats_search,
        ).pack(side="left", padx=8)
        self.stats_row_count_label = ctk.CTkLabel(search_bar, text="0개 업체", text_color="#64748B")
        self.stats_row_count_label.pack(side="right")

        columns = (
            "campaign",
            "adgroup",
            "keywords",
            "impressions",
            "clicks",
            "ctr",
        )
        self.stats_tree = ttk.Treeview(table_card, columns=columns, show="headings", height=20)
        headings = {
            "campaign": "캠페인",
            "adgroup": "업체명 / 광고그룹",
            "keywords": "키워드 수",
            "impressions": "노출수",
            "clicks": "클릭수",
            "ctr": "CTR",
        }
        widths = {
            "campaign": 120,
            "adgroup": 260,
            "keywords": 100,
            "impressions": 120,
            "clicks": 100,
            "ctr": 100,
        }
        sort_map = {
            "campaign": "campaign_name",
            "adgroup": "adgroup_name",
            "keywords": "keyword_count",
            "impressions": "impressions",
            "clicks": "clicks",
            "ctr": "ctr_pct",
        }
        for column in columns:
            key = sort_map[column]
            self.stats_tree.heading(
                column,
                text=headings[column],
                command=lambda k=key: self.sort_stats(k),
            )
            anchor = "e" if column in {"keywords", "impressions", "clicks", "ctr"} else "w"
            self.stats_tree.column(column, width=widths[column], anchor=anchor)

        yscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.stats_tree.yview)
        xscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.stats_tree.xview)
        self.stats_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.stats_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
        yscroll.pack(side="right", fill="y", padx=(0, 6), pady=(0, 12))
        xscroll.pack(side="bottom", fill="x", padx=12, pady=(0, 6))

    def set_quick_range(self, days: int) -> None:
        today = datetime.now(KST).date()
        until = today - timedelta(days=1)
        since = until - timedelta(days=days - 1)
        self.stats_since_var.set(since.isoformat())
        self.stats_until_var.set(until.isoformat())

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

        command = [
            sys.executable,
            str(ROOT_DIR / "query_account_stats.py"),
            "--since",
            since.isoformat(),
            "--until",
            until.isoformat(),
        ]
        campaign = self.stats_campaign_var.get().strip()
        if campaign:
            command.extend(["--campaign", campaign])

        self.stats_running = True
        self.stats_query_button.configure(state="disabled", text="조회 중...")
        self.stats_status_label.configure(text="네이버 광고 통계를 조회하고 있습니다. 잠시 기다려주세요.")

        def worker() -> None:
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT_DIR),
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

    def finish_stats_query(self, success: bool, message: str) -> None:
        self.stats_running = False
        self.stats_query_button.configure(state="normal", text="조회 실행")
        if not success:
            self.stats_status_label.configure(text="조회 실패")
            messagebox.showerror("통계 조회 실패", message)
            return
        self.stats_status_label.configure(text="조회 완료 · 최신 결과를 표시합니다.")
        self.refresh_stats_from_latest()

    def _latest_stats_file(self) -> Path | None:
        files = list(STATS_DIR.glob("adgroup_stats_*.csv"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None

    def refresh_stats_from_latest(self) -> None:
        path = self._latest_stats_file()
        if path is None:
            return
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fp:
                self.stats_rows = list(csv.DictReader(fp))
        except Exception as exc:
            self.stats_status_label.configure(text=f"CSV 읽기 실패: {exc}")
            return

        if self.stats_rows:
            self.stats_since_var.set(self.stats_rows[0].get("since", self.stats_since_var.get()))
            self.stats_until_var.set(self.stats_rows[0].get("until", self.stats_until_var.get()))

        self.apply_stats_filter()

    @staticmethod
    def _number(row: dict[str, str], key: str) -> float:
        try:
            return float(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def apply_stats_filter(self) -> None:
        query = self.stats_search_var.get().strip().casefold()
        if query:
            rows = [
                row for row in self.stats_rows
                if query in row.get("adgroup_name", "").casefold()
                or query in row.get("campaign_name", "").casefold()
            ]
        else:
            rows = list(self.stats_rows)
        self.stats_filtered_rows = rows
        self._sort_stats_rows()
        self.render_stats_rows()
        self.update_stats_metrics()

    def reset_stats_search(self) -> None:
        self.stats_search_var.set("")
        self.apply_stats_filter()

    def sort_stats(self, key: str) -> None:
        if self.stats_sort_key == key:
            self.stats_sort_reverse = not self.stats_sort_reverse
        else:
            self.stats_sort_key = key
            self.stats_sort_reverse = key in {"keyword_count", "impressions", "clicks", "ctr_pct"}
        self._sort_stats_rows()
        self.render_stats_rows()

    def _sort_stats_rows(self) -> None:
        numeric = self.stats_sort_key in {"keyword_count", "impressions", "clicks", "ctr_pct"}
        if numeric:
            self.stats_filtered_rows.sort(
                key=lambda row: self._number(row, self.stats_sort_key),
                reverse=self.stats_sort_reverse,
            )
        else:
            self.stats_filtered_rows.sort(
                key=lambda row: row.get(self.stats_sort_key, "").casefold(),
                reverse=self.stats_sort_reverse,
            )

    def render_stats_rows(self) -> None:
        for item in self.stats_tree.get_children():
            self.stats_tree.delete(item)
        for row in self.stats_filtered_rows:
            imp = int(self._number(row, "impressions"))
            clk = int(self._number(row, "clicks"))
            kws = int(self._number(row, "keyword_count"))
            ctr = self._number(row, "ctr_pct")
            self.stats_tree.insert(
                "",
                "end",
                values=(
                    row.get("campaign_name", ""),
                    row.get("adgroup_name", ""),
                    f"{kws:,}",
                    f"{imp:,}",
                    f"{clk:,}",
                    f"{ctr:.2f}%",
                ),
            )
        self.stats_row_count_label.configure(text=f"{len(self.stats_filtered_rows):,}개 업체")

    def update_stats_metrics(self) -> None:
        rows = self.stats_filtered_rows
        campaigns = len({r.get("campaign_id", "") for r in rows if r.get("campaign_id")})
        adgroups = len({r.get("adgroup_id", "") for r in rows if r.get("adgroup_id")})
        keywords = sum(int(self._number(r, "keyword_count")) for r in rows)
        impressions = sum(int(self._number(r, "impressions")) for r in rows)
        clicks = sum(int(self._number(r, "clicks")) for r in rows)
        ctr = (clicks / impressions * 100.0) if impressions else 0.0

        self.stats_metric_labels["campaigns"].configure(text=f"{campaigns:,}")
        self.stats_metric_labels["adgroups"].configure(text=f"{adgroups:,}")
        self.stats_metric_labels["keywords"].configure(text=f"{keywords:,}")
        self.stats_metric_labels["impressions"].configure(text=f"{impressions:,}")
        self.stats_metric_labels["clicks"].configure(text=f"{clicks:,}")
        self.stats_metric_labels["ctr"].configure(text=f"{ctr:.2f}%")


def main() -> int:
    app = DesktopAppV2()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
