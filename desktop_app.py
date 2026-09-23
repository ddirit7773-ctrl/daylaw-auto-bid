from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from tkinter import END, StringVar, ttk, messagebox

import customtkinter as ctk

from src.keyword_cleaner.policy_v2 import CleanerPolicy


ROOT_DIR = Path(__file__).resolve().parent
BACKUP_DIR = ROOT_DIR / "data" / "backups"
CONFIG_DIR = ROOT_DIR / "config"

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


class DesktopApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DAYLAW Keyword Cleaner")
        self.geometry("1380x860")
        self.minsize(1180, 720)

        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(side="left", fill="both", expand=True)

        self.pages: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}

        self._build_sidebar()
        self._build_pages()
        self.show_page("dashboard")

    def _build_sidebar(self) -> None:
        ctk.CTkLabel(
            self.sidebar,
            text="DAYLAW",
            font=ctk.CTkFont(size=26, weight="bold"),
        ).pack(anchor="w", padx=24, pady=(28, 2))
        ctk.CTkLabel(
            self.sidebar,
            text="Keyword Cleaner",
            text_color="#64748B",
            font=ctk.CTkFont(size=13),
        ).pack(anchor="w", padx=24, pady=(0, 24))

        items = [
            ("dashboard", "대시보드"),
            ("cleanup", "키워드 정리"),
            ("stats", "통계 조회"),
            ("protection", "보호 키워드 관리"),
            ("delete_queue", "삭제 대기"),
            ("history", "삭제 기록 / 복원"),
            ("settings", "설정"),
        ]
        for key, label in items:
            button = ctk.CTkButton(
                self.sidebar,
                text=label,
                height=42,
                anchor="w",
                corner_radius=8,
                fg_color="transparent",
                hover_color="#E8EEF8",
                text_color="#1E293B",
                command=lambda k=key: self.show_page(k),
            )
            button.pack(fill="x", padx=14, pady=3)
            self.nav_buttons[key] = button

        ctk.CTkLabel(
            self.sidebar,
            text="실제 삭제는 승인 + 재검증 후만 실행",
            wraplength=170,
            justify="left",
            text_color="#64748B",
            font=ctk.CTkFont(size=11),
        ).pack(side="bottom", anchor="w", padx=22, pady=24)

    def _page(self, key: str, title: str, subtitle: str = "") -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.content, fg_color="#F8FAFC", corner_radius=0)
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(26, 12))
        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color="#0F172A",
        ).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                header,
                text=subtitle,
                font=ctk.CTkFont(size=13),
                text_color="#64748B",
            ).pack(anchor="w", pady=(4, 0))
        self.pages[key] = frame
        return frame

    def _build_pages(self) -> None:
        self._build_dashboard_page()
        self._build_cleanup_page()
        self._build_stats_page()
        self._build_protection_page()
        self._build_delete_queue_page()
        self._build_history_page()
        self._build_settings_page()

    def show_page(self, key: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        page = self.pages[key]
        page.pack(fill="both", expand=True)

        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(
                fg_color="#2563EB" if active else "transparent",
                text_color="white" if active else "#1E293B",
            )

        if key == "dashboard":
            self.refresh_dashboard()
        elif key == "delete_queue":
            self.refresh_delete_queue()

    @staticmethod
    def _latest(pattern: str) -> Path | None:
        files = list(BACKUP_DIR.glob(pattern))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _read_csv(path: Path | None) -> list[dict[str, str]]:
        if path is None or not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            return list(csv.DictReader(fp))

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    def _build_dashboard_page(self) -> None:
        page = self._page(
            "dashboard",
            "대시보드",
            "계정 상태와 키워드 정리 현황을 한눈에 확인합니다.",
        )
        self.dashboard_cards = ctk.CTkFrame(page, fg_color="transparent")
        self.dashboard_cards.pack(fill="x", padx=28, pady=(4, 12))
        for column in range(4):
            self.dashboard_cards.grid_columnconfigure(column, weight=1)

        self.metric_labels: dict[str, ctk.CTkLabel] = {}
        cards = [
            ("campaigns", "캠페인"),
            ("adgroups", "광고그룹"),
            ("keywords", "키워드"),
            ("pending", "삭제 대기"),
        ]
        for idx, (key, title) in enumerate(cards):
            card = ctk.CTkFrame(self.dashboard_cards, fg_color="white", corner_radius=12)
            card.grid(row=0, column=idx, sticky="nsew", padx=6, pady=4)
            ctk.CTkLabel(card, text=title, text_color="#64748B").pack(anchor="w", padx=18, pady=(16, 4))
            value = ctk.CTkLabel(
                card,
                text="-",
                font=ctk.CTkFont(size=28, weight="bold"),
                text_color="#0F172A",
            )
            value.pack(anchor="w", padx=18, pady=(0, 16))
            self.metric_labels[key] = value

        lower = ctk.CTkFrame(page, fg_color="transparent")
        lower.pack(fill="both", expand=True, padx=28, pady=(0, 28))
        lower.grid_columnconfigure((0, 1), weight=1)
        lower.grid_rowconfigure(0, weight=1)

        status_card = ctk.CTkFrame(lower, fg_color="white", corner_radius=12)
        status_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ctk.CTkLabel(
            status_card,
            text="V2 판정 현황",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(anchor="w", padx=18, pady=(18, 10))
        self.status_text = ctk.CTkTextbox(status_card, height=250, fg_color="#F8FAFC")
        self.status_text.pack(fill="both", expand=True, padx=18, pady=(0, 18))

        action_card = ctk.CTkFrame(lower, fg_color="white", corner_radius=12)
        action_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ctk.CTkLabel(
            action_card,
            text="빠른 작업",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(anchor="w", padx=18, pady=(18, 10))
        ctk.CTkButton(
            action_card,
            text="V2 전체 스캔 실행",
            height=44,
            command=lambda: self.run_script("run_v2_scan.py"),
        ).pack(fill="x", padx=18, pady=6)
        ctk.CTkButton(
            action_card,
            text="안전 삭제 계획 생성",
            height=44,
            fg_color="#334155",
            hover_color="#1E293B",
            command=lambda: self.run_script("build_v2_delete_plan.py"),
        ).pack(fill="x", padx=18, pady=6)
        ctk.CTkButton(
            action_card,
            text="삭제 실행기 DRY RUN",
            height=44,
            fg_color="#475569",
            hover_color="#334155",
            command=lambda: self.run_script("execute_v2_delete.py"),
        ).pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(
            action_card,
            text="※ 실제 삭제 버튼은 삭제 대기 화면에서 별도 확인 후 제공됩니다.",
            wraplength=420,
            justify="left",
            text_color="#64748B",
        ).pack(anchor="w", padx=18, pady=(12, 18))

    def refresh_dashboard(self) -> None:
        rows = self._read_csv(self._latest("v2_scan_*.csv"))
        if not rows:
            for label in self.metric_labels.values():
                label.configure(text="-")
            self.status_text.delete("1.0", END)
            self.status_text.insert("1.0", "아직 V2 스캔 결과가 없습니다.")
            return

        campaigns = len({r.get("campaign_id", "") for r in rows if r.get("campaign_id")})
        adgroups = len({r.get("adgroup_id", "") for r in rows if r.get("adgroup_id")})
        statuses = Counter(r.get("status", "UNKNOWN") for r in rows)

        self.metric_labels["campaigns"].configure(text=f"{campaigns:,}")
        self.metric_labels["adgroups"].configure(text=f"{adgroups:,}")
        self.metric_labels["keywords"].configure(text=f"{len(rows):,}")
        pending = statuses.get("DELETE_PENDING", 0) + statuses.get("DELETE_APPROVED", 0)
        self.metric_labels["pending"].configure(text=f"{pending:,}")

        lines = [
            f"PERMANENT        {statuses.get('PERMANENT', 0):>10,}",
            f"PROTECTED_NEW    {statuses.get('PROTECTED_NEW', 0):>10,}",
            f"KEEP             {statuses.get('KEEP', 0):>10,}",
            f"WATCH            {statuses.get('WATCH', 0):>10,}",
            f"DATA_INSUFFICIENT{statuses.get('DATA_INSUFFICIENT', 0):>10,}",
            f"DELETE_PENDING   {statuses.get('DELETE_PENDING', 0):>10,}",
            f"DELETE_APPROVED  {statuses.get('DELETE_APPROVED', 0):>10,}",
        ]
        self.status_text.delete("1.0", END)
        self.status_text.insert("1.0", "\n".join(lines))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def _build_cleanup_page(self) -> None:
        page = self._page(
            "cleanup",
            "키워드 정리",
            "V2 분석 → 삭제 계획 → 재검증 순서로 안전하게 진행합니다.",
        )
        box = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        box.pack(fill="x", padx=28, pady=10)
        steps = [
            "1. 계정 전체를 다시 스캔합니다.",
            "2. 보호/유지/관찰/삭제대기 상태를 판정합니다.",
            "3. 정리 목표치만큼 GENERAL 후보를 안전하게 선별합니다.",
            "4. 7일 후 다시 조건을 통과한 항목만 DELETE_APPROVED가 됩니다.",
            "5. 실삭제 직전에도 상태와 통계를 다시 조회합니다.",
        ]
        for text in steps:
            ctk.CTkLabel(box, text=text, anchor="w").pack(fill="x", padx=20, pady=7)
        ctk.CTkButton(page, text="V2 스캔 실행", height=44, command=lambda: self.run_script("run_v2_scan.py")).pack(
            anchor="w", padx=28, pady=(10, 6)
        )
        ctk.CTkButton(
            page,
            text="삭제 계획 생성",
            height=44,
            fg_color="#334155",
            hover_color="#1E293B",
            command=lambda: self.run_script("build_v2_delete_plan.py"),
        ).pack(anchor="w", padx=28, pady=6)

    # ------------------------------------------------------------------
    # Stats placeholder
    # ------------------------------------------------------------------
    def _build_stats_page(self) -> None:
        page = self._page(
            "stats",
            "통계 조회",
            "기간별 노출수·클릭수·CTR과 업체명 정렬 기능을 넣을 화면입니다.",
        )
        box = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        box.pack(fill="both", expand=True, padx=28, pady=(10, 28))
        ctk.CTkLabel(
            box,
            text="다음 구현 단계",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 8))
        ctk.CTkLabel(
            box,
            text="날짜 범위 선택 · 캠페인 선택 · 업체명 검색/정렬 · 노출/클릭/CTR 정렬 · 광고그룹/키워드 수 표시",
            wraplength=900,
            justify="left",
            text_color="#475569",
        ).pack(anchor="w", padx=20, pady=(0, 20))

    # ------------------------------------------------------------------
    # Protection management
    # ------------------------------------------------------------------
    def _build_protection_page(self) -> None:
        page = self._page(
            "protection",
            "보호 키워드 관리",
            "언제든 보호 키워드와 접미어를 추가·수정할 수 있습니다.",
        )
        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=28, pady=(8, 28))
        grid.grid_columnconfigure((0, 1), weight=1)
        grid.grid_rowconfigure((0, 1), weight=1)

        configs = [
            ("protected_keywords.txt", "영구 보호 키워드", 0, 0),
            ("protected_suffixes.txt", "영구 보호 접미어", 0, 1),
            ("type_core_keywords.txt", "유형 핵심 키워드", 1, 0),
            ("type_core_suffixes.txt", "유형 핵심 접미어", 1, 1),
        ]
        self.protection_boxes: dict[str, ctk.CTkTextbox] = {}
        for filename, title, row, col in configs:
            card = ctk.CTkFrame(grid, fg_color="white", corner_radius=12)
            card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=15, weight="bold")).pack(
                anchor="w", padx=16, pady=(14, 6)
            )
            textbox = ctk.CTkTextbox(card)
            textbox.pack(fill="both", expand=True, padx=16, pady=(0, 10))
            path = CONFIG_DIR / filename
            if path.exists():
                textbox.insert("1.0", path.read_text(encoding="utf-8"))
            ctk.CTkButton(
                card,
                text="저장",
                command=lambda f=filename, t=textbox: self.save_text_config(f, t),
            ).pack(anchor="e", padx=16, pady=(0, 14))
            self.protection_boxes[filename] = textbox

    def save_text_config(self, filename: str, textbox: ctk.CTkTextbox) -> None:
        path = CONFIG_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        text = textbox.get("1.0", END).strip()
        path.write_text((text + "\n") if text else "", encoding="utf-8")
        messagebox.showinfo("저장 완료", f"{filename} 저장이 완료되었습니다.")

    # ------------------------------------------------------------------
    # Delete queue
    # ------------------------------------------------------------------
    def _build_delete_queue_page(self) -> None:
        page = self._page(
            "delete_queue",
            "삭제 대기",
            "DELETE_PENDING / DELETE_APPROVED 키워드를 확인합니다.",
        )
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.pack(fill="x", padx=28, pady=(4, 8))
        self.delete_queue_summary = ctk.CTkLabel(
            top,
            text="-",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.delete_queue_summary.pack(side="left")
        ctk.CTkButton(top, text="새로고침", width=100, command=self.refresh_delete_queue).pack(side="right")

        table_wrap = ctk.CTkFrame(page, fg_color="white", corner_radius=12)
        table_wrap.pack(fill="both", expand=True, padx=28, pady=(0, 28))

        columns = ("campaign", "adgroup", "keyword", "tier", "status", "age", "reason")
        self.queue_tree = ttk.Treeview(table_wrap, columns=columns, show="headings", height=22)
        headings = {
            "campaign": "캠페인",
            "adgroup": "업체명/광고그룹",
            "keyword": "키워드",
            "tier": "구분",
            "status": "상태",
            "age": "등록일수",
            "reason": "판정 사유",
        }
        widths = {"campaign": 100, "adgroup": 160, "keyword": 190, "tier": 95, "status": 130, "age": 80, "reason": 430}
        for key in columns:
            self.queue_tree.heading(key, text=headings[key])
            self.queue_tree.column(key, width=widths[key], anchor="w")

        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.queue_tree.yview)
        xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.queue_tree.xview)
        self.queue_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.queue_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        yscroll.pack(side="right", fill="y", pady=12, padx=(0, 6))
        xscroll.pack(side="bottom", fill="x", padx=12, pady=(0, 6))

    def refresh_delete_queue(self) -> None:
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        rows = self._read_csv(self._latest("v2_scan_*.csv"))
        queue = [r for r in rows if r.get("status") in {"DELETE_PENDING", "DELETE_APPROVED"}]
        approved = sum(1 for r in queue if r.get("status") == "DELETE_APPROVED")
        pending = len(queue) - approved
        self.delete_queue_summary.configure(text=f"삭제대기 {pending:,}개 · 삭제승인 {approved:,}개")

        for row in queue[:1000]:
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

    # ------------------------------------------------------------------
    # History placeholder
    # ------------------------------------------------------------------
    def _build_history_page(self) -> None:
        page = self._page(
            "history",
            "삭제 기록 / 복원",
            "실제 삭제가 시작되면 보관된 archive와 복원 기능을 연결합니다.",
        )
        ctk.CTkLabel(
            page,
            text="현재는 실제 삭제가 아직 시작되지 않아 기록이 없습니다.",
            text_color="#64748B",
        ).pack(anchor="w", padx=28, pady=12)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _build_settings_page(self) -> None:
        page = self._page(
            "settings",
            "설정",
            "삭제 기준 숫자를 프로그램에서 직접 수정할 수 있습니다.",
        )
        policy = CleanerPolicy.load()

        form = ctk.CTkScrollableFrame(page, fg_color="white", corner_radius=12)
        form.pack(fill="both", expand=True, padx=28, pady=(8, 28))
        form.grid_columnconfigure(1, weight=1)

        fields = [
            ("general_protection_days", "일반 신규 보호기간 (일)", policy.general_protection_days),
            ("type_core_protection_days", "유형 핵심 보호기간 (일)", policy.type_core_protection_days),
            ("general_inactivity_days", "일반 무활동 판단기간 (일)", policy.general_inactivity_days),
            ("type_core_inactivity_days", "유형 핵심 무활동 판단기간 (일)", policy.type_core_inactivity_days),
            ("click_protection_days", "클릭 발생 시 보호기간 (일)", policy.click_protection_days),
            ("pending_recheck_days", "삭제후보 재검증 대기 (일)", policy.pending_recheck_days),
            ("cleanup_start", "정리 시작 기준 (개)", policy.cleanup_start),
            ("cleanup_stop", "정리 중단 기준 (개)", policy.cleanup_stop),
            ("first_test_batch", "최초 테스트 삭제량", policy.first_test_batch),
            ("second_test_batch", "2차 테스트 삭제량", policy.second_test_batch),
            ("normal_batch", "정상 삭제 단위", policy.normal_batch),
        ]
        self.setting_vars: dict[str, StringVar] = {}
        for row, (key, label, value) in enumerate(fields):
            ctk.CTkLabel(form, text=label).grid(row=row, column=0, sticky="w", padx=18, pady=8)
            var = StringVar(value=str(value))
            entry = ctk.CTkEntry(form, textvariable=var, width=220)
            entry.grid(row=row, column=1, sticky="w", padx=18, pady=8)
            self.setting_vars[key] = var

        buttons = ctk.CTkFrame(form, fg_color="transparent")
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=18, pady=(18, 22))
        ctk.CTkButton(buttons, text="저장", width=130, command=self.save_settings).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            buttons,
            text="현재값 다시 불러오기",
            width=170,
            fg_color="#475569",
            hover_color="#334155",
            command=self.reload_settings,
        ).pack(side="left")

    def save_settings(self) -> None:
        try:
            current = CleanerPolicy.load()
            values = {key: int(var.get().replace(",", "").strip()) for key, var in self.setting_vars.items()}
            updated = current.with_updates(**values)
            updated.save()
        except Exception as exc:
            messagebox.showerror("설정 저장 실패", str(exc))
            return
        messagebox.showinfo("저장 완료", "삭제 기준 설정이 저장되었습니다. 다음 스캔부터 적용됩니다.")

    def reload_settings(self) -> None:
        policy = CleanerPolicy.load()
        mapping = {
            "general_protection_days": policy.general_protection_days,
            "type_core_protection_days": policy.type_core_protection_days,
            "general_inactivity_days": policy.general_inactivity_days,
            "type_core_inactivity_days": policy.type_core_inactivity_days,
            "click_protection_days": policy.click_protection_days,
            "pending_recheck_days": policy.pending_recheck_days,
            "cleanup_start": policy.cleanup_start,
            "cleanup_stop": policy.cleanup_stop,
            "first_test_batch": policy.first_test_batch,
            "second_test_batch": policy.second_test_batch,
            "normal_batch": policy.normal_batch,
        }
        for key, value in mapping.items():
            self.setting_vars[key].set(str(value))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def run_script(self, filename: str) -> None:
        path = ROOT_DIR / filename
        if not path.exists():
            messagebox.showerror("실행 실패", f"{filename} 파일을 찾을 수 없습니다.")
            return
        try:
            subprocess.Popen([sys.executable, str(path)], cwd=str(ROOT_DIR))
        except Exception as exc:
            messagebox.showerror("실행 실패", str(exc))
            return
        messagebox.showinfo(
            "실행 시작",
            f"{filename} 실행을 시작했습니다.\n현재 제작 단계에서는 터미널 로그에서 진행 상황을 확인합니다.",
        )


def main() -> int:
    app = DesktopApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
