from __future__ import annotations

from collections import Counter
from tkinter import END, ttk

import customtkinter as ctk

from desktop_app_v4 import DesktopAppV4
from src.keyword_cleaner.policy_v2 import CleanerPolicy


BG = "#F4F7FB"
SIDEBAR = "#0B1220"
SIDEBAR_HOVER = "#172033"
PRIMARY = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
CARD = "#FFFFFF"
TEXT = "#0F172A"
MUTED = "#64748B"
BORDER = "#E2E8F0"
DANGER = "#DC2626"
SUCCESS = "#16A34A"
WARNING = "#D97706"

STATUS_COLORS = {
    "PERMANENT": "#16A34A",
    "PROTECTED_NEW": "#2563EB",
    "KEEP": "#0F766E",
    "WATCH": "#D97706",
    "DATA_INSUFFICIENT": "#64748B",
    "DELETE_PENDING": "#EA580C",
    "DELETE_APPROVED": "#DC2626",
}

STATUS_KO = {
    "PERMANENT": "영구 보호",
    "PROTECTED_NEW": "보호 중",
    "KEEP": "유지",
    "WATCH": "관찰",
    "DATA_INSUFFICIENT": "데이터 부족",
    "DELETE_PENDING": "삭제 대기",
    "DELETE_APPROVED": "삭제 승인",
}

NAV_ITEMS = [
    ("dashboard", "대시보드"),
    ("cleanup", "키워드 정리"),
    ("stats", "통계 조회"),
    ("protection", "보호 키워드 관리"),
    ("delete_queue", "삭제 대기"),
    ("history", "삭제 기록 / 복원"),
    ("settings", "설정"),
]


class DesktopAppV5(DesktopAppV4):
    """Modern visual shell for the existing safe keyword-cleaner workflows."""

    def __init__(self) -> None:
        super().__init__()
        self.title("DAYLAW Keyword Cleaner")
        self.geometry("1460x900")
        self.minsize(1240, 760)
        self.configure(fg_color=BG)
        self._configure_table_style()

    def _configure_table_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground="#1E293B",
            rowheight=31,
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#F8FAFC",
            foreground="#475569",
            borderwidth=0,
            relief="flat",
            font=("Segoe UI Semibold", 10),
            padding=(10, 9),
        )
        style.map(
            "Treeview",
            background=[("selected", "#DBEAFE")],
            foreground=[("selected", "#0F172A")],
        )

    # ------------------------------------------------------------------
    # Shell / navigation
    # ------------------------------------------------------------------
    def _build_sidebar(self) -> None:
        self.sidebar.configure(fg_color=SIDEBAR, width=238)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=22, pady=(28, 24))
        ctk.CTkLabel(
            brand,
            text="DAYLAW",
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=27, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            brand,
            text="KEYWORD CLEANER  •  v9",
            text_color="#94A3B8",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(anchor="w", pady=(4, 0))

        divider = ctk.CTkFrame(self.sidebar, height=1, fg_color="#1E293B")
        divider.pack(fill="x", padx=18, pady=(0, 15))

        for key, label in NAV_ITEMS:
            button = ctk.CTkButton(
                self.sidebar,
                text=label,
                height=44,
                anchor="w",
                corner_radius=10,
                border_width=0,
                fg_color="transparent",
                hover_color=SIDEBAR_HOVER,
                text_color="#CBD5E1",
                font=ctk.CTkFont(size=14),
                command=lambda k=key: self.show_page(k),
            )
            button.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[key] = button

        safe = ctk.CTkFrame(self.sidebar, fg_color="#111C2F", corner_radius=12)
        safe.pack(side="bottom", fill="x", padx=14, pady=18)
        ctk.CTkLabel(
            safe,
            text="●  SAFE MODE",
            text_color="#86EFAC",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(anchor="w", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            safe,
            text="실제 삭제는 승인 + 재검증을\n모두 통과한 항목만 실행됩니다.",
            justify="left",
            text_color="#94A3B8",
            font=ctk.CTkFont(size=10),
        ).pack(anchor="w", padx=14, pady=(0, 12))

    def _page(self, key: str, title: str, subtitle: str = "") -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.content, fg_color=BG, corner_radius=0)
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=32, pady=(28, 14))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(
            title_row,
            text=title,
            font=ctk.CTkFont(size=29, weight="bold"),
            text_color=TEXT,
        ).pack(side="left", anchor="w")
        ctk.CTkLabel(
            title_row,
            text="DAYLAW  ·  안전 운영",
            height=26,
            corner_radius=13,
            fg_color="#E8F0FF",
            text_color=PRIMARY,
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(side="right", padx=(10, 0))

        if subtitle:
            ctk.CTkLabel(
                header,
                text=subtitle,
                font=ctk.CTkFont(size=13),
                text_color=MUTED,
            ).pack(anchor="w", pady=(5, 0))

        self.pages[key] = frame
        return frame

    def show_page(self, key: str) -> None:
        super().show_page(key)
        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(
                fg_color=PRIMARY if active else "transparent",
                hover_color=PRIMARY_HOVER if active else SIDEBAR_HOVER,
                text_color="#FFFFFF" if active else "#CBD5E1",
            )

    # ------------------------------------------------------------------
    # Modern dashboard
    # ------------------------------------------------------------------
    def _build_dashboard_page(self) -> None:
        page = self._page(
            "dashboard",
            "대시보드",
            "계정 상태, 정리 목표, 보호 현황과 실행 단계를 한눈에 확인합니다.",
        )

        self.dashboard_cards = ctk.CTkFrame(page, fg_color="transparent")
        self.dashboard_cards.pack(fill="x", padx=26, pady=(2, 10))
        for column in range(4):
            self.dashboard_cards.grid_columnconfigure(column, weight=1)

        self.metric_labels = {}
        self.metric_hints = {}
        cards = [
            ("campaigns", "캠페인", "조회 대상"),
            ("adgroups", "광고그룹", "전체 운영 단위"),
            ("keywords", "키워드", "현재 계정 총량"),
            ("pending", "삭제 대기", "재검증 대기 포함"),
        ]
        for idx, (key, title, hint) in enumerate(cards):
            card = ctk.CTkFrame(
                self.dashboard_cards,
                fg_color=CARD,
                corner_radius=16,
                border_width=1,
                border_color=BORDER,
            )
            card.grid(row=0, column=idx, sticky="nsew", padx=6, pady=4)
            ctk.CTkLabel(
                card,
                text=title,
                text_color=MUTED,
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(anchor="w", padx=18, pady=(16, 2))
            value = ctk.CTkLabel(
                card,
                text="-",
                font=ctk.CTkFont(size=30, weight="bold"),
                text_color=TEXT,
            )
            value.pack(anchor="w", padx=18, pady=(1, 2))
            hint_label = ctk.CTkLabel(
                card,
                text=hint,
                text_color="#94A3B8",
                font=ctk.CTkFont(size=10),
            )
            hint_label.pack(anchor="w", padx=18, pady=(0, 15))
            self.metric_labels[key] = value
            self.metric_hints[key] = hint_label

        capacity = ctk.CTkFrame(
            page,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        capacity.pack(fill="x", padx=32, pady=(2, 12))
        cap_head = ctk.CTkFrame(capacity, fg_color="transparent")
        cap_head.pack(fill="x", padx=18, pady=(15, 8))
        ctk.CTkLabel(
            cap_head,
            text="계정 용량 관리",
            text_color=TEXT,
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(side="left")
        self.capacity_label = ctk.CTkLabel(
            cap_head,
            text="스캔 후 표시",
            text_color=MUTED,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.capacity_label.pack(side="right")
        self.capacity_bar = ctk.CTkProgressBar(
            capacity,
            height=10,
            corner_radius=5,
            progress_color=PRIMARY,
            fg_color="#E8EEF7",
        )
        self.capacity_bar.pack(fill="x", padx=18, pady=(0, 8))
        self.capacity_bar.set(0)
        self.capacity_hint = ctk.CTkLabel(
            capacity,
            text="90,000개부터 정리 시작 · 82,000개 부근에서 정리 중단",
            text_color=MUTED,
            font=ctk.CTkFont(size=10),
        )
        self.capacity_hint.pack(anchor="w", padx=18, pady=(0, 14))

        lower = ctk.CTkFrame(page, fg_color="transparent")
        lower.pack(fill="both", expand=True, padx=32, pady=(0, 28))
        lower.grid_columnconfigure(0, weight=6)
        lower.grid_columnconfigure(1, weight=4)
        lower.grid_rowconfigure(0, weight=1)

        status_card = ctk.CTkFrame(
            lower,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        status_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ctk.CTkLabel(
            status_card,
            text="판정 현황",
            text_color=TEXT,
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(
            status_card,
            text="최근 V2 스캔 기준",
            text_color="#94A3B8",
            font=ctk.CTkFont(size=10),
        ).pack(anchor="w", padx=20, pady=(0, 10))

        status_grid = ctk.CTkFrame(status_card, fg_color="transparent")
        status_grid.pack(fill="x", padx=16, pady=(0, 8))
        status_grid.grid_columnconfigure((0, 1), weight=1)
        self.status_value_labels = {}
        for idx, key in enumerate(STATUS_KO):
            row, col = divmod(idx, 2)
            cell = ctk.CTkFrame(status_grid, fg_color="#F8FAFC", corner_radius=11)
            cell.grid(row=row, column=col, sticky="ew", padx=4, pady=4)
            marker = ctk.CTkLabel(
                cell,
                text="●",
                text_color=STATUS_COLORS[key],
                font=ctk.CTkFont(size=11),
            )
            marker.pack(side="left", padx=(12, 7), pady=11)
            ctk.CTkLabel(
                cell,
                text=STATUS_KO[key],
                text_color="#475569",
                font=ctk.CTkFont(size=11, weight="bold"),
            ).pack(side="left", pady=11)
            value = ctk.CTkLabel(
                cell,
                text="0",
                text_color=TEXT,
                font=ctk.CTkFont(size=13, weight="bold"),
            )
            value.pack(side="right", padx=12, pady=11)
            self.status_value_labels[key] = value

        action_card = ctk.CTkFrame(
            lower,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        action_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ctk.CTkLabel(
            action_card,
            text="빠른 작업",
            text_color=TEXT,
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(18, 3))
        ctk.CTkLabel(
            action_card,
            text="권장 순서대로 진행하면 됩니다.",
            text_color="#94A3B8",
            font=ctk.CTkFont(size=10),
        ).pack(anchor="w", padx=20, pady=(0, 10))

        ctk.CTkButton(
            action_card,
            text="1  ·  V2 전체 스캔",
            height=46,
            corner_radius=10,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.run_script("run_v2_scan.py"),
        ).pack(fill="x", padx=20, pady=5)
        ctk.CTkButton(
            action_card,
            text="2  ·  안전 삭제 계획 생성",
            height=46,
            corner_radius=10,
            fg_color="#1E293B",
            hover_color="#0F172A",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.run_script("build_v2_delete_plan.py"),
        ).pack(fill="x", padx=20, pady=5)
        ctk.CTkButton(
            action_card,
            text="3  ·  삭제 실행기 DRY RUN",
            height=46,
            corner_radius=10,
            fg_color="#475569",
            hover_color="#334155",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.run_script("execute_v2_delete.py"),
        ).pack(fill="x", padx=20, pady=5)

        self.status_text = ctk.CTkTextbox(
            action_card,
            height=104,
            corner_radius=10,
            fg_color="#F8FAFC",
            border_width=1,
            border_color=BORDER,
            text_color="#475569",
            font=ctk.CTkFont(size=10),
        )
        self.status_text.pack(fill="both", expand=True, padx=20, pady=(10, 18))
        self.status_text.insert("1.0", "대기 중입니다.\n스캔 실행 시 진행 상태가 이곳에 표시됩니다.")

    def refresh_dashboard(self) -> None:
        rows = self._read_csv(self._latest("v2_scan_*.csv"))
        if not rows:
            for label in self.metric_labels.values():
                label.configure(text="-")
            for label in self.status_value_labels.values():
                label.configure(text="0")
            self.capacity_bar.set(0)
            self.capacity_label.configure(text="스캔 후 표시")
            self.status_text.delete("1.0", END)
            self.status_text.insert("1.0", "아직 V2 스캔 결과가 없습니다.\n먼저 V2 전체 스캔을 실행해주세요.")
            return

        policy = CleanerPolicy.load()
        campaigns = len({r.get("campaign_id", "") for r in rows if r.get("campaign_id")})
        adgroups = len({r.get("adgroup_id", "") for r in rows if r.get("adgroup_id")})
        statuses = Counter(r.get("status", "UNKNOWN") for r in rows)
        total = len(rows)
        pending = statuses.get("DELETE_PENDING", 0) + statuses.get("DELETE_APPROVED", 0)
        target_remove = max(total - policy.cleanup_stop, 0) if total >= policy.cleanup_start else 0

        self.metric_labels["campaigns"].configure(text=f"{campaigns:,}")
        self.metric_labels["adgroups"].configure(text=f"{adgroups:,}")
        self.metric_labels["keywords"].configure(text=f"{total:,}")
        self.metric_labels["pending"].configure(text=f"{pending:,}")
        self.metric_hints["keywords"].configure(
            text=f"한도 {policy.hard_limit_reference:,}개 기준"
        )
        self.metric_hints["pending"].configure(
            text=f"삭제 승인 {statuses.get('DELETE_APPROVED', 0):,}개"
        )

        for key, label in self.status_value_labels.items():
            label.configure(text=f"{statuses.get(key, 0):,}")

        ratio = min(total / max(policy.hard_limit_reference, 1), 1.0)
        self.capacity_bar.set(ratio)
        if total >= policy.cleanup_start:
            self.capacity_bar.configure(progress_color=WARNING if total < policy.hard_limit_reference else DANGER)
            self.capacity_label.configure(
                text=f"{total:,} / {policy.hard_limit_reference:,}  ·  정리 목표 {target_remove:,}개"
            )
        else:
            self.capacity_bar.configure(progress_color=SUCCESS)
            self.capacity_label.configure(
                text=f"{total:,} / {policy.hard_limit_reference:,}  ·  정리 불필요"
            )
        self.capacity_hint.configure(
            text=(
                f"{policy.cleanup_start:,}개부터 정리 시작 · "
                f"{policy.cleanup_stop:,}개 부근에서 자동 중단"
            )
        )

        self.status_text.delete("1.0", END)
        if statuses.get("DELETE_APPROVED", 0) > 0:
            self.status_text.insert(
                "1.0",
                f"삭제 승인 {statuses.get('DELETE_APPROVED', 0):,}개가 있습니다.\n"
                "삭제 대기 화면에서 DRY RUN 후 실제 삭제를 진행하세요.",
            )
        elif statuses.get("DELETE_PENDING", 0) > 0:
            self.status_text.insert(
                "1.0",
                f"삭제 대기 {statuses.get('DELETE_PENDING', 0):,}개 · 승인 0개\n"
                f"{policy.pending_recheck_days}일 재검증 게이트가 적용 중입니다.",
            )
        else:
            self.status_text.insert("1.0", "현재 실행 가능한 삭제 항목이 없습니다.")


def main() -> int:
    app = DesktopAppV5()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
