"""크롬 제어(Playwright) 공통 도구.

- 사장님 PC에 깔린 크롬(없으면 엣지)을 봇 전용 프로필로 띄운다 → 평소 쓰는 크롬과 안 섞임
- 창이 가려져도 멈추지 않게 하는 옵션, 로컬 사방넷 클라이언트(8181) 접근 허용 옵션 포함
- 단계마다 화면 캡처/HTML/텍스트를 로그 폴더에 남긴다
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from . import config
from .records import RunLog

CHROME_ARGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion,LocalNetworkAccessChecks,"
    "LocalNetworkAccessChecksWebSockets,BlockInsecurePrivateNetworkRequests,"
    "PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults",
    "--no-first-run",
    "--no-default-browser-check",
    "--lang=ko-KR",
    "--window-size=1500,950",
]


class StepError(Exception):
    """작업 단계 실패 (사람이 봐야 하는 문제)."""


class ConfirmOK:
    """with ConfirmOK(b): 이 구간에서만 브라우저 확인창에 '확인'."""

    def __init__(self, b: "Browser"):
        self.b = b

    def __enter__(self):
        self.b.allow_confirm = True

    def __exit__(self, *a):
        self.b.allow_confirm = False


class Browser:
    def __init__(self, run: RunLog, headless: bool = True):
        self.run = run
        self.headless = headless
        self.pw = None
        self.ctx = None
        self.dialogs: list[str] = []
        self.allow_confirm = False  # 브라우저 기본 확인창(confirm)은 허락된 단계에서만 '확인'

    # -------------------------------------------------- 시작/종료
    def __enter__(self) -> "Browser":
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.ctx = self._launch()
        self.ctx.set_default_timeout(20_000)
        self.ctx.on("page", self._attach)
        for p in self.ctx.pages:
            self._attach(p)
        return self

    def __exit__(self, *a):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass

    def _launch_once(self, channel: str, ua: str | None):
        kw = dict(
            user_data_dir=str(config.chrome_profile_dir()),
            channel=channel,
            headless=self.headless,
            args=CHROME_ARGS,
            viewport={"width": 1480, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            accept_downloads=True,
            ignore_default_args=["--enable-automation"],
        )
        if ua:
            kw["user_agent"] = ua
        return self.pw.chromium.launch_persistent_context(**kw)

    def _launch(self):
        last = None
        for channel in ("chrome", "msedge", None):
            try:
                ctx = self._launch_once(channel, None)
                if self.headless:
                    # 헤드리스 표시(HeadlessChrome)를 일반 크롬처럼 보이게
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    ua = page.evaluate("navigator.userAgent")
                    if "Headless" in ua:
                        ctx.close()
                        ctx = self._launch_once(channel, ua.replace("HeadlessChrome", "Chrome"))
                self.run.info(f"브라우저 시작: {channel or 'chromium'} ({'숨김' if self.headless else '창 보임'})")
                self._grant_local_network(ctx)
                return ctx
            except Exception as e:
                last = e
                self.run.warn(f"{channel or 'chromium'} 실행 실패: {str(e).splitlines()[0][:200]}")
        raise StepError(f"크롬/엣지를 실행할 수 없음: {last}")

    def _grant_local_network(self, ctx) -> None:
        """미니 관리화면이 PC 안의 사방넷 클라이언트(127.0.0.1:8181)를 부를 수 있게 허용.
        서버 번호(sbadmin01~)는 계정마다 다를 수 있어 01~20을 미리 허용하고, 로그인 후 실제 주소로 한 번 더."""
        origins = [f"https://sbadmin{n:02d}.sbmini.co.kr" for n in range(1, 21)] + ["https://www.sbmini.co.kr"]
        self.grant_local_network(origins, ctx)

    def grant_local_network(self, origins: list[str], ctx=None) -> None:
        ctx = ctx or self.ctx
        if ctx is None:
            return
        for name in ("local-network-access", "local-network", "loopback-network"):
            for o in origins:
                try:
                    ctx.grant_permissions([name], origin=o)
                except Exception:
                    pass
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            for name in ("local-network-access", "local-network", "loopback-network"):
                for o in origins:
                    try:
                        cdp.send("Browser.setPermission", {"permission": {"name": name}, "setting": "granted", "origin": o})
                    except Exception:
                        pass
            cdp.detach()
        except Exception:
            pass

    def _attach(self, page) -> None:
        def on_dialog(d):
            ok = d.type in ("alert", "beforeunload") or (d.type == "confirm" and self.allow_confirm)
            msg = f"[{d.type}] {d.message}"
            self.dialogs.append(msg)
            self.run.info(f"창 알림: {msg[:200]} → {'확인' if ok else '취소'}")
            try:
                d.accept() if ok else d.dismiss()
            except Exception:
                pass
        page.on("dialog", on_dialog)

    # -------------------------------------------------- 페이지
    def page(self):
        if self.ctx.pages:
            return self.ctx.pages[0]
        return self.ctx.new_page()

    def pages_matching(self, pattern: str) -> list:
        return [p for p in self.ctx.pages if re.search(pattern, p.url or "")]

    # -------------------------------------------------- 기록
    def snap(self, page, name: str, full: bool = True) -> None:
        base = self.run.next_name(name)
        try:
            page.screenshot(path=str(base.with_suffix(".png")), full_page=full, timeout=15_000)
        except Exception as e:
            self.run.warn(f"캡처 실패({name}): {str(e)[:120]}")
        try:
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            txt = page.evaluate("document.body ? document.body.innerText : ''")
            base.with_suffix(".txt").write_text(f"URL: {page.url}\n\n{txt}", encoding="utf-8")
        except Exception:
            pass


# ------------------------------------------------------------------ 공통 동작
def sleep(sec: float) -> None:
    time.sleep(sec)


def body_text(page) -> str:
    try:
        return page.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def wait_text(page, text: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if text in body_text(page):
            return True
        time.sleep(0.5)
    return False


def visible_buttons(scope, texts: Iterable[str], exact: bool = True):
    """보이는 버튼 중 글자가 맞는 것들 (순서대로)."""
    out = []
    for t in texts:
        if exact:
            loc = scope.locator("button, a.el-button, .v-btn").filter(has_text=re.compile(rf"^\s*{re.escape(t)}\s*$"))
        else:
            loc = scope.locator("button, a.el-button, .v-btn").filter(has_text=t)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    out.append(el)
            except Exception:
                pass
    return out


def click_button(scope, texts: Iterable[str], exact: bool = True, timeout: float = 10, what: str = "") -> str:
    """보이는 버튼을 글자로 찾아 클릭. 누른 버튼 글자를 돌려준다."""
    texts = list(texts)
    end = time.time() + timeout
    while time.time() < end:
        for t in texts:
            btns = visible_buttons(scope, [t], exact=exact)
            if btns:
                btns[0].click()
                return t
        time.sleep(0.4)
    raise StepError(f"버튼을 못 찾음: {what or texts}")


# ---- Element UI (사방넷 미니)
def el_messagebox(page, accept: bool = True, timeout: float = 6) -> str | None:
    """Element 확인창(MessageBox)이 뜨면 확인/취소를 누르고 메시지를 돌려준다."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if page.is_closed():
                return None
            box = page.locator(".el-message-box__wrapper").filter(has=page.locator(".el-message-box"))
            cnt = box.count()
        except Exception:
            return None
        for i in range(cnt):
            b = box.nth(i)
            try:
                if not b.is_visible():
                    continue
                msg = b.locator(".el-message-box__message").inner_text(timeout=2000).strip()
                if accept:
                    btn = b.locator(".el-message-box__btns button.el-button--primary")
                    if btn.count() == 0:
                        btn = b.locator(".el-message-box__btns button").last
                else:
                    # 취소가 있으면 취소, 버튼이 하나뿐인 안내창이면 그 버튼(확인)
                    all_btns = b.locator(".el-message-box__btns button:visible")
                    btn = b.locator(".el-message-box__btns button:visible").filter(has_text="취소")
                    if btn.count() == 0:
                        if all_btns.count() == 1:
                            btn = all_btns
                        else:
                            btn = b.locator(".el-message-box__headerbtn")
                btn.first.click()
                time.sleep(0.6)
                return msg
            except Exception:
                continue
        time.sleep(0.3)
    return None


def el_toasts(page) -> list[str]:
    try:
        if page.is_closed():
            return []
        return [t.strip() for t in page.locator(".el-message, .el-notification").all_inner_texts() if t.strip()]
    except Exception:
        return []


def close_el_dialogs(page) -> None:
    """공지 팝업 등 떠 있는 Element 다이얼로그 닫기."""
    try:
        btns = page.locator(".el-dialog__wrapper .el-dialog__headerbtn")
        for i in range(btns.count()):
            b = btns.nth(i)
            if b.is_visible():
                b.click()
                time.sleep(0.3)
    except Exception:
        pass


# ---- Vuetify (사방넷 풀필먼트)
def v_dialogs(page):
    loc = page.locator(".v-overlay--active .v-overlay__content, .v-dialog--active, [role=dialog]")
    return [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]


def wait_v_dialog(page, timeout: float = 8):
    end = time.time() + timeout
    while time.time() < end:
        ds = v_dialogs(page)
        if ds:
            return ds[-1]
        time.sleep(0.3)
    return None
