"""화면 (멀티파이프 프로그램 공통 디자인: 파란 제목·둥근 파란 버튼)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
import traceback
import webbrowser
from pathlib import Path

from . import config, jobs, notify, records

log = logging.getLogger("app")
WEEK = ["월", "화", "수", "목", "금", "토", "일"]

# 색
BLUE = "#1f5bd8"
BLUE_DARK = "#1848b0"
TITLE_BLUE = "#1257d1"
BODY = "#e6e6e6"
WHITE = "#ffffff"
LINE = "#c9c9c9"
TABLE_LINE = "#5b7fbf"
TEXT = "#1f1f1f"
MUTED = "#6b6b6b"
GREEN = "#15803d"
RED = "#dc2626"
ORANGE = "#c2410c"
FONT = "맑은 고딕"

DEFAULT_LINKS = {
    "youtube": "https://www.youtube.com/@multi-pipe",
}


def branding() -> dict:
    """app\\branding.json 이 있으면 링크를 거기서 읽음 (exe 다시 안 만들고 바꿀 수 있게)."""
    d = dict(DEFAULT_LINKS)
    try:
        p = config.shared_dir() / "app" / "branding.json"
        if p.exists():
            d.update(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    return d


def run_gui(start_hidden: bool, updated: bool = False) -> None:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox, ttk

    from .app import (Runner, Scheduler, _spawn, autostart_enabled, cleanup_old_bins,
                      early_run, missed_days_notice, order_deadline_passed, release_single_instance,
                      set_autostart, show_flag)

    if config.IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 흐릿하지 않게
        except Exception:
            pass

    root = tk.Tk()
    S = max(1.0, root.winfo_fpixels("1i") / 96.0)  # 화면 배율

    def px(v: float) -> int:
        return int(round(v * S))

    root.title(f"멀티파이프 사방넷 발주 자동화 v{config.VERSION}")
    root.geometry(f"{px(1000)}x{px(900)}")
    root.minsize(px(900), px(720))
    root.configure(bg=BODY)
    try:
        root.iconbitmap(str(notify.icon_path()))
    except Exception:
        pass

    # 글씨체: 모든 기본 글꼴을 맑은 고딕으로 통일
    for nm in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
               "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(nm).configure(family=FONT, size=11)
        except Exception:
            pass
    root.option_add("*Font", (FONT, 11))
    root.option_add("*TCombobox*Listbox.font", (FONT, 11))
    root.option_add("*TCombobox*Listbox.selectBackground", "#dbe6fb")
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure(".", font=(FONT, 11))
    style.configure("TCombobox", padding=(px(6), px(4)), arrowsize=px(14), fieldbackground=WHITE, background=WHITE,
                    bordercolor="#b5b5b5", lightcolor=WHITE, darkcolor=WHITE, arrowcolor=TEXT)
    style.map("TCombobox", fieldbackground=[("readonly", WHITE)], selectbackground=[("readonly", WHITE)],
              selectforeground=[("readonly", TEXT)], bordercolor=[("focus", BLUE)])
    style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=TEXT,
                    rowheight=px(36), font=(FONT, 11), borderwidth=0)
    style.configure("Treeview.Heading", background=WHITE, foreground=TEXT, font=(FONT, 11, "bold"),
                    relief="flat", borderwidth=0, padding=(0, px(8)))
    style.map("Treeview.Heading", background=[("active", WHITE)])
    style.map("Treeview", background=[("selected", "#dbe6fb")], foreground=[("selected", TEXT)])

    links = branding()
    state = {"tray": None, "tab": "상태"}

    # ------------------------------------------------------------ 위젯
    class RoundButton(tk.Canvas):
        """둥근 버튼 (파란 채움 / 흰 바탕 테두리)."""

        def __init__(self, parent, text, command, *, kind="primary", size="md", bg=BODY, width=None):
            f = tkfont.Font(family=FONT, size={"sm": 10, "md": 11, "lg": 12}[size], weight="bold")
            h = px({"sm": 36, "md": 46, "lg": 50}[size])
            w = width or (f.measure(text) + px(56))
            super().__init__(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
            self.text, self.command, self.kind, self.font = text, command, kind, f
            self.w, self.h = w, h
            self.enabled = True
            self._draw(False)
            self.bind("<Button-1>", self._click)
            self.bind("<Enter>", lambda e: self._draw(True))
            self.bind("<Leave>", lambda e: self._draw(False))

        def _rr(self, x1, y1, x2, y2, r, **kw):
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                   x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            return self.create_polygon(pts, smooth=True, **kw)

        def _draw(self, hover):
            self.delete("all")
            r = px(12)
            if self.kind == "primary":
                fill = "#9bb3e6" if not self.enabled else (BLUE_DARK if hover else BLUE)
                self._rr(1, 1, self.w - 1, self.h - 1, r, fill=fill, outline=fill)
                color = WHITE
            else:
                fill = "#f3f5f9" if hover else WHITE
                self._rr(1, 1, self.w - 1, self.h - 1, r, fill=fill, outline="#a9a9a9", width=1)
                color = TEXT if self.enabled else MUTED
            self.create_text(self.w / 2, self.h / 2, text=self.text, fill=color, font=self.font)

        def _click(self, _e):
            if self.enabled and self.command:
                self.command()

        def set_enabled(self, on: bool):
            self.enabled = on
            self.configure(cursor="hand2" if on else "arrow")
            self._draw(False)

    class CheckBox(tk.Frame):
        """큼직한 파란 V 체크박스."""

        def __init__(self, parent, text, variable, bg=WHITE):
            super().__init__(parent, bg=bg, cursor="hand2")
            self.var = variable
            self.d = px(22)
            self.c = tk.Canvas(self, width=self.d, height=self.d, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
            self.c.pack(side="left")
            self.lb = tk.Label(self, text=text, bg=bg, fg=TEXT, font=(FONT, 11), cursor="hand2")
            self.lb.pack(side="left", padx=(px(8), 0))
            for w in (self, self.c, self.lb):
                w.bind("<Button-1>", self.toggle)
            self.var.trace_add("write", lambda *a: self.draw())
            self.draw()

        def toggle(self, _e=None):
            self.var.set(not bool(self.var.get()))

        def draw(self):
            c, d = self.c, self.d
            c.delete("all")
            on = bool(self.var.get())
            r = px(5)
            pts = [2 + r, 2, d - 2 - r, 2, d - 2, 2, d - 2, 2 + r, d - 2, d - 2 - r, d - 2, d - 2, d - 2 - r, d - 2,
                   2 + r, d - 2, 2, d - 2, 2, d - 2 - r, 2, 2 + r, 2, 2]
            c.create_polygon(pts, smooth=True, fill=BLUE if on else WHITE, outline=BLUE if on else "#9a9a9a",
                             width=max(1, px(1.6)))
            if on:
                c.create_line(d * 0.24, d * 0.52, d * 0.43, d * 0.72, d * 0.78, d * 0.30, fill=WHITE,
                              width=max(2, px(2.6)), capstyle="round", joinstyle="round")

    class RadioBox(tk.Frame):
        """파란 동그라미 선택."""

        def __init__(self, parent, text, variable, value, bg=WHITE):
            super().__init__(parent, bg=bg, cursor="hand2")
            self.var, self.value = variable, value
            self.d = px(22)
            self.c = tk.Canvas(self, width=self.d, height=self.d, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
            self.c.pack(side="left")
            self.lb = tk.Label(self, text=text, bg=bg, fg=TEXT, font=(FONT, 11), cursor="hand2")
            self.lb.pack(side="left", padx=(px(8), 0))
            for w in (self, self.c, self.lb):
                w.bind("<Button-1>", lambda e: self.var.set(self.value))
            self.var.trace_add("write", lambda *a: self.draw())
            self.draw()

        def draw(self):
            c, d = self.c, self.d
            c.delete("all")
            on = self.var.get() == self.value
            w = max(1, px(1.6))
            c.create_oval(2, 2, d - 2, d - 2, outline=BLUE if on else "#9a9a9a", width=w, fill=WHITE)
            if on:
                c.create_oval(d * 0.28, d * 0.28, d * 0.72, d * 0.72, fill=BLUE, outline=BLUE)

    def combo(parent, var, values, width=5):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width, state="readonly",
                          font=(FONT, 11), justify="center")
        return cb

    def time_picker(parent, var):
        """시(00~23) : 분(10분 단위) 클릭 선택."""
        f = tk.Frame(parent, bg=WHITE)
        try:
            h, m = var.get().strip().split(":")
            h, m = int(h), (int(m) // 10) * 10
        except Exception:
            h, m = 0, 0
        hv, mv = tk.StringVar(value=f"{h:02d}"), tk.StringVar(value=f"{m:02d}")
        combo(f, hv, [f"{i:02d}" for i in range(24)], 4).pack(side="left")
        tk.Label(f, text="시", bg=WHITE, fg=TEXT, font=(FONT, 11)).pack(side="left", padx=(px(6), px(12)))
        combo(f, mv, [f"{i:02d}" for i in range(0, 60, 10)], 4).pack(side="left")
        tk.Label(f, text="분", bg=WHITE, fg=TEXT, font=(FONT, 11)).pack(side="left", padx=(px(6), 0))

        def upd(*_a):
            var.set(f"{hv.get()}:{mv.get()}")
        hv.trace_add("write", upd)
        mv.trace_add("write", upd)
        upd()
        return f

    def card(parent, **kw):
        return tk.Frame(parent, bg=WHITE, highlightthickness=1, highlightbackground=TABLE_LINE, **kw)

    def label(parent, text, *, size=11, bold=False, color=TEXT, bg=WHITE, **kw):
        return tk.Label(parent, text=text, bg=bg, fg=color, font=(FONT, size, "bold" if bold else "normal"), **kw)

    def entry(parent, var, width=22, show=None):
        e = tk.Entry(parent, textvariable=var, width=width, font=(FONT, 11), relief="flat", bd=0,
                     highlightthickness=1, highlightbackground="#b5b5b5", highlightcolor=BLUE, show=show or "")
        return e

    # ------------------------------------------------------------ 머리
    head = tk.Frame(root, bg=WHITE)
    head.pack(fill="x")
    title = tk.Frame(head, bg=WHITE)
    title.pack(side="left", padx=px(28), pady=(px(22), px(18)))
    tk.Label(title, text="멀티파이프", bg=WHITE, fg=TITLE_BLUE, font=(FONT, 26, "bold")).pack(side="left")
    tk.Label(title, text=" 사방넷 발주 자동화", bg=WHITE, fg=TEXT, font=(FONT, 26, "bold")).pack(side="left")
    RoundButton(head, "▶  멀티파이프 YouTube 채널", lambda: webbrowser.open(links["youtube"]),
                kind="outline", size="md", bg=WHITE).pack(side="right", padx=px(24))

    # ------------------------------------------------------------ 탭
    tabbar = tk.Frame(root, bg=WHITE)
    tabbar.pack(fill="x")
    tk.Frame(root, bg=LINE, height=1).pack(fill="x")
    body = tk.Frame(root, bg=BODY)
    body.pack(fill="both", expand=True)
    pages: dict[str, tk.Frame] = {}
    tab_labels: dict[str, tk.Label] = {}

    def select_tab(name: str):
        state["tab"] = name
        for n, lb in tab_labels.items():
            on = n == name
            lb.configure(bg=WHITE if on else "#efefef", fg=TEXT, relief="solid" if on else "flat",
                         bd=1 if on else 0)
        pages[name].tkraise()

    for n in ("상태", "설정", "계정"):
        lb = tk.Label(tabbar, text=f"  {n}  ", font=(FONT, 11), bg="#efefef", padx=px(14), pady=px(6), cursor="hand2")
        lb.pack(side="left", padx=(px(2), 0), pady=(px(4), 0))
        lb.bind("<Button-1>", lambda e, n=n: select_tab(n))
        tab_labels[n] = lb
        pg = tk.Frame(body, bg=BODY)
        pg.place(relx=0, rely=0, relwidth=1, relheight=1)
        pages[n] = pg

    # ------------------------------------------------------------ 아래 (꼬리말)
    foot = tk.Frame(root, bg=WHITE)
    foot.pack(fill="x", side="bottom", before=body)
    foot_line = tk.Frame(foot, bg=WHITE)
    foot_line.pack(fill="x", padx=px(16), pady=px(8))
    tk.Label(foot_line, text="Made by MULTIPIPE", bg=WHITE, fg=MUTED, font=(FONT, 10)).pack(side="left")
    lk = tk.Label(foot_line, text="YouTube 채널", bg=WHITE, fg=TITLE_BLUE, font=(FONT, 10), cursor="hand2")
    lk.pack(side="right")
    lk.bind("<Button-1>", lambda e: webbrowser.open(links["youtube"]))

    # ------------------------------------------------------------ 작업 실행
    def ui(fn, *a):
        root.after(0, lambda: fn(*a))

    def on_job_done(name, res):
        ui(refresh)
        if res and res.get("pending"):
            ui(show_window, "상태")

    runner = Runner(on_done=on_job_done)

    def on_update_ready(nxt: Path):
        def go():
            if runner.busy:
                return
            log.info(f"새 버전으로 교체: {nxt}")
            quit_app(restart_with=nxt)
        ui(go)

    sched = Scheduler(runner, on_update_ready)

    def need_account() -> bool:
        if not config.load_secrets().mini_ready():
            messagebox.showwarning("사방넷 봇", "먼저 [계정] 탭에서 아이디/비밀번호를 저장해주세요.")
            select_tab("계정")
            return True
        return False

    def run_now():
        if need_account():
            return
        st = records.job_state("order")
        if st.get("status") == "done" and not messagebox.askyesno("사방넷 봇", "오늘 발주 작업이 이미 끝났습니다. 한 번 더 실행할까요?"):
            return
        if not runner.start("발주", jobs.run_order, "manual"):
            messagebox.showinfo("사방넷 봇", "이미 실행 중입니다.")
        refresh()

    def invoice_now():
        if need_account():
            return
        if not runner.start("송장", jobs.run_invoice, "manual"):
            messagebox.showinfo("사방넷 봇", "이미 실행 중입니다.")
        refresh()

    def check_now():
        if need_account():
            return
        if not runner.start("점검", jobs.run_check):
            messagebox.showinfo("사방넷 봇", "이미 실행 중입니다.")
        refresh()

    def open_logs():
        p = config.logs_root() / dt.date.today().isoformat()
        p = p if p.exists() else config.logs_root()
        if config.IS_WIN:
            os.startfile(str(p))

    def export_logs():
        """최근 3일 로그·실행기록·설정(계정 제외)·app.log를 zip 하나로 바탕화면에 → 카톡으로 보내면 원격에서 원인 확인 가능."""
        try:
            out = records.export_logs(days=3)
        except Exception as e:
            messagebox.showerror("사방넷 봇", f"로그 묶기 실패: {e}")
            return
        messagebox.showinfo("사방넷 봇", f"로그 파일을 만들었습니다.\n\n{out}\n\n이 파일을 카톡으로 보내주세요. (계정·비밀번호는 안 들어 있음)")
        if config.IS_WIN:
            try:
                subprocess.Popen(["explorer", "/select,", str(out)])
            except Exception:
                pass

    def approve():
        st = records.job_state("order")
        pend = [p["po_no"] for p in (st.get("pending") or [])]
        if not pend:
            return
        if not messagebox.askyesno("사방넷 봇", f"발주 {len(pend)}건을 출고요청할까요?\n\n" + "\n".join(pend)):
            return
        if not runner.start("출고요청", jobs.run_release, pend):
            messagebox.showinfo("사방넷 봇", "다른 작업이 실행 중입니다. 잠시 후 다시 눌러주세요.")
        refresh()

    # ------------------------------------------------------------ [상태] 탭
    pg = pages["상태"]
    inner = tk.Frame(pg, bg=BODY)
    inner.pack(fill="both", expand=True, padx=px(28), pady=px(24))
    top = tk.Frame(inner, bg=BODY)
    top.pack(fill="x")
    btn_run = RoundButton(top, "지금 발주 실행", run_now, size="lg")
    btn_run.pack(side="left")
    btn_inv = RoundButton(top, "지금 송장 전송", invoice_now, size="lg")
    btn_inv.pack(side="left", padx=(px(14), 0))
    btn_check = RoundButton(top, "점검 실행", check_now, size="lg")
    btn_check.pack(side="left", padx=px(14))
    RoundButton(top, "로그 폴더", open_logs, kind="outline", size="lg").pack(side="right")
    RoundButton(top, "로그 보내기", export_logs, kind="outline", size="lg").pack(side="right", padx=(0, px(10)))
    lbl_next = tk.Label(top, text="", bg=BODY, fg=TEXT, font=(FONT, 12))
    lbl_next.pack(side="right", padx=px(18))

    tbl = card(inner)
    tbl.pack(fill="x", pady=(px(22), 0))
    jobs_tv = ttk.Treeview(tbl, columns=("job", "time", "st", "res"), show="headings", height=2, selectmode="none")
    for c, t, w, anc in (("job", "작업", 170, "center"), ("time", "예정", 110, "center"),
                         ("st", "상태", 150, "center"), ("res", "결과", 460, "w")):
        jobs_tv.heading(c, text=t)
        jobs_tv.column(c, width=px(w), anchor=anc, stretch=(c == "res"))
    jobs_tv.tag_configure("done", foreground=GREEN)
    jobs_tv.tag_configure("failed", foreground=RED)
    jobs_tv.tag_configure("wait", foreground=ORANGE)
    jobs_tv.tag_configure("run", foreground=BLUE)
    jobs_tv.tag_configure("idle", foreground=MUTED)
    jobs_tv.pack(fill="x", padx=px(2), pady=px(2))

    appr = card(inner)
    appr_head = tk.Frame(appr, bg=WHITE)
    appr_head.pack(fill="x", padx=px(14), pady=(px(12), px(6)))
    label(appr_head, "출고요청 승인 대기", size=12, bold=True, color=ORANGE).pack(side="left")
    label(appr_head, "  처음 며칠만 확인하고, 괜찮으면 [설정]에서 '출고요청 전에 확인받기'를 끄면 완전 자동",
          color=MUTED).pack(side="left")
    RoundButton(appr_head, "출고요청 실행", approve, size="md", bg=WHITE).pack(side="right")
    appr_tv = ttk.Treeview(appr, columns=("po", "mall", "st"), show="headings", height=3, selectmode="none")
    for c, t, w in (("po", "발주번호", 200), ("mall", "쇼핑몰 주문번호", 260), ("st", "진행상태", 160)):
        appr_tv.heading(c, text=t)
        appr_tv.column(c, width=px(w), anchor="center")
    appr_tv.pack(fill="x", padx=px(2), pady=(0, px(2)))

    label(inner, "최근 기록", size=11, bold=True, bg=BODY).pack(anchor="w", pady=(px(20), px(6)))
    logbox = tk.Text(inner, height=8, wrap="word", relief="flat", bd=0, font=(FONT, 10), bg=WHITE, fg=TEXT,
                     highlightthickness=1, highlightbackground=TABLE_LINE, padx=px(12), pady=px(10))
    logbox.pack(fill="both", expand=True)
    lbl_info = tk.Label(inner, text="", bg=BODY, fg=MUTED, font=(FONT, 10), anchor="w", justify="left")
    lbl_info.pack(fill="x", pady=(px(8), 0))

    # ------------------------------------------------------------ [설정] 탭
    pg = pages["설정"]
    inner2 = tk.Frame(pg, bg=BODY)
    inner2.pack(fill="both", expand=True, padx=px(28), pady=px(24))
    form = card(inner2)
    form.pack(fill="x")
    f2 = tk.Frame(form, bg=WHITE)
    f2.pack(fill="x", padx=px(24), pady=px(20))
    s0 = config.load_settings()
    v = {
        "order_time": tk.StringVar(value=s0.order_time),
        "invoice_time": tk.StringVar(value=s0.invoice_time),
        "release_deadline": tk.StringVar(value=s0.release_deadline),
        "collect_days": tk.StringVar(value=str(s0.collect_days)),
        "max_orders_per_run": tk.StringVar(value=str(s0.max_orders_per_run)),
        "fulfill_url": tk.StringVar(value=s0.fulfill_url),
        "approve_before_release": tk.BooleanVar(value=s0.approve_before_release),
        "show_browser": tk.BooleanVar(value=s0.show_browser),
        "invoice_enabled": tk.BooleanVar(value=s0.invoice_enabled),
    }
    wd = [tk.BooleanVar(value=i in s0.weekdays) for i in range(7)]

    def frow(parent, r, text, widget, hint="", span=1, ipady=4):
        label(parent, text, width=14, anchor="w").grid(row=r, column=0, sticky="w", pady=px(8))
        widget.grid(row=r, column=1, columnspan=span, sticky="w", pady=px(8), ipady=px(ipady))
        if hint:
            label(parent, hint, color=MUTED).grid(row=r, column=2, sticky="w", padx=px(18))

    frow(f2, 0, "발주·출고요청", time_picker(f2, v["order_time"]), "매일 이 시각에 발주·출고요청", ipady=0)
    frow(f2, 1, "송장 전송", time_picker(f2, v["invoice_time"]), "매일 이 시각에 송장을 쇼핑몰로 전송", ipady=0)
    wdf = tk.Frame(f2, bg=WHITE)
    for i, n in enumerate(WEEK):
        CheckBox(wdf, n, wd[i]).pack(side="left", padx=(0, px(14)))
    frow(f2, 2, "실행 요일", wdf, span=2, ipady=0)
    frow(f2, 3, "3PL 출고 마감", time_picker(f2, v["release_deadline"]), "PC가 꺼져 이 시각을 넘기면 발주는 다음 평일에", ipady=0)
    frow(f2, 4, "주문수집 기간", combo(f2, v["collect_days"], [str(i) for i in range(1, 7)], 4),
         "일 (주말·연휴 주문까지 넉넉히)", ipady=0)
    frow(f2, 5, "한 번에 최대", combo(f2, v["max_orders_per_run"], ["30", "50", "100", "150", "200", "300"], 5),
         "건 (넘으면 이상 징후로 보고 멈춤)", ipady=0)
    frow(f2, 6, "풀필먼트 주소", entry(f2, v["fulfill_url"], 36), span=2)
    opt = tk.Frame(f2, bg=WHITE)
    opt.grid(row=7, column=0, columnspan=3, sticky="w", pady=(px(14), 0))
    CheckBox(opt, "송장 자동 전송", v["invoice_enabled"]).pack(anchor="w", pady=px(5))
    CheckBox(opt, "출고요청 전에 확인받기 (처음 며칠 권장)", v["approve_before_release"]).pack(anchor="w", pady=px(5))
    CheckBox(opt, "브라우저 창 보이기 (문제 확인할 때만)", v["show_browser"]).pack(anchor="w", pady=px(5))
    lbl_cut = label(inner2, "", color=MUTED, bg=BODY, justify="left", anchor="w")
    lbl_cut.pack(fill="x", pady=(px(12), 0))

    def save_set():
        try:
            for k in ("order_time", "invoice_time", "release_deadline"):
                h, m = v[k].get().strip().split(":")
                dt.time(int(h), int(m))
            days = int(v["collect_days"].get())
            mx = int(v["max_orders_per_run"].get())
        except Exception:
            messagebox.showerror("사방넷 봇", "설정값을 확인해주세요.")
            return
        s = config.load_settings()
        s.order_time = v["order_time"].get().strip()
        s.invoice_time = v["invoice_time"].get().strip()
        s.release_deadline = v["release_deadline"].get().strip()
        s.collect_days = max(1, min(6, days))
        s.max_orders_per_run = mx
        s.approve_before_release = v["approve_before_release"].get()
        s.show_browser = v["show_browser"].get()
        s.invoice_enabled = v["invoice_enabled"].get()
        s.fulfill_url = v["fulfill_url"].get().strip().rstrip("/")
        s.weekdays = [i for i in range(7) if wd[i].get()]
        config.save_settings(s)
        messagebox.showinfo("사방넷 봇", "저장했습니다. (노트북·회사PC 공용)")
        refresh()

    RoundButton(inner2, "저장", save_set, size="lg", width=px(160)).pack(anchor="e", pady=px(16))

    # ------------------------------------------------------------ [계정] 탭
    pg = pages["계정"]
    inner3 = tk.Frame(pg, bg=BODY)
    inner3.pack(fill="both", expand=True, padx=px(28), pady=px(24))
    sec0 = config.load_secrets()
    lc0 = config.load_local()
    a = {k: tk.StringVar(value=getattr(sec0, k)) for k in ("mini_id", "mini_pw", "mini_otp", "ff_company", "ff_id", "ff_pw")}
    prio = tk.IntVar(value=int(lc0.pc_priority))
    auto = tk.BooleanVar(value=autostart_enabled() if config.IS_WIN else lc0.autostart)
    cols = tk.Frame(inner3, bg=BODY)
    cols.pack(fill="x")
    c1 = card(cols)
    c1.pack(side="left", fill="both", expand=True, padx=(0, px(10)))
    c2 = card(cols)
    c2.pack(side="left", fill="both", expand=True, padx=(px(10), 0))
    g1 = tk.Frame(c1, bg=WHITE)
    g1.pack(fill="x", padx=px(22), pady=px(18))
    label(g1, "사방넷 미니", size=12, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(8)))
    frow(g1, 1, "아이디", entry(g1, a["mini_id"]))
    frow(g1, 2, "비밀번호", entry(g1, a["mini_pw"], show="●"))
    frow(g1, 3, "구글 OTP 키", entry(g1, a["mini_otp"], show="●"))
    label(g1, "OTP 키를 넣으면 2단계 인증을 봇이 자동 통과", color=MUTED).grid(row=4, column=0, columnspan=2, sticky="w")
    g2 = tk.Frame(c2, bg=WHITE)
    g2.pack(fill="x", padx=px(22), pady=px(18))
    label(g2, "사방넷 풀필먼트", size=12, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(8)))
    frow(g2, 1, "회사코드", entry(g2, a["ff_company"]))
    frow(g2, 2, "아이디", entry(g2, a["ff_id"]))
    frow(g2, 3, "비밀번호", entry(g2, a["ff_pw"], show="●"))
    c3 = card(inner3)
    c3.pack(fill="x", pady=(px(18), 0))
    g3 = tk.Frame(c3, bg=WHITE)
    g3.pack(fill="x", padx=px(22), pady=px(16))
    label(g3, "이 PC", size=12, bold=True).pack(anchor="w", pady=(0, px(6)))
    RadioBox(g3, "1순위 — 정시에 실행 (주로 켜두는 PC)", prio, 1).pack(anchor="w", pady=px(4))
    RadioBox(g3, "2순위 — 5분 늦게, 다른 PC가 안 했을 때만", prio, 2).pack(anchor="w", pady=px(4))
    CheckBox(g3, "윈도우 켜지면 자동 실행 (오른쪽 아래 아이콘으로 상주)", auto).pack(anchor="w", pady=(px(10), 0))
    label(inner3, "계정은 이 PC 안에만 암호화해서 저장합니다 (원드라이브에 안 올라감).", color=MUTED, bg=BODY).pack(anchor="w", pady=(px(10), 0))

    def save_acct():
        sec = config.Secrets(**{k: a[k].get().strip() for k in a})
        config.save_secrets(sec)
        lc = config.load_local()
        lc.pc_priority = prio.get()
        config.save_local(lc)
        try:
            set_autostart(auto.get())
        except Exception as e:
            messagebox.showwarning("사방넷 봇", f"자동 실행 등록 실패: {e}")
        messagebox.showinfo("사방넷 봇", "저장했습니다." + ("\n이제 윈도우가 켜지면 봇이 알아서 시작됩니다." if auto.get() else ""))
        refresh()

    RoundButton(inner3, "저장", save_acct, size="lg", width=px(160)).pack(anchor="e", pady=px(16))

    # ------------------------------------------------------------ 갱신
    ST = {"running": ("실행 중", "run"), "done": ("완료", "done"), "failed": ("실패", "failed"),
          "awaiting_approval": ("승인 대기", "wait"), "skipped": ("건너뜀", "idle")}

    def job_row(name, t, st, planned, enabled=True):
        if not enabled:
            return (name, t, "준비 중", "2차 버전에서 켜짐"), "idle"
        if not st:
            return (name, t, "예정" if planned else "쉬는 날", ""), "idle"
        text, tag = ST.get(st.get("status", ""), (st.get("status", ""), "idle"))
        when = (st.get("finished") or st.get("updated") or "")[11:16]
        who = st.get("pc", "")
        res = st.get("summary") or st.get("error") or ""
        return (name, t, f"{text} {when}".strip(), f"{res}  ({who})" if who else res), tag

    def stt_early(st: dict, t: dt.datetime) -> bool:
        return st.get("status") in ("done", "failed") and early_run(st, t)

    def next_run_text(s: config.Settings) -> str:
        now = dt.datetime.now()
        wds = [int(x) for x in s.weekdays]
        if not wds:
            return "실행 요일 없음"
        st = records.job_state("order")
        for add in range(0, 8):
            d = dt.date.today() + dt.timedelta(days=add)
            if d.weekday() not in wds:
                continue
            h, m = s.order_time.split(":")
            t = dt.datetime.combine(d, dt.time(int(h), int(m)))
            if add == 0 and stt_early(st, t) and not order_deadline_passed(s, now):
                return f"다음 실행  오늘({WEEK[d.weekday()]}) {s.order_time}" if now < t else "오늘 정시 작업 — 곧 실행"
            if add == 0 and now > t:
                stt = st.get("status")
                if stt in ("done", "awaiting_approval", "skipped") or order_deadline_passed(s, now) or (
                        stt == "failed" and int(st.get("attempts", 0)) >= int(s.max_attempts)):
                    continue
                return "오늘 밀린 작업 — 곧 실행"
            label_day = "오늘" if add == 0 else ("내일" if add == 1 else f"{d.month}/{d.day}")
            return f"다음 실행  {label_day}({WEEK[d.weekday()]}) {s.order_time}"
        return ""

    def refresh():
        s = config.load_settings()
        today = dt.date.today()
        planned = today.weekday() in [int(x) for x in s.weekdays]
        busy = runner.busy
        lbl_next.configure(text=(f"실행 중: {runner.current} …" if busy else next_run_text(s)),
                           fg=BLUE if busy else TEXT)
        btn_run.set_enabled(not busy)
        btn_check.set_enabled(not busy)
        btn_inv.set_enabled(not busy)
        jobs_tv.delete(*jobs_tv.get_children())
        vals, tag = job_row("발주 · 출고요청", s.order_time, records.job_state("order"), planned)
        jobs_tv.insert("", "end", values=vals, tags=(tag,))
        vals, tag = job_row("송장 전송", s.invoice_time, records.job_state("invoice"), planned, s.invoice_enabled)
        jobs_tv.insert("", "end", values=vals, tags=(tag,))
        st = records.job_state("order")
        pend = st.get("pending") or []
        if st.get("status") == "awaiting_approval" and pend:
            appr_tv.delete(*appr_tv.get_children())
            for p in pend:
                appr_tv.insert("", "end", values=(p.get("po_no"), p.get("mall_ord_no"), p.get("status")))
            if not appr.winfo_ismapped():
                appr.pack(fill="x", pady=(px(18), 0), after=tbl)
        elif appr.winfo_ismapped():
            appr.pack_forget()
        # 최근 기록
        try:
            raw = (config.local_dir() / "app.log").read_text(encoding="utf-8").splitlines()[-300:]
        except Exception:
            raw = []
        lines = []
        for ln in raw:
            m = re.match(r"\d{4}-(\d{2})-(\d{2}) (\d{2}:\d{2}):\d{2},\d+ (\w+) (.*)", ln)
            if not m or "args=" in m.group(5) or m.group(4) == "ERROR" or m.group(5).startswith("Traceback"):
                continue
            mark = "⚠ " if m.group(4) == "WARNING" else ""
            lines.append(f"{int(m.group(1))}/{int(m.group(2))} {m.group(3)}   {mark}{m.group(5)}")
        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.insert("end", "\n".join(lines[-60:]))
        logbox.see("end")
        logbox.configure(state="disabled")
        cut = jobs.cutover_dt(s)
        lc = config.load_local()
        auto_on = autostart_enabled() if config.IS_WIN else lc.autostart
        info = (f"{cut.month}/{cut.day} {cut:%H:%M} 이후 들어온 주문부터 봇이 처리  ·  이 PC {lc.pc_priority}순위  ·  "
                f"자동 실행 {'켜짐' if auto_on else '꺼짐'}  ·  {config.pc_name()}")
        lbl_info.configure(text=info)
        lbl_cut.configure(text=f"자동화 시작: {cut:%Y-%m-%d %H:%M} (봇을 처음 켠 시점) — 이전에 들어온 주문은 봇이 건드리지 않습니다.")

    def poll():
        if show_flag().exists():
            try:
                show_flag().unlink()
            except OSError:
                pass
            show_window()
        try:
            refresh()
        except Exception:
            log.warning(traceback.format_exc())
        root.after(5000, poll)

    # ------------------------------------------------------------ 창/트레이
    def show_window(tab: str | None = None):
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        if tab:
            select_tab(tab)

    def hide_window():
        if state["tray"] is not None:
            root.withdraw()
        else:
            quit_app()

    def quit_app(restart_with: Path | None = None):
        if runner.busy and restart_with is None:
            if not messagebox.askyesno("사방넷 봇", f"'{runner.current}' 작업이 실행 중입니다. 그래도 끌까요?"):
                return
        sched.stop.set()
        if state["tray"] is not None:
            try:
                state["tray"].stop()
            except Exception:
                pass
        release_single_instance()
        if restart_with is not None:
            lc = config.load_local()
            _spawn(restart_with, ["--tray", "--updated"] + (["--src", lc.src_exe] if lc.src_exe else []))
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", hide_window)

    def start_tray():
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("창 열기", lambda *_: ui(show_window), default=True),
                pystray.MenuItem("지금 발주 실행", lambda *_: ui(run_now)),
                pystray.MenuItem("지금 송장 전송", lambda *_: ui(invoice_now)),
                pystray.MenuItem("점검 실행", lambda *_: ui(check_now)),
                pystray.MenuItem("로그 폴더", lambda *_: ui(open_logs)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("종료", lambda *_: ui(quit_app)),
            )
            icon = pystray.Icon(config.APP_NAME, notify.icon_image(64), "멀티파이프 사방넷 발주 자동화", menu)
            icon.run_detached()
            state["tray"] = icon
        except Exception:
            log.warning("트레이 아이콘 실패: " + traceback.format_exc())

    # 점검은 [점검 실행] 버튼을 눌렀을 때만 돈다. (업데이트 뒤 자동 점검 없음 — v0.3.2)
    # 봇이 스스로 로그인하는 건 정해진 시각의 발주·송장 작업뿐.
    jobs.ensure_cutover()
    select_tab("상태")
    start_tray()
    sched.start()
    refresh()
    root.after(5000, poll)
    root.after(3000, missed_days_notice)
    root.after(8000, cleanup_old_bins)
    root.after(10000, lambda: records.cleanup_logs(14))
    sec_ok = config.load_secrets()
    if start_hidden and sec_ok.mini_ready() and state["tray"] is not None:
        root.withdraw()
    elif not sec_ok.mini_ready():
        show_window("계정")
    if os.environ.get("SBBOT_TAB"):
        select_tab(os.environ["SBBOT_TAB"])
    root.mainloop()
