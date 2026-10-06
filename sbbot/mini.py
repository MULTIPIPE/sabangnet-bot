"""사방넷 미니 (sbadminNN.sbmini.co.kr — 서버 번호는 로그인 후 자동 인식) 작업.

순서: 로그인(2단계 인증=구글 OTP 자동) → 주문수집 → 주문확정 → 주문상태 '주문확인'
화면 버튼을 사람처럼 누르되, 표 데이터는 화면 뒤의 값을 직접 읽어 정확하게 판단한다.
"""
from __future__ import annotations

import datetime as dt
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .browser import (Browser, ConfirmOK, StepError, body_text, click_button, close_el_dialogs,
                      el_messagebox, el_toasts, v_dialogs, visible_buttons, wait_text)

# 화면 뒤 Vue 페이지 객체
JS_VM = """(() => { const r=document.querySelector('#app'); if(!r||!r.__vue__) return null;
  const m=r.__vue__.$route.matched; return m.length? m[m.length-1].instances.default : null; })()"""


@dataclass
class MiniOrder:
    ord_no: str          # 사방넷 주문번호
    mall_ord_no: str     # 쇼핑몰 주문번호
    mall: str            # 쇼핑몰 코드(shop0055 등)
    login_id: str
    collected: str       # 수집일시 ISO
    prd_no: str
    sku_no: str
    decided: str         # 확정여부 Y/N
    status: str          # 주문상태 코드 (001=신규주문 ...)
    product: str = ""
    ordered: str = ""    # 쇼핑몰 주문(결제)일시 — 있으면 시작 시점 판단에 이걸 씀
    ordered_key: str = ""

    @property
    def collected_date(self) -> dt.date | None:
        try:
            return dt.date.fromisoformat(self.collected[:10])
        except Exception:
            return None

    @property
    def when(self) -> dt.datetime | None:
        """시작 시점과 비교할 시각: 주문일시 우선, 없으면 수집일시."""
        for v in (self.ordered, self.collected):
            d = parse_dt(v)
            if d:
                return d
        return None

    def after(self, cut: dt.datetime) -> bool:
        w = self.when
        return bool(w and w >= cut)

    @property
    def mapped(self) -> bool:
        return bool(re.fullmatch(r"\d+", self.prd_no or "")) and bool(re.fullmatch(r"\d{4}", self.sku_no or ""))

    def label(self) -> str:
        return f"{self.mall_ord_no}({self.product[:18]})" if self.product else self.mall_ord_no


# ------------------------------------------------------------------ 공통
def goto(page, s: config.Settings, route: str, wait_for: str | None = None) -> None:
    close_vm_modals(page)
    page.goto(f"{s.mini_admin_url}/#/{route}", wait_until="domcontentloaded")
    time.sleep(2.5)
    if wait_for and not wait_text(page, wait_for, 25):
        raise StepError(f"미니 화면이 안 열림: {route}")
    close_el_dialogs(page)
    close_vm_modals(page)


def _visible_vm(page, text: str | None = None):
    """화면 안 모달(vue-js-modal, .vm--modal) 중 보이는 것. text가 있으면 그 글자를 포함한 것만."""
    try:
        loc = page.locator(".vm--modal")
        if text:
            loc = loc.filter(has_text=text)
        vis = [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]
        return vis[-1] if vis else None
    except Exception:
        return None


def close_vm_modals(page) -> None:
    """떠 있는 화면 안 모달을 [닫기]로 모두 닫음 (10/6: 일괄주문확정 결과창이 남아 다음 클릭을 막았음)."""
    for _ in range(8):
        m = _visible_vm(page)
        if m is None:
            return
        try:
            btn = m.locator("button").filter(has_text=re.compile(r"^\s*닫기\s*$"))
            if btn.count():
                btn.last.click(timeout=3000)
            else:
                page.keyboard.press("Escape")
        except Exception:
            page.keyboard.press("Escape")
        time.sleep(0.7)


def run_bulk_modal(b: Browser, page, title: str, n: int) -> str:
    """[일괄주문확정]·[일괄단품매핑] 같은 버튼을 누른 뒤 뜨는 모달 처리.
    실제 화면(10/6): '선택할 데이터 ○선택된 데이터 : N건 ○검색된 데이터 : M건' + [제목(실행)] [닫기]
    → 실행 후 같은 자리에 '[성공] 주문번호 … 총 N 건이 처리되었습니다. [닫기]'. 중간에 확인창이 뜨면 확인.
    선택된 건수가 n과 다르면 실행하지 않음."""
    end = time.time() + 15
    m = None
    while time.time() < end:
        msg = el_messagebox(page, accept=True, timeout=0.5)
        if msg:
            b.run.info(f"{title} 확인창: {msg[:80]}")
        m = _visible_vm(page, "선택된 데이터")
        if m is not None:
            break
        time.sleep(0.3)
    if m is None:
        b.snap(page, f"mini_{title}_no_modal")
        return ""
    txt = re.sub(r"\s+", "", m.inner_text())
    if f"선택된데이터:{n}건" not in txt:
        b.snap(page, f"mini_{title}_count_mismatch")
        close_vm_modals(page)
        raise StepError(f"{title} 창의 선택 건수가 다름 (대상 {n}) — {txt[:80]}")
    r = m.locator(".el-radio").filter(has_text="선택된 데이터")
    if r.count() and "is-checked" not in (r.first.get_attribute("class") or ""):
        r.first.click()
        time.sleep(0.3)
    b.snap(page, f"mini_{title}_modal")
    m.locator("button").filter(has_text=re.compile(rf"^\s*{title}\s*(실행)?\s*$")).first.click()
    res = ""
    end = time.time() + 90
    while time.time() < end:
        msg = el_messagebox(page, accept=True, timeout=0.5)
        if msg:
            b.run.info(f"{title} 확인창: {msg[:80]}")
        mm = _visible_vm(page, "처리되었습니다")
        if mm is not None:
            res = re.sub(r"\s+", " ", mm.inner_text()).strip()
            break
        time.sleep(0.5)
    b.snap(page, f"mini_{title}_result")
    ok = res.count("[성공]") + res.count("(성공)")
    bad = res.count("[실패]") + res.count("(실패)")
    b.run.info(f"{title} 결과: 성공 {ok} / 실패 {bad} — {res[:200]}")
    if bad:
        b.run.warn(f"{title} 실패 있음: {res[:300]}")
    close_vm_modals(page)
    return res


_ADMIN_HOST = "sbadmin01.sbmini.co.kr"
_ADMIN_RE = re.compile(r"https?://(sbadmin\d+\.sbmini\.co\.kr)", re.I)


def _admin_host_of(url: str) -> str:
    m = _ADMIN_RE.match(url or "")
    return m.group(1).lower() if m else ""


def is_admin(page) -> bool:
    """미니 관리화면(sbadminNN.sbmini.co.kr)인지. 계정마다 서버 번호가 다를 수 있어 숫자는 아무거나."""
    try:
        return bool(_admin_host_of(page.url)) and page.locator(".app-main").count() > 0
    except Exception:
        return False


def _adopt_admin(b: Browser, s: config.Settings, page) -> None:
    """실제 관리화면 주소를 설정에 반영 (수강생 배포: sbadmin01 고정 금지)."""
    global _ADMIN_HOST
    host = _admin_host_of(page.url)
    if not host:
        return
    url = f"https://{host}"
    if host != _ADMIN_HOST or s.mini_admin_url.rstrip("/") != url:
        _ADMIN_HOST = host
        if s.mini_admin_url.rstrip("/") != url:
            b.run.info(f"미니 관리화면 주소: {url} (설정 갱신)")
            s.mini_admin_url = url
            try:
                config.save_settings(s)
            except Exception:
                pass
    b.grant_local_network([url])


def table_data(page) -> list[dict]:
    """현재 화면 표의 데이터(화면 뒤 값)."""
    data = page.evaluate(f"""() => {{
      const vm = {JS_VM}; if(!vm) return null;
      const d = vm.$data || {{}};
      let rows = d.tableData;
      if(!Array.isArray(rows)) {{ const k = Object.keys(d).find(k=>Array.isArray(d[k]) && d[k].length && typeof d[k][0]==='object'); rows = k? d[k] : null; }}
      if(!Array.isArray(rows) || !rows.length) {{
        // 주문서확인처리 화면(10/6 확인): 화면 객체엔 목록이 없고 표(el-table)에만 있음
        const t = [...document.querySelectorAll('.app-main .el-table')].map(e=>e.__vue__).find(t=>t && Array.isArray(t.data));
        rows = t ? t.data : (rows || []);
      }}
      return rows.map(r => {{ const o={{}}; for(const [k,v] of Object.entries(r)) {{ if(v===null||typeof v!=='object') o[k]=v; }} return o; }});
    }}""")
    return data or []


def parse_dt(v) -> dt.datetime | None:
    if v in (None, ""):
        return None
    t = str(v).strip()
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})(?:[ T]?(\d{2}):?(\d{2})(?::?(\d{2}))?)?", t)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None


_ORDER_DT_KEYS = ("ordDt", "shmaOrdDt", "ordDttm", "shmaOrdDttm", "pymtDt", "pymtDttm", "payDt", "ordrDt", "ordDate")
_ORDER_DT_RE = re.compile(r"^(shma)?(ord|pymt|pay)[A-Za-z]*(Dt|Dttm|Date)$", re.I)


def _order_dt(r: dict) -> tuple[str, str]:
    for k in _ORDER_DT_KEYS:
        if parse_dt(r.get(k)):
            return str(r[k]), k
    for k, v in r.items():
        if _ORDER_DT_RE.match(k) and k not in ("ordDcdDt",) and parse_dt(v):
            return str(v), k
    return "", ""


def to_order(r: dict) -> MiniOrder:
    g = lambda *ks: next((str(r[k]) for k in ks if r.get(k) not in (None, "")), "")
    od, ok = _order_dt(r)
    return MiniOrder(
        ordered=od, ordered_key=ok,
        ord_no=g("ordNo"), mall_ord_no=g("shmaOrdNo"), mall=g("shmaId"),
        login_id=g("shmaCnctnLoginId"), collected=g("fstRegsDt", "clctDt", "regsDt"),
        prd_no=g("prdNo"), sku_no=g("skuNo"), decided=g("ordDcdYn"), status=g("ordStsCd"),
        product=g("clctPrdNm", "dcdPrdNm", "prdNm", "shmaPrdNm", "ordClctFldVl1"),
    )


def set_dates(page, start: dt.date, end: dt.date) -> bool:
    """검색 기간(시작/끝) 설정. 날짜 입력칸 컴포넌트에 직접 값 전달."""
    ok = page.evaluate("""([a,b]) => {
      const pick = [...document.querySelectorAll('.app-main .el-date-editor')].map(e=>e.__vue__).filter(Boolean);
      const comps = pick.map(c => c.$options.name==='ElDatePicker'? c : (c.$parent && c.$parent.$options.name==='ElDatePicker'? c.$parent : c));
      if(comps.length < 2) return false;
      comps[0].$emit('input', a); comps[0].$emit('change', a);
      comps[1].$emit('input', b); comps[1].$emit('change', b);
      return true; }""", [start.strftime("%Y%m%d"), end.strftime("%Y%m%d")])
    time.sleep(0.5)
    vals = page.evaluate("[...document.querySelectorAll('.app-main .el-date-editor input')].map(i=>i.value)")
    return bool(ok) and len(vals) >= 2 and vals[0] == start.strftime("%Y%m%d") and vals[1] == end.strftime("%Y%m%d")


def select_rows(page, ord_nos: list[str]) -> int:
    """표에서 해당 주문만 체크 (나머지는 해제). 체크된 개수를 돌려준다."""
    n = page.evaluate("""(nos) => {
      const tables=[...document.querySelectorAll('.app-main .el-table')].map(e=>e.__vue__).filter(t=>t && t.toggleRowSelection && Array.isArray(t.data));
      let cnt=0;
      for(const t of tables){ t.clearSelection(); for(const row of t.data){ if(nos.includes(String(row.ordNo))){ t.toggleRowSelection(row,true); cnt++; } } }
      return cnt; }""", ord_nos)
    time.sleep(0.8)
    return n


def selected_count(page) -> int:
    return page.evaluate("""() => { const t=[...document.querySelectorAll('.app-main .el-table')].map(e=>e.__vue__).find(t=>t && t.store);
      return t ? (t.store.states.selection||[]).length : -1; }""")


# ------------------------------------------------------------------ 로그인
def _otp_code(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(secret.replace(" ", "").upper()).now()


_B32 = re.compile(r"(?<![A-Z2-7])((?:[A-Z2-7]{4}[ -]?){4,10})(?![A-Z2-7])")


def _secret_from_uri(uri: str) -> str | None:
    m = re.search(r"[?&]secret=([A-Za-z2-7=]+)", uri)
    return m.group(1).upper().rstrip("=") if m else None


def find_otp_secret(b: Browser, page) -> str | None:
    """구글 OTP 등록 화면에서 비밀키 찾기: otpauth 주소 → 글자 키 → QR 이미지 해독 순."""
    try:
        html = page.content()
    except Exception:
        html = ""
    m = re.search(r"otpauth://totp/[^\"'<>\s]+", html)
    if m:
        sec = _secret_from_uri(m.group(0).replace("&amp;", "&"))
        if sec:
            b.run.info("OTP 등록 키 발견(주소)")
            return sec
    text = body_text(page)
    vals = []
    try:
        vals = page.evaluate("[...document.querySelectorAll('input')].map(i=>i.value||'')")
    except Exception:
        pass
    for chunk in [text] + list(vals):
        for g in _B32.findall(chunk.upper()):
            k = re.sub(r"[ -]", "", g)
            if 16 <= len(k) <= 40 and not k.isdigit():
                b.run.info("OTP 등록 키 발견(글자)")
                return k
    # QR 이미지 해독
    try:
        import io
        import zxingcpp
        from PIL import Image
        shot = page.screenshot(full_page=True)
        for r in zxingcpp.read_barcodes(Image.open(io.BytesIO(shot))):
            if "otpauth://" in (r.text or ""):
                sec = _secret_from_uri(r.text)
                if sec:
                    b.run.info("OTP 등록 키 발견(QR)")
                    return sec
    except Exception as e:
        b.run.warn(f"QR 해독 실패: {str(e)[:80]}")
    return None


def _tab(page, pattern: str) -> bool:
    loc = page.locator("button").filter(has_text=re.compile(pattern))
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if el.is_visible():
                el.click()
                time.sleep(1.5)
                return True
        except Exception:
            pass
    return False


def _active_dialog(page):
    """열려 있는 대화창(Vuetify/Element/부트스트랩) 중 맨 위."""
    try:
        ds = v_dialogs(page)
        if ds:
            return ds[-1]
    except Exception:
        pass
    for sel in (".el-dialog__wrapper .el-dialog", ".modal.show .modal-content"):
        try:
            loc = page.locator(sel).filter(visible=True)
            if loc.count():
                return loc.last
        except Exception:
            pass
    return None


def _has_qr(scope) -> bool:
    try:
        return scope.locator("img, canvas").count() > 0
    except Exception:
        return True


def _code_inputs(scope) -> list:
    """OTP 6자리 입력칸. 한 칸짜리 6개로 나뉜 경우도 돌려줌."""
    try:
        inputs = scope.locator("input:visible")
        n = inputs.count()
    except Exception:
        return []
    singles, best, good = [], None, None
    for i in range(n):
        el = inputs.nth(i)
        try:
            typ = (el.get_attribute("type") or "text").lower()
            if typ not in ("text", "tel", "number", "password"):
                continue
            ph = el.get_attribute("placeholder") or ""
            if "아이디" in ph or "비밀번호" in ph:
                continue
            ml = el.get_attribute("maxlength") or ""
            if ml == "1":
                singles.append(el)
                continue
            if good is None and ("인증" in ph or "OTP" in ph.upper() or "번호" in ph or ml == "6"):
                good = el
            if best is None:
                best = el
        except Exception:
            continue
    if len(singles) >= 6:
        return singles[:6]
    t = good or best
    return [t] if t is not None else []


def _fill_code(inputs: list, code: str) -> None:
    if len(inputs) >= 6:
        for el, ch in zip(inputs, code):
            el.fill(ch)
            time.sleep(0.1)
    else:
        inputs[0].fill(code)
    time.sleep(0.3)


def _on_2fa(p) -> bool:
    try:
        if p.is_closed():
            return False
        if "/auth/" in (p.url or "") and "sbmini" in (p.url or ""):
            return True
        return bool(re.search(r"2단계 인증|Google OTP|OTP 등록", body_text(p)))
    except Exception:
        return False


def _otp_pages(b: Browser, page=None) -> list:
    """2단계 인증 화면인 창들 (가장 최근 것이 마지막)."""
    return [p for p in list(b.ctx.pages) if not _admin_host_of(p.url) and _on_2fa(p)]


def _save_otp(b: Browser, sec: config.Secrets, key: str) -> None:
    sec.mini_otp = key
    cur = config.load_secrets()
    cur.mini_otp = key
    config.save_secrets(cur)
    b.run.info("구글 OTP 키를 이 PC에 암호화 저장")


def _otp_flow(b: Browser, page, sec: config.Secrets) -> bool:
    """Google OTP 탭으로 인증. OTP 키가 없으면 등록 화면(인증하기 → QR/키)에서 키를 찾아 저장하고 등록까지.
    키를 못 찾으면 등록 버튼은 절대 누르지 않고 False."""
    _tab(page, r"^\s*Google OTP\s*$")
    b.snap(page, "mini_2fa_otp_tab")
    tried_codes = 0
    for step in range(8):
        if any(is_admin(p) for p in b.ctx.pages):
            return True
        pages = _otp_pages(b, page)
        if not pages:
            return True  # 인증 화면을 벗어남 → 로그인 흐름이 이어서 확인
        cur = pages[-1]
        msg = el_messagebox(cur, accept=True, timeout=1)
        if msg:
            b.run.info(f"OTP 화면 안내창: {msg[:100]}")
        dlg = _active_dialog(cur)
        scope = dlg or cur
        registering = bool(re.search(r"OTP\s*등록|Authenticator\s*설치", body_text(cur)))
        if registering or not sec.mini_otp:
            # 등록 화면이면 화면에 보이는 키가 진짜(창을 열 때마다 새 키가 나옴) → 항상 다시 읽어서 저장
            key = find_otp_secret(b, cur)
            if key and key != sec.mini_otp:
                _save_otp(b, sec, key)
                from . import notify
                notify.toast("구글 OTP 등록", "봇이 미니 OTP 키를 저장했습니다. 이제 2단계 인증을 자동으로 통과합니다.")
        inputs = _code_inputs(scope)
        if inputs and sec.mini_otp:
            if tried_codes >= 3:
                b.snap(cur, "mini_2fa_otp_fail")
                return False
            tried_codes += 1
            if tried_codes > 1:
                time.sleep(max(1, 31 - (time.time() % 30)))  # 30초 경계 → 다음 코드로
            _fill_code(inputs, _otp_code(sec.mini_otp))
            names = ["OTP 등록하기", "등록하기", "등록", "OTP 등록", "인증하기", "인증", "확인", "완료", "사방넷 미니 접속", "로그인"]
            clicked = None
            for n in names:
                btns = visible_buttons(scope, [n])
                if btns:
                    btns[0].click()
                    clicked = n
                    break
            if not clicked:
                for part in ("등록", "인증", "확인"):  # 'OTP 등록하기'처럼 앞뒤에 글자가 붙은 버튼
                    btns = visible_buttons(scope, [part], exact=False)
                    btns = [x for x in btns if "다운로드" not in (x.inner_text() or "") and "매뉴얼" not in (x.inner_text() or "")]
                    if btns:
                        btns[0].click()
                        clicked = (btns[0].inner_text() or part).strip()
                        break
            if not clicked:
                inputs[-1].press("Enter")
            b.run.info(f"OTP 코드 입력 → [{clicked or 'Enter'}]")
            time.sleep(4)
            msg = el_messagebox(cur, accept=True, timeout=2) if not cur.is_closed() else None
            if msg:
                b.run.info(f"OTP 결과 안내창: {msg[:100]}")
            try:
                if not cur.is_closed():
                    b.snap(cur, "mini_2fa_otp_after")
            except Exception:
                pass
            continue
        if inputs and not sec.mini_otp:
            b.run.warn("OTP 입력칸은 있는데 등록 키를 못 찾음")
            b.snap(cur, "mini_2fa_otp_nokey")
            return False
        # 입력칸 없음 → 다음 단계 버튼 (키를 못 찾은 상태에선 '인증하기/다음'만)
        names = ["인증하기", "다음", "OTP 등록"]
        if sec.mini_otp:
            names += ["등록하기", "등록", "확인"]
        elif dlg is not None and not _has_qr(dlg):
            names += ["확인", "예"]  # QR 없는 글자 안내창(등록하시겠습니까 등)만 '확인' — 키 모르는 채로 등록 마무리는 안 함
        clicked = None
        for n in names:
            btns = visible_buttons(scope, [n])
            if btns:
                btns[0].click()
                clicked = n
                break
        if not clicked:
            b.snap(cur, "mini_2fa_otp_stuck")
            return bool(sec.mini_otp)
        b.run.info(f"OTP 화면: [{clicked}] 클릭")
        time.sleep(3)
        try:
            b.snap(b.ctx.pages[-1], f"mini_2fa_otp_step{step}")
        except Exception:
            pass
    return bool(sec.mini_otp)


def _back_to_alimtalk(b: Browser, page) -> None:
    """OTP 등록 대화창 닫고 알림톡 탭으로 (사장님이 직접 인증할 수 있게)."""
    try:
        for p in [page]:
            if p.is_closed():
                continue
            for _ in range(2):
                dlg = _active_dialog(p)
                if not dlg:
                    break
                btns = visible_buttons(dlg, ["닫기", "취소", "×", "X"])
                if btns:
                    btns[0].click()
                else:
                    p.keyboard.press("Escape")
                time.sleep(1)
        if not page.is_closed():
            _tab(page, r"^\s*알림톡\s*$")
    except Exception:
        pass


def _handle_2fa(b: Browser, page, sec: config.Secrets) -> None:
    b.snap(page, "mini_2fa")
    try:
        ok = _otp_flow(b, page, sec)
    except Exception as e:
        b.run.warn(f"구글 OTP 처리 중 오류: {str(e)[:120]}")
        ok = False
    if ok:
        return
    _back_to_alimtalk(b, page)
    if b.headless:
        raise StepError("미니 2단계 인증이 떴습니다(새 장소/인터넷). 구글 OTP 자동 등록이 안 됨 → 창을 띄워 알림톡 인증 필요")
    # 창이 보이는 상태면 사장님이 직접 인증할 때까지 10분 기다림
    from . import notify
    try:
        page.bring_to_front()
    except Exception:
        pass
    notify.toast("미니 2단계 인증 필요", "봇이 띄운 크롬 창에서 [알림톡] → [발송] → 휴대폰 번호 6자리 입력 → [사방넷 미니 접속]. "
                 "같은 인터넷에선 90일간 다시 안 물어봅니다.", urgent=True)
    end = time.time() + 600
    while time.time() < end:
        if not b.ctx.pages:
            raise StepError("봇 크롬 창이 닫혀서 미니 인증을 중단함")
        if any(is_admin(p) for p in b.ctx.pages):
            return
        if not _otp_pages(b, page):
            return  # 인증 끝남 → 로그인 흐름이 이어서 처리
        time.sleep(3)
    raise StepError("미니 2단계 인증을 10분 안에 못 받아서 중단")


def _confirm_v_dialog(b: Browser, page, keywords) -> bool:
    """www(Vuetify) 화면에 뜬 안내 창 중 keywords가 들어간 것에 '확인'."""
    try:
        dlg = page.locator(".v-overlay--active .v-overlay__content, .v-dialog").filter(visible=True)
        for i in range(dlg.count()):
            d = dlg.nth(i)
            t = d.inner_text(timeout=2000)
            if any(k in t for k in keywords):
                b.run.info(f"미니 안내창: {' '.join(t.split())[:80]} → 확인")
                btn = d.locator("button").filter(has_text=re.compile(r"^\s*확인\s*$"))
                if btn.count():
                    btn.first.click()
                    return True
    except Exception:
        pass
    return False


def logout(b: Browser, s: config.Settings) -> None:
    """작업 끝나면 로그아웃 (사장님이 크롬에서 미니 들어갈 때 '동시 접속' 안 뜨게)."""
    try:
        page = b.ctx.new_page()
        page.goto(s.mini_login_url.split("/login")[0] + "/", wait_until="domcontentloaded")
        time.sleep(2)
        btn = page.locator(".btn-logout-text").filter(visible=True)
        if btn.count():
            btn.first.click()
            time.sleep(2)
            _confirm_v_dialog(b, page, ("로그아웃",))
            b.run.info("미니: 로그아웃")
        page.close()
    except Exception as e:
        b.run.warn(f"미니 로그아웃 실패(무시): {str(e)[:100]}")


def login(b: Browser, s: config.Settings, sec: config.Secrets):
    global _ADMIN_HOST
    if not sec.mini_ready():
        raise StepError("미니 아이디/비밀번호가 설정에 없습니다.")
    from urllib.parse import urlparse
    _ADMIN_HOST = urlparse(s.mini_admin_url).netloc or _ADMIN_HOST
    ff_host = urlparse(s.fulfill_url).netloc or "sbfulfillment"

    def mine() -> list:
        """미니 쪽 창들만 (송장 작업처럼 풀필먼트 창이 먼저 열려 있어도 헷갈리지 않게)."""
        return [p for p in b.ctx.pages if ff_host not in (p.url or "")]

    page = next(iter(mine()), None) or b.ctx.new_page()
    page.goto(f"{s.mini_admin_url}/#/dashboard", wait_until="domcontentloaded")
    time.sleep(3)
    if is_admin(page):
        b.run.info("미니: 이미 로그인됨")
        _adopt_admin(b, s, page)
        close_el_dialogs(page)
        return page
    b.run.info("미니: 로그인 시작")
    page.goto(s.mini_login_url, wait_until="domcontentloaded")
    time.sleep(2)
    page.get_by_placeholder("아이디를 입력해주세요.").fill(sec.mini_id)
    pw = page.get_by_placeholder("비밀번호를 입력해주세요.")
    pw.fill(sec.mini_pw)
    pw.press("Enter")
    time.sleep(4)
    handled_2fa = 0
    entered = False
    last_entry_retry = time.time()
    end = time.time() + 150
    while time.time() < end:
        for p in b.ctx.pages:
            if is_admin(p):
                b.run.info("미니: 로그인 완료")
                _adopt_admin(b, s, p)
                for other in mine():
                    if other is not p and not _admin_host_of(other.url):
                        try:
                            other.close()
                        except Exception:
                            pass
                time.sleep(2)
                close_el_dialogs(p)
                return p
        if not mine():
            raise StepError("봇 크롬 창이 닫혀서 미니 로그인을 중단함")
        cur = mine()[-1]
        t = body_text(cur)
        if re.search("아이디 또는 비밀번호|비밀번호가 일치|존재하지 않는 아이디", t):
            b.snap(cur, "mini_login_fail")
            raise StepError("미니 로그인 실패: 아이디/비밀번호를 확인해주세요.")
        msg = el_messagebox(cur, accept=True, timeout=1)
        if msg:
            b.run.info(f"미니 로그인 중 확인창: {msg[:100]}")
        if handled_2fa < 2 and re.search("2단계|인증번호|OTP|본인 ?인증|인증 ?방식", t) and not _admin_host_of(cur.url):
            handled_2fa += 1
            _handle_2fa(b, cur, sec)
            continue
        # 동시 접속 안내(이미 로그인된 아이디) → 확인 (먼저 로그인한 쪽이 로그아웃됨)
        if _confirm_v_dialog(b, cur, ("동시 접속", "로그인하시겠습니까", "이미 로그인")):
            time.sleep(3)
            continue
        # 로그인 후 홈 화면 오른쪽 위 "사방넷 미니 접속" (서비스코드 박스) → 관리화면(새 탭일 수 있음)
        entry = cur.locator(".sbn-join").filter(has_text="미니 접속")
        try:
            if entry.count() == 0:
                entry = cur.locator(".sbn-move").filter(has_text="미니 접속")
            has_entry = entry.count() > 0 and entry.first.is_visible()
        except Exception:
            has_entry = False
        if not has_entry:
            btns = visible_buttons(cur, ["사방넷 미니 접속", "로그인 후 시스템 접속", "시스템 접속"], exact=False)
            entry = btns[0] if btns else None
            has_entry = entry is not None
        else:
            entry = entry.first
        if has_entry and not entered:
            b.run.info("미니: '사방넷 미니 접속' 클릭")
            n_pages = len(b.ctx.pages)
            try:
                entry.click(timeout=5000)
            except Exception as e:
                b.run.warn(f"'사방넷 미니 접속' 클릭 막힘: {str(e).splitlines()[0][:80]}")
                b.snap(cur, "mini_entry_blocked")
                time.sleep(2)
                continue
            entered = True
            for _ in range(20):
                time.sleep(1)
                if len(b.ctx.pages) > n_pages:
                    b.ctx.pages[-1].wait_for_load_state("domcontentloaded")
                    break
            time.sleep(3)
            b.snap(mine()[-1], "mini_after_entry")
            continue
        if entered and time.time() - last_entry_retry > 40:
            entered = False  # 40초 지나도 관리화면이 안 뜨면 한 번 더 누름
            last_entry_retry = time.time()
        time.sleep(2)
    b.snap((mine() or b.ctx.pages)[-1], "mini_login_timeout")
    raise StepError("미니 로그인이 끝나지 않음 (캡처 확인 필요)")


# ------------------------------------------------------------------ 주문수집
def _client_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8181), timeout=2):
            return True
    except Exception:
        return False


def _start_client(b: Browser) -> bool:
    """사방넷 클라이언트가 꺼져 있으면 찾아서 켠다."""
    if _client_alive():
        return True
    import os
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA", "USERPROFILE")]
    found = None
    for r in filter(None, roots):
        base = Path(r)
        for pat in ("Sabangnet*/**/SabangnetClient*.exe", "*/Sabangnet*/**/SabangnetClient*.exe", "Desktop/SabangnetClient*.exe",
                    "Desktop/**/SabangnetClient*.exe", "Programs/**/SabangnetClient*.exe"):
            try:
                hits = list(base.glob(pat))
            except Exception:
                hits = []
            if hits:
                found = hits[0]
                break
        if found:
            break
    if not found:
        b.run.warn("사방넷 클라이언트 실행 파일을 못 찾음")
        return False
    b.run.info(f"사방넷 클라이언트 실행: {found}")
    try:
        subprocess.Popen([str(found)], cwd=str(found.parent), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception as e:
        b.run.warn(f"클라이언트 실행 실패: {e}")
        return False
    for _ in range(30):
        if _client_alive():
            time.sleep(3)
            return True
        time.sleep(1)
    return False


_ROW_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\[[^\]]*\][\s|]*([가-힣A-Za-z]+)")


def collect_rows(page) -> list[dict]:
    rows = page.evaluate("""() => [...document.querySelectorAll('.app-main .el-table__body-wrapper tbody tr.el-table__row')]
        .map(tr => [...tr.querySelectorAll('td')].map(td => td.textContent.replace(/\\s+/g,' ').trim()).join(' | '))""")
    out = []
    for r in rows:
        m = _ROW_RE.findall(r)
        mall = "스마트스토어" if "스마트스토어" in r else ("쿠팡" if "쿠팡" in r else r.split("|")[1].strip()[:15] if "|" in r else r[:15])
        out.append({"mall": mall, "last": m[0][0] if m else "", "status": m[0][1] if m else "", "raw": r[:200]})
    return out


HIST_ROUTE = "popup/views/pages/order/order-collect-auto/get-order-collect-auto-his-lists-popup.vue?menuNo=1258"
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _history_url(page, s: config.Settings) -> str:
    """[주문수집이력] 버튼이 여는 주소 (창은 실제로 안 열고 주소만 알아냄)."""
    from urllib.parse import urljoin
    u = ""
    try:
        u = page.evaluate("""() => { const o = window.open; let u = '';
            window.open = function (x) { u = String(x); return null; };
            try { const b = [...document.querySelectorAll('.app-main button')].find(e => e.innerText.trim() === '주문수집이력');
                  if (b) b.click(); } finally { window.open = o; }
            return u; }""") or ""
    except Exception:
        u = ""
    return urljoin(page.url, u) if u else f"{s.mini_admin_url}/#/{HIST_ROUTE}"


def read_history(hp, url: str, first: bool) -> list[dict]:
    """주문서수집이력: 쇼핑몰·구분(주문/클레임)·수집일자·종료일자·실패사유. 종료일자가 비어 있으면 아직 안 끝났거나 중간에 끊긴 것."""
    if first:
        hp.goto(url, wait_until="domcontentloaded")
    else:
        hp.reload(wait_until="domcontentloaded")
    rows = []
    for _ in range(30):
        time.sleep(0.5)
        try:
            rows = hp.evaluate("""() => [...document.querySelectorAll('.el-table__body-wrapper tbody tr.el-table__row')]
                .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.replace(/\\s+/g, ' ').trim()))""")
        except Exception:
            rows = []
        if rows:
            break
    out = []
    for r in rows:
        txt = " | ".join(r)
        ts = _TS_RE.findall(txt)
        out.append({"mall": r[1] if len(r) > 1 else "", "kind": r[3] if len(r) > 3 else "",
                    "start": ts[0] if ts else "", "end": ts[1] if len(ts) > 1 else "",
                    "reason": (r[7] if len(r) > 7 else "").strip(), "raw": txt[:200]})
    return out


def _client_frame_text(page) -> str:
    """화면 안 사방넷 클라이언트(127.0.0.1:8181) 작은 창의 글자 (진단용)."""
    for f in page.frames:
        if "8181" in (f.url or ""):
            try:
                return re.sub(r"\s+", " ", f.inner_text("body", timeout=1500))[:150] or "(빈 화면)"
            except Exception:
                return "(읽기 실패)"
    return ""


def _start_collect(b: Browser, page, days: int) -> str | None:
    click_button(page, [f"{days}일" if days > 1 else "오늘"], what="수집기간")
    time.sleep(0.5)
    # 쇼핑몰 전체 선택
    page.evaluate("""() => { const t=[...document.querySelectorAll('.app-main .el-table')].map(e=>e.__vue__).find(t=>t && t.toggleRowSelection);
       if(t){ t.clearSelection(); t.data.forEach(r=>t.toggleRowSelection(r,true)); } }""")
    time.sleep(0.5)
    with ConfirmOK(b):
        click_button(page, ["주문수집(신규+주문확인)"], what="주문수집 버튼")
        return el_messagebox(page, accept=True, timeout=5)


def _wait_collect(b: Browser, page, hp, hurl: str, prev_latest: str, n_malls: int, tag: str) -> list[dict]:
    """수집이 끝날 때까지 기다림. ★ 이 동안 미니 화면은 절대 건드리지 않음
    (10/2 원인: 수집 중에도 상태칸은 '비정상종료'로 보임 → 봇이 끝난 줄 알고 다른 화면으로 넘어가 수집을 끊어버림.
     수집은 화면 안의 작은 창(클라이언트 8181)이 살아 있어야 진행됨)"""
    t_click = time.time()
    last_frame = None
    new: list[dict] = []
    while time.time() - t_click < 600:
        time.sleep(5)
        ft = _client_frame_text(page)
        if ft and ft != last_frame:
            b.run.info(f"클라이언트 창: {ft}")
            last_frame = ft
        try:
            hist = read_history(hp, hurl, first=False)
        except Exception as e:
            b.run.warn(f"수집이력 읽기 오류: {str(e)[:80]}")
            continue
        new = [h for h in hist if h["start"] and h["start"] > prev_latest]
        if not new:
            if time.time() - t_click > 90:
                b.snap(page, f"mini_collect_not_started{tag}")
                raise StepError("미니 주문수집이 시작되지 않았습니다 (수집이력에 새 기록 없음)")
            continue
        if len(new) >= n_malls and all(h["end"] for h in new):
            break
    else:
        b.snap(page, f"mini_collect_timeout{tag}")
        b.snap(hp, f"mini_collect_history_timeout{tag}")
        raise StepError("미니 주문수집이 10분 넘게 안 끝남 " + "; ".join(f"{h['mall']}-{h['kind']} {h['start']}~{h['end'] or '진행중'}" for h in new))
    b.run.info(f"수집이력({int(time.time() - t_click)}초): " + "; ".join(
        f"{h['mall']}-{h['kind']} {h['start'][11:]}~{h['end'][11:]}" + (f" 실패:{h['reason']}" if h["reason"] else "") for h in new))
    b.snap(hp, f"mini_collect_history{tag}")
    return new


def collect(b: Browser, page, s: config.Settings, check_only: bool = False) -> list[dict]:
    goto(page, s, "order/order-collect-auto", "주문수집이력")
    b.snap(page, "mini_collect_page")
    before = collect_rows(page)
    b.run.info("미니 수집 현황: " + " / ".join(f"{r['mall']} {r['last']} {r['status']}" for r in before))
    if not before:
        raise StepError("미니 주문수집 화면에 연결된 쇼핑몰이 없음")
    alive = _client_alive() or _start_client(b)
    b.run.info(f"사방넷 클라이언트: {'켜져 있음' if alive else '응답 없음'}")
    hurl = _history_url(page, s)
    hp = b.ctx.new_page()  # 수집이력은 별도 탭에서 봄 (미니 화면은 그대로 둠)
    try:
        hist = read_history(hp, hurl, first=True)
        prev_latest = max((h["start"] for h in hist if h["start"]), default="")
        if hist:
            h0 = hist[0]
            b.run.info(f"최근 수집이력: {h0['mall']}-{h0['kind']} {h0['start']}~{h0['end'] or '(종료 없음)'}"
                       + (f" 실패:{h0['reason']}" if h0["reason"] else ""))
        if check_only:
            return before
        days = max(1, min(6, int(s.collect_days)))
        rows = before
        for attempt in (1, 2):
            tag = "" if attempt == 1 else "_retry"
            msg = _start_collect(b, page, days)
            if msg:
                b.run.info(f"수집 확인창: {msg[:60]}")
            time.sleep(3)
            b.snap(page, f"mini_collect_clicked{tag}")
            new = _wait_collect(b, page, hp, hurl, prev_latest, len(before), tag)
            prev_latest = max([prev_latest] + [h["start"] for h in new])
            # 끝난 뒤에만 새로고침
            try:
                click_button(page, ["새로고침"], timeout=3)
            except StepError:
                pass
            time.sleep(3)
            rows = collect_rows(page)
            b.snap(page, f"mini_collect_result{tag}")
            summary = " / ".join(f"{r['mall']} {r['last']} {r['status']}" for r in rows)
            b.run.info(f"미니 수집 결과: {summary}")
            bad = [r for r in rows if "비정상" in r["status"]] or [h for h in new if h["reason"]]
            if not bad:
                break
            if attempt == 1:
                b.run.warn("수집 결과에 비정상종료/실패사유 있음 → 한 번 더 수집")
                time.sleep(5)
        for r in rows:
            if "비정상" in r["status"]:
                b.run.warn(f"{r['mall']} 수집 '비정상종료' (주문은 들어왔을 수 있음)")
        return rows
    finally:
        try:
            hp.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 주문확정
def search_start(cut: dt.datetime) -> dt.date:
    return max(cut.date(), dt.date.today() - dt.timedelta(days=7))


def set_page_size(b: Browser, page, size: int = 100) -> None:
    """목록 개수(25) → 100으로."""
    try:
        inp = page.locator(".app-main .el-select input").all()
        target = None
        for el in inp:
            if (el.input_value() or "").strip() in ("25", "50", "20", "10") and el.is_visible():
                target = el
                break
        if target is None:
            return
        target.click()
        time.sleep(0.6)
        opts = page.locator(".el-select-dropdown__item:visible")
        best, best_n = None, 0
        for i in range(opts.count()):
            t = opts.nth(i).inner_text().strip()
            n = int(re.sub(r"\D", "", t) or 0)
            if n > best_n and n <= 500:
                best, best_n = opts.nth(i), n
        if best is not None:
            best.click()
            time.sleep(0.8)
            b.run.info(f"목록 개수 {best_n}개로 변경")
        else:
            page.keyboard.press("Escape")
    except Exception as e:
        b.run.warn(f"목록 개수 변경 실패: {e}")


def decide(b: Browser, page, s: config.Settings, cut: dt.datetime, check_only: bool = False) -> dict:
    goto(page, s, "order/order-decide", "일괄주문확정")
    start = search_start(cut)
    if not set_dates(page, start, dt.date.today()):
        b.run.warn("확정관리: 검색 기간 설정 실패 → 기본 기간으로 검색")
    set_page_size(b, page)
    click_button(page, ["검색"], what="검색")
    time.sleep(3)
    b.snap(page, "mini_decide_list")
    orders = [to_order(r) for r in table_data(page)]
    if orders:
        keys = sorted({o.ordered_key for o in orders if o.ordered_key})
        ex = next((o for o in orders if o.ordered), orders[0])
        b.run.info(f"주문일시 기준 항목: {keys or '(없음 → 수집일시 사용)'} 예) {ex.ordered or ex.collected}")
    new = [o for o in orders if o.after(cut)]
    old = [o for o in orders if not o.after(cut)]
    undecided = [o for o in new if o.decided != "Y"]
    unmapped = [o for o in undecided if not o.mapped]
    ready = [o for o in undecided if o.mapped]
    b.run.info(f"확정관리: 전체 {len(orders)} / 시작일 이후 {len(new)} / 미확정 {len(undecided)} "
               f"(매핑됨 {len(ready)}, 매핑 필요 {len(unmapped)}) / 시작일 이전 {len(old)}")
    res = {"ready": ready, "unmapped": unmapped, "confirmed": [], "old": old}
    if check_only or not ready:
        return res
    if old:
        b.run.warn(f"검색결과에 시작일 이전 주문 {len(old)}건 포함 → 확정은 선택한 주문만 하도록 진행")
    n = select_rows(page, [o.ord_no for o in ready])
    sel = selected_count(page)
    b.run.info(f"확정 대상 선택: {n}건 (화면 선택 {sel})")
    if n != len(ready) or (sel >= 0 and sel != len(ready)):
        raise StepError(f"확정할 주문 선택이 맞지 않음 (대상 {len(ready)} / 선택 {sel})")
    with ConfirmOK(b):
        page.locator(".app-main button").filter(has_text=re.compile(r"^\s*일괄주문확정\s*$")).first.click()
        run_bulk_modal(b, page, "일괄주문확정", len(ready))
    toasts = el_toasts(page)
    if toasts:
        b.run.info(f"확정 메시지: {toasts}")
    b.snap(page, "mini_decide_done")
    close_vm_modals(page)
    click_button(page, ["검색"], what="검색")
    time.sleep(3)
    left = {to_order(r).ord_no for r in table_data(page) if to_order(r).decided != "Y"}
    res["confirmed"] = [o for o in ready if o.ord_no not in left]
    if len(res["confirmed"]) != len(ready):
        b.run.warn(f"확정 후에도 미확정으로 보이는 주문 {len(ready) - len(res['confirmed'])}건 → 주문서확인처리 화면에서 다시 확인")
    return res


# ------------------------------------------------------------------ 주문상태 '주문확인'
STATUS_NEW = "001"


def confirm_list(b: Browser, page, s: config.Settings, cut: dt.datetime) -> list[MiniOrder]:
    goto(page, s, "order/order-confirm", "주문상태변경")
    if not set_dates(page, search_start(cut), dt.date.today()):
        b.run.warn("확인처리: 검색 기간 설정 실패 → 기본 기간으로 검색")
    set_page_size(b, page)
    click_button(page, ["검색"], what="검색")
    time.sleep(3)
    return [to_order(r) for r in table_data(page)]


def status_confirm(b: Browser, page, s: config.Settings, cut: dt.datetime, check_only: bool = False,
                   probe_popup: bool = False) -> dict:
    orders = confirm_list(b, page, s, cut)
    b.snap(page, "mini_confirm_list")
    new = [o for o in orders if o.after(cut)]
    targets = [o for o in new if o.status == STATUS_NEW]
    others = {}
    for o in new:
        others[o.status] = others.get(o.status, 0) + 1
    b.run.info(f"확인처리: 시작일 이후 {len(new)}건, 상태별 {others} → 주문확인 대상 {len(targets)}건")
    res = {"targets": targets, "done": [], "all": new}
    if check_only:
        if probe_popup and orders:
            _probe_status_popup(b, page, [orders[0].ord_no])
        return res
    if not targets:
        return res
    n = select_rows(page, [o.ord_no for o in targets])
    sel = selected_count(page)
    if n != len(targets) or (sel >= 0 and sel != len(targets)):
        raise StepError(f"주문확인 대상 선택이 맞지 않음 (대상 {len(targets)} / 선택 {sel})")
    popup, modal = _open_status_changer(b, page)
    scope = popup or modal
    b.snap(popup or page, "mini_status_popup")
    # 실제 창(10/6 확인): '주문서 일괄 상태변경' — ○선택된 주문서 : N건 ○검색된 주문서 : M건 / 변경할 주문상태 / 송장번호 삭제 여부 / [저장][닫기]
    head = re.sub(r"\s+", "", scope.inner_text("body") if popup else scope.inner_text())
    if "선택된주문서" in head and f"선택된주문서:{len(targets)}건" not in head:
        if popup:
            popup.close()
        raise StepError(f"상태변경 창의 선택 건수가 다름 (대상 {len(targets)}) — {head[:80]}")
    # 상태 선택: '주문상태코드' 선택칸 → '주문확인'
    sel_input = scope.get_by_placeholder("주문상태코드")
    if sel_input.count() == 0:
        sel_input = scope.locator(".el-select input").first
    sel_input.first.click()
    time.sleep(0.8)
    owner = popup or page
    opt = owner.locator(".el-select-dropdown__item:visible").filter(has_text=re.compile(r"^\s*주문확인\s*$"))
    if opt.count() == 0:
        opt = owner.locator(".el-select-dropdown__item:visible").filter(has_text="주문확인")
    if opt.count() == 0:
        b.snap(owner, "mini_status_no_option")
        raise StepError("주문상태 목록에 '주문확인'이 없음")
    opt.first.click()
    time.sleep(0.5)
    # '선택된 주문서'만 적용 (검색된 전체 X)
    try:
        r = scope.locator(".el-radio").filter(has_text=re.compile("선택"))
        if r.count() and "is-checked" not in (r.first.get_attribute("class") or ""):
            r.first.click()
    except Exception:
        pass
    # 송장번호 삭제 여부: 기본값이 '기존송장내용을 지운다' → '지우지 않는다'로
    try:
        owner_ = popup or page
        ok_keep = owner_.evaluate("""() => { const s=[...document.querySelectorAll('.el-select')].map(e=>e.__vue__).filter(Boolean)
            .find(s => (s.options||[]).some(o => /지우지 않는다/.test(o.currentLabel || o.label || '')));
          if(!s) return null; s.$emit('input', 'N'); s.$emit('change', 'N'); return true; }""")
        time.sleep(0.4)
        if ok_keep:
            b.run.info("송장번호 삭제 여부: 지우지 않는다")
    except Exception as e:
        b.run.warn(f"송장번호 삭제 여부 설정 실패: {str(e)[:80]}")
    b.snap(owner, "mini_status_filled")
    with ConfirmOK(b):
        click_button(scope, ["저장", "변경", "상태변경", "주문상태변경", "적용"], what="상태변경 저장 버튼")
        msg = el_messagebox(owner, accept=True, timeout=6)
        b.run.info(f"상태변경 확인창: {msg}")
        time.sleep(3)
        msg2 = el_messagebox(owner, accept=True, timeout=3) if not owner.is_closed() else None
    if msg2:
        b.run.info(f"상태변경 결과창: {msg2[:150]}")
    close_vm_modals(page)
    if popup and not popup.is_closed():
        b.snap(popup, "mini_status_popup_after")
        try:
            popup.close()
        except Exception:
            pass
    after = confirm_list(b, page, s, cut)
    still = {o.ord_no for o in after if o.status == STATUS_NEW}
    res["done"] = [o for o in targets if o.ord_no not in still]
    b.snap(page, "mini_confirm_after")
    b.run.info(f"주문확인 처리: {len(res['done'])}/{len(targets)}건")
    if not res["done"]:
        raise StepError("미니 주문상태 '주문확인' 변경이 반영되지 않음")
    return res


def _open_status_changer(b: Browser, page):
    """주문상태변경 버튼 → 팝업창(새 창) 또는 화면 안 모달."""
    n = len(b.ctx.pages)
    click_button(page, ["주문상태변경"], what="주문상태변경")
    end = time.time() + 10
    while time.time() < end:
        if len(b.ctx.pages) > n:
            pop = b.ctx.pages[-1]
            pop.wait_for_load_state("domcontentloaded")
            time.sleep(2)
            return pop, None
        msg = el_messagebox(page, accept=False, timeout=0.5)
        if msg:
            raise StepError(f"주문상태변경 불가: {msg}")
        m = page.locator(".vm--modal:visible, .el-dialog__wrapper:visible").filter(has_text="주문상태")
        if m.count():
            return None, m.last
        time.sleep(0.4)
    raise StepError("주문상태변경 창이 안 열림")


def _probe_status_popup(b: Browser, page, ord_nos: list[str]) -> None:
    """점검용: 상태변경 창을 열어 구조만 기록하고 저장 없이 닫는다."""
    try:
        select_rows(page, ord_nos)
        popup, modal = _open_status_changer(b, page)
        if popup:
            b.snap(popup, "probe_mini_status_popup")
            popup.close()
        elif modal is not None:
            b.snap(page, "probe_mini_status_modal")
            try:
                click_button(modal, ["닫기", "취소"], timeout=3)
            except StepError:
                page.keyboard.press("Escape")
        select_rows(page, [])
    except Exception as e:
        b.run.warn(f"점검: 상태변경 창 확인 실패 - {e}")


# ------------------------------------------------------------------ 쇼핑몰 운송장 송신 (미니 → 스마트스토어·쿠팡)
_INV_KEY = re.compile(r"(invc|invoice|wybl|waybill|dlvNo|delvNo|dlvyNo|trnspNo|trnsNo|dlvryNo)", re.I)
_SENT_KEY = re.compile(r"(trsm|send|snd|trns).*(yn|Yn|YN|stat|Sts|sts)$|^(trsm|send|snd).*", re.I)
_SAFE_KEY = re.compile(r"nm|name|addr|tel|hp|zip|msg|email|rcv|ordr|buyer|memo", re.I)


def _waybill_page(b: Browser, page, s: config.Settings) -> None:
    goto(page, s, "order/mall-waybill-transmit", None)
    end = time.time() + 20
    while time.time() < end and not visible_buttons(page, ["송신"], exact=False):
        time.sleep(1)
    close_el_dialogs(page)


def _waybill_fields(rows: list[dict]) -> tuple[str | None, str | None]:
    inv = sent = None
    for r in rows:
        for k, v in r.items():
            if inv is None and _INV_KEY.search(k) and str(v or "").strip() and re.search(r"\d{8,}", str(v)):
                inv = k
            if sent is None and _SENT_KEY.search(k) and str(v) in ("Y", "N"):
                sent = k
    return inv, sent


def _waybill_search(b: Browser, page) -> None:
    """기간 1주일 + '송장미송신'만 보기 → 검색."""
    try:
        click_button(page, ["1주일"], timeout=3)
    except StepError:
        set_dates(page, dt.date.today() - dt.timedelta(days=7), dt.date.today())
    try:
        cb = page.locator(".app-main .el-checkbox").filter(has_text="송장미송신")
        if cb.count() and "is-checked" not in (cb.first.get_attribute("class") or ""):
            cb.first.click()
            time.sleep(0.3)
    except Exception:
        b.run.warn("운송장송신: '송장미송신' 체크 실패")
    set_page_size(b, page)
    click_button(page, ["검색"], timeout=5, what="검색")
    time.sleep(3)


def probe_waybill(b: Browser, page, s: config.Settings) -> str:
    """점검용: 쇼핑몰운송장송신 화면 구조만 기록 (아무것도 안 누름)."""
    _waybill_page(b, page, s)
    b.snap(page, "probe_mini_waybill")
    btns = [t.strip() for t in page.locator(".app-main button").all_inner_texts() if t.strip() and t.strip() != "?"]
    radios = [t.strip() for t in page.locator(".app-main .el-radio, .app-main .el-checkbox").all_inner_texts() if t.strip()]
    rows = table_data(page)
    keys = sorted({k for r in rows for k in r.keys() if not _SAFE_KEY.search(k)})
    inv, sent = _waybill_fields(rows)
    b.run.info(f"운송장송신 화면 버튼: {btns[:40]}")
    b.run.info(f"운송장송신 화면 선택지: {radios[:30]}")
    b.run.info(f"운송장송신 표: {len(rows)}줄, 항목: {keys[:80]}")
    b.run.info(f"운송장송신 추정 항목: 송장={inv}, 송신여부={sent}")
    return f"{len(rows)}줄"


def send_waybills(b: Browser, page, s: config.Settings) -> dict:
    """미니 [쇼핑몰운송장송신]: 송장번호가 들어왔고 아직 쇼핑몰에 안 보낸 주문을 골라 송신."""
    _waybill_page(b, page, s)
    _waybill_search(b, page)
    b.snap(page, "mini_waybill_list")
    rows = table_data(page)
    inv, sent = _waybill_fields(rows)
    b.run.info(f"운송장송신 목록 {len(rows)}줄 (송장 항목={inv}, 송신여부 항목={sent})")
    if not rows:
        return {"sent": 0, "targets": 0, "note": "보낼 송장 없음"}
    if inv is None:
        raise StepError("운송장송신 화면에서 송장번호 항목을 못 찾음 (캡처 확인 필요)")
    targets = [r for r in rows if str(r.get(inv) or "").strip() and (sent is None or str(r.get(sent)) != "Y")]
    b.run.info(f"송신 대상 {len(targets)}건: " + ", ".join(str(r.get('shmaOrdNo')) for r in targets[:10]))
    if not targets:
        return {"sent": 0, "targets": 0, "note": "보낼 송장 없음"}
    n = select_rows(page, [str(r.get("ordNo")) for r in targets])
    b.run.info(f"송신 대상 선택 {n}건 (화면 선택 {selected_count(page)})")
    # 버튼: '선택' 들어간 송신 버튼 우선, 없으면 '송신' 버튼
    btn = None
    for pat in (r"^\s*운송장\s*송신\s*$", r"선택.*송신", r"송신"):
        cand = [el for el in visible_buttons(page, ["송신"], exact=False)
                if re.search(pat, el.inner_text()) and "강제" not in el.inner_text()]
        cand = [el for el in cand if "전체" not in el.inner_text()] or cand
        if cand:
            btn = cand[0]
            break
    if btn is None:
        raise StepError("운송장송신 버튼을 못 찾음")
    b.run.info(f"누를 버튼: {btn.inner_text().strip()}")
    n_pages = len(b.ctx.pages)
    with ConfirmOK(b):
        btn.click()
        msg = el_messagebox(page, accept=True, timeout=6)
        if msg:
            b.run.info(f"운송장송신 확인창: {msg[:150]}")
        time.sleep(3)
        msg2 = el_messagebox(page, accept=True, timeout=3)
        if msg2:
            b.run.info(f"운송장송신 결과창: {msg2[:150]}")
    b.snap(page, "mini_waybill_clicked")
    # 송신은 사방넷 클라이언트가 처리 → 목록 다시 조회하며 송신여부 확인 (최대 5분)
    done = 0
    end = time.time() + 300
    want = {str(r.get("ordNo")) for r in targets}
    while time.time() < end:
        for p in b.ctx.pages[n_pages:]:
            try:
                if re.search("완료|종료", body_text(p)):
                    b.snap(p, "mini_waybill_popup")
                    p.close()
            except Exception:
                pass
        time.sleep(10)
        try:
            click_button(page, ["검색"], timeout=3)
        except StepError:
            pass
        time.sleep(3)
        now_rows = {str(r.get("ordNo")): r for r in table_data(page)}
        if sent:
            done = sum(1 for o in want if str(now_rows.get(o, {}).get(sent)) == "Y" or o not in now_rows)
        else:
            done = sum(1 for o in want if o not in now_rows)
        if done >= len(want):
            break
    b.snap(page, "mini_waybill_after")
    b.run.info(f"쇼핑몰 운송장 송신: {done}/{len(want)}건 확인")
    return {"sent": done, "targets": len(want), "note": ""}
