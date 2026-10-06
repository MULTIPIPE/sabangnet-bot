"""사방넷 풀필먼트 (wms02.sbfulfillment.co.kr) 작업.

순서: 로그인 → [주문·배송 처리] 주문수집(미니 주문 → 발주 자동생성) → [발주조회] 새 발주만 골라 출고요청
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass

from . import config
from .browser import (Browser, ConfirmOK, StepError, click_button, v_dialogs, visible_buttons,
                      wait_v_dialog)

PO_RE = re.compile(r"O\d{8}-\d{5}")
DONE_WORDS = ("출고요청", "출고지시", "피킹", "검수", "포장", "출고완료", "배송", "취소", "보류")
PENDING_WORDS = ("출고요청전", "발주등록", "등록", "대기", "")


@dataclass
class PO:
    po_no: str
    mall_ord_no: str
    status: str
    raw: str
    receiver: str = ""
    product: str = ""

    @property
    def requested(self) -> bool:
        st = re.sub(r"\s+", "", self.status)
        if st in PENDING_WORDS:
            return False  # '출고요청전'은 아직 요청 전 (10/6: '출고요청'이 들어 있어 요청된 걸로 잘못 봄)
        return any(w in st for w in DONE_WORDS)

    @property
    def product_key(self) -> str:
        return re.sub(r"\s+", "", re.sub(r"^\s*\[미니\]\s*", "", self.product))


def find_duplicates(targets: list[PO], pos: list[PO]) -> dict[str, PO]:
    """봇이 올린 발주가, 봇이 올리지 않은 기존 발주(엑셀 등록 등)와 받는분·상품이 같으면 중복 의심."""
    mine = {t.po_no for t in targets}
    others = [e for e in pos if e.po_no not in mine]
    dup = {}
    for t in targets:
        if not t.receiver or not t.product_key:
            continue
        for e in others:
            if e.receiver == t.receiver and e.product_key == t.product_key:
                dup[t.po_no] = e
                break
    return dup


def _url(s: config.Settings, path: str) -> str:
    return s.fulfill_url.rstrip("/") + path


def login(b: Browser, s: config.Settings, sec: config.Secrets):
    if not sec.ff_ready():
        raise StepError("풀필먼트 회사코드/아이디/비밀번호가 설정에 없습니다.")
    page = b.ctx.new_page()
    page.goto(_url(s, "/login"), wait_until="domcontentloaded")
    time.sleep(2.5)
    if "/login" not in page.url:
        b.run.info("풀필먼트: 이미 로그인됨")
        return page
    b.run.info("풀필먼트: 로그인 시작")
    page.locator("input[name=companyCode]").fill(sec.ff_company)
    page.locator("input[name=id]").fill(sec.ff_id)
    page.locator("input[name=password]").fill(sec.ff_pw)
    click_button(page, ["로그인"], what="풀필먼트 로그인")
    end = time.time() + 40
    while time.time() < end:
        time.sleep(1.5)
        if "/login" not in page.url:
            time.sleep(2)
            b.run.info("풀필먼트: 로그인 완료")
            return page
        d = wait_v_dialog(page, 0.5)
        if d is not None:
            t = d.inner_text()
            b.run.info(f"풀필먼트 로그인 창: {t[:150]}")
            if re.search("인증번호|2단계", t):
                b.snap(page, "ff_2fa")
                raise StepError("풀필먼트 2단계 인증이 켜져 있어 자동 로그인 불가 (풀필먼트 설정 확인)")
            if re.search("일치|올바르지|잘못|존재하지", t):
                b.snap(page, "ff_login_fail")
                raise StepError("풀필먼트 로그인 실패: 회사코드/아이디/비밀번호 확인")
            try:
                click_button(d, ["확인", "예", "로그인"], timeout=2)
            except StepError:
                pass
    b.snap(page, "ff_login_timeout")
    raise StepError("풀필먼트 로그인이 끝나지 않음 (캡처 확인 필요)")


# ------------------------------------------------------------------ 주문수집 (미니 → 발주)
def _set_range(b: Browser, page, days: int) -> str | None:
    """조회기간을 최근 days일로. 기본값이 '오늘~오늘'이라 어제 이전 주문 송장이 안 잡힘.
    실제 화면(10/2 확인): 시작칸('조회기간' 라벨)·끝칸이 따로인 flatpickr 2개(단일 날짜, 끝칸 최소값=시작일).
    범위 달력(mode range) 하나짜리 화면도 대비."""
    end = dt.date.today()
    start = end - dt.timedelta(days=max(1, days) - 1)
    try:
        r = page.evaluate("""([a, z]) => {
          const D = s => new Date(s + 'T00:00:00');
          const all = [...document.querySelectorAll('input.flatpickr-input')];
          const rng = all.find(i => i._flatpickr && i._flatpickr.config && i._flatpickr.config.mode === 'range');
          if (rng) { rng._flatpickr.setDate([D(a), D(z)], true); return {mode: 'range', idx: all.indexOf(rng), v: rng.value || ''}; }
          const lab = [...document.querySelectorAll('label')].find(l => (l.textContent || '').trim() === '조회기간');
          let st = null;
          if (lab) { const box = lab.closest('.v-input') || lab.parentElement; st = box && box.querySelector('input.flatpickr-input'); }
          if (!st) st = all[0];
          if (!st) return null;
          const idx = all.indexOf(st), en = all[idx + 1];
          if (!st._flatpickr) return {mode: 'pair', fp: false, idx, v: st.value || ''};
          if (en && en._flatpickr) en._flatpickr.setDate(D(z), true);   // 끝 먼저(오늘) → 시작의 최대값 문제 없음
          st._flatpickr.setDate(D(a), true);
          return {mode: 'pair', fp: true, idx, v: (st.value || '') + ' ~ ' + (en ? en.value || '' : '')};
        }""", [start.isoformat(), end.isoformat()])
    except Exception as e:
        b.run.warn(f"조회기간 설정 오류: {str(e)[:100]}")
        return None
    v = None
    if r is not None:
        v = r.get("v", "")
        if r.get("mode") == "pair" and not r.get("fp"):
            # 달력 객체가 없으면 시작칸에 직접 입력 (allow-input)
            try:
                box = page.locator("input.flatpickr-input").nth(int(r.get("idx", 0)))
                box.click()
                page.keyboard.press("Control+A")
                page.keyboard.type(start.isoformat())
                page.keyboard.press("Enter")
                page.keyboard.press("Escape")
                v = box.input_value()
            except Exception as e:
                b.run.warn(f"조회기간 직접 입력 오류: {str(e)[:100]}")
    if v is None:
        b.run.warn("조회기간 달력을 못 찾음 → 화면 기본 기간으로 진행")
    elif start.isoformat() not in v:
        b.run.warn(f"조회기간이 기대와 다름: {v}")
    else:
        b.run.info(f"조회기간: {v}")
    time.sleep(0.5)
    return v


def _link_row(b: Browser, page, s: config.Settings, snap: str) -> dict:
    """주문·배송 처리 화면 → 조회기간 최근 7일 → 돋보기 → 미니 연동 줄의 숫자들(수집 대상/송장등록/미등록/전송완료/전송실패)."""
    page.goto(_url(s, "/interconnect/sb-order-list"), wait_until="domcontentloaded")
    time.sleep(3)
    if not visible_buttons(page, ["주문수집"], exact=False):
        time.sleep(3)
    _set_range(b, page, 7)
    _click_search(page)
    time.sleep(3)
    heads, rows = read_table(page)
    b.snap(page, snap)
    b.run.info(f"주문·배송처리 연동 목록: {len(rows)}줄 {[' | '.join(r)[:120] for r in rows[:3]]}")
    info = {"rows": rows, "heads": heads}
    row = next((r for r in rows if re.search("미니|mw\\d+", " ".join(r))), rows[0] if len(rows) == 1 else None)
    if row:
        def num(name):
            i = next((k for k, h in enumerate(heads) if h.replace(" ", "") == name), None)
            try:
                return int(re.sub(r"\D", "", row[i]) or 0) if i is not None and i < len(row) else None
            except Exception:
                return None
        info.update(collect_target=num("수집대상"), inv_registered=num("송장등록"), inv_missing=num("미등록"),
                    inv_sent=num("전송완료"), inv_failed=num("전송실패"))
    return info


def _check_link_row(page, rows) -> int:
    checked = 0
    trs = page.locator("tbody tr")
    for i in range(trs.count()):
        tr = trs.nth(i)
        txt = tr.inner_text()
        if len(rows) == 1 or re.search("미니|mw\\d+", txt):
            cb = tr.locator("input[type=checkbox]")
            if cb.count():
                try:
                    cb.first.check(force=True)
                except Exception:
                    cb.first.click(force=True)
                checked += 1
    time.sleep(0.5)
    return checked


def _wait_result(b: Browser, page, busy_re: str, secs: int = 240) -> str:
    result = ""
    end = time.time() + secs
    while time.time() < end:
        time.sleep(3)
        texts = []
        for dd in v_dialogs(page):
            try:
                texts.append(dd.inner_text())
            except Exception:
                pass
        try:
            texts += page.locator(".v-snackbar:visible, .v-alert:visible").all_inner_texts()
        except Exception:
            pass
        joined = " / ".join(t.strip().replace("\n", " ") for t in texts if t.strip())
        if joined and re.search("완료|건|없습니다|성공|실패|오류", joined) and not re.search(busy_re, joined):
            result = joined
            break
    for dd in v_dialogs(page):
        try:
            click_button(dd, ["확인", "닫기"], timeout=2)
        except Exception:
            pass
    return result


def _set_collect_period(b: Browser, page, days: int) -> None:
    """[주문수집] 창의 수집기간(기본 오늘~오늘)을 최근 days일로.
    10/6 실제: 연휴(10/5)에 미니로 들어온 주문은 '오늘~오늘'이면 안 가져옴 → 수집성공인데 발주 0건이었음.
    이미 가져간 주문은 미니에서 '출고대기'로 바뀌므로 기간을 넓혀도 두 번 가져가지 않음."""
    end = dt.date.today()
    start = end - dt.timedelta(days=max(1, days) - 1)
    try:
        r = page.evaluate("""([a, z]) => {
          const D = s => new Date(s + 'T00:00:00');
          const boxes = [...document.querySelectorAll('.v-overlay__content, .v-dialog, [role=dialog]')]
            .filter(e => e.offsetParent !== null && /수집기간/.test(e.innerText || ''));
          const box = boxes[boxes.length - 1];
          if (!box) return null;
          const ins = [...box.querySelectorAll('input.flatpickr-input')];
          if (!ins.length) return {n: 0};
          const st = ins[0], en = ins[1];
          if (!st._flatpickr) return {n: ins.length, fp: false};
          if (en && en._flatpickr) en._flatpickr.setDate(D(z), true);
          st._flatpickr.setDate(D(a), true);
          return {n: ins.length, fp: true, v: (st.value || '') + ' ~ ' + (en ? en.value || '' : '')};
        }""", [start.isoformat(), end.isoformat()])
    except Exception as e:
        b.run.warn(f"수집기간 설정 오류: {str(e)[:100]}")
        return
    if not r or not r.get("n"):
        b.run.warn("주문수집 창에 수집기간 칸이 안 보임 → 기본 기간으로 진행")
        return
    v = r.get("v", "")
    if not r.get("fp"):
        try:
            loc = page.locator(".v-overlay__content input.flatpickr-input, [role=dialog] input.flatpickr-input").first
            loc.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(start.isoformat())
            page.keyboard.press("Enter")
            page.keyboard.press("Tab")
            v = loc.input_value()
        except Exception as e:
            b.run.warn(f"수집기간 직접 입력 오류: {str(e)[:80]}")
    time.sleep(0.5)
    if start.isoformat() in (v or ""):
        b.run.info(f"수집기간: {v}")
    else:
        b.run.warn(f"수집기간이 기대와 다름: {v}")


def collect(b: Browser, page, s: config.Settings, check_only: bool = False) -> str:
    info = _link_row(b, page, s, "ff_order_list")
    if check_only:
        return "점검: 화면만 확인"
    rows = info["rows"]
    if not rows:
        raise StepError("풀필먼트 주문·배송처리에 연동 계정 줄이 안 보임")
    if not _check_link_row(page, rows):
        raise StepError("풀필먼트 주문·배송처리에서 미니 연동 줄을 못 골랐음")
    with ConfirmOK(b):
        click_button(page, ["주문수집"], exact=False, what="풀필먼트 주문수집")
        time.sleep(1.5)
        d = wait_v_dialog(page, 6)
        if d is not None:
            _set_collect_period(b, page, 7)
            b.snap(page, "ff_collect_dialog")
            t = d.inner_text()
            b.run.info(f"주문수집 창: {t[:200]}")
            click_button(d, ["수집", "주문수집", "수집하기", "확인", "예", "실행"], what="주문수집 실행 버튼")
    result = _wait_result(b, page, "진행 ?중|수집 ?중")
    b.snap(page, "ff_collect_result")
    b.run.info(f"풀필먼트 주문수집 결과: {result or '(메시지 없음)'}")
    if re.search("실패|오류", result):
        raise StepError(f"풀필먼트 주문수집 오류: {result[:200]}")
    return result


def send_invoices(b: Browser, page, s: config.Settings) -> dict:
    """주문·배송 처리 → 미니 줄 체크 → [송장전송] : 3PL이 찍은 송장을 미니로 보냄."""
    info = _link_row(b, page, s, "ff_invoice_before")
    reg = info.get("inv_registered")
    b.run.info(f"풀필먼트 송장: 등록 {reg} / 미등록 {info.get('inv_missing')} / 전송완료 {info.get('inv_sent')} / 전송실패 {info.get('inv_failed')}")
    if reg == 0 and not info.get("inv_failed"):
        return {"sent": 0, "info": info, "result": "보낼 송장 없음"}
    if not info["rows"] or not _check_link_row(page, info["rows"]):
        raise StepError("풀필먼트 주문·배송처리에서 미니 연동 줄을 못 골랐음")
    with ConfirmOK(b):
        click_button(page, ["송장전송"], exact=False, what="풀필먼트 송장전송")
        time.sleep(1.5)
        d = wait_v_dialog(page, 6)
        if d is not None:
            b.snap(page, "ff_invoice_dialog")
            b.run.info(f"송장전송 창: {d.inner_text()[:200]}")
            click_button(d, ["전송", "송장전송", "전송하기", "확인", "예", "실행"], what="송장전송 실행 버튼")
    result = _wait_result(b, page, "진행 ?중|전송 ?중")
    b.snap(page, "ff_invoice_result")
    b.run.info(f"풀필먼트 송장전송 결과: {result or '(메시지 없음)'}")
    after = _link_row(b, page, s, "ff_invoice_after")
    sent = max(0, (after.get("inv_sent") or 0) - (info.get("inv_sent") or 0))
    b.run.info(f"송장전송 후: 등록 {after.get('inv_registered')} / 전송완료 {after.get('inv_sent')} / 전송실패 {after.get('inv_failed')}")
    if re.search("실패|오류", result) and not sent:
        raise StepError(f"풀필먼트 송장전송 오류: {result[:200]}")
    return {"sent": sent, "info": after, "result": result}


# ------------------------------------------------------------------ 발주조회
def _click_search(page) -> None:
    for t in ("검색", "조회"):
        btns = visible_buttons(page, [t])
        if btns:
            btns[0].click()
            return
    icon = page.locator("button:has(.mdi-magnify):visible")
    if icon.count():
        icon.first.click()
        return
    prim = page.locator("button.bg-primary:visible")
    for i in range(prim.count()):
        el = prim.nth(i)
        if not (el.inner_text() or "").strip():
            el.click()
            return
    raise StepError("발주조회 검색 버튼을 못 찾음")


def read_table(page) -> tuple[list[str], list[list[str]]]:
    data = page.evaluate("""() => {
      const tables=[...document.querySelectorAll('table')].filter(t=>t.offsetParent!==null);
      let best=null, n=-1;
      for(const t of tables){ const c=t.querySelectorAll('tbody tr').length; if(c>n){n=c; best=t;} }
      if(!best) return [[],[]];
      const norm=s=>s.replace(/\\s+/g,' ').trim();
      const hrows=[...best.querySelectorAll('thead tr')];
      const last=hrows.length? hrows[hrows.length-1] : null;
      const heads= last? [...last.querySelectorAll('th')].map(th=>norm(th.textContent)) : [];
      const rows=[...best.querySelectorAll('tbody tr')].map(tr=>[...tr.querySelectorAll('td')].map(td=>norm(td.textContent)));
      return [heads, rows]; }""")
    return data[0], data[1]


def list_pos(b: Browser, page, s: config.Settings, snap_name: str = "ff_po_list") -> list[PO]:
    page.goto(_url(s, "/order/list"), wait_until="domcontentloaded")
    time.sleep(3)
    _click_search(page)
    time.sleep(3)
    b.snap(page, snap_name)
    heads, rows = read_table(page)
    b.run.info(f"발주조회 표 머리글: {heads}")
    i_mall = next((i for i, h in enumerate(heads) if h.replace(" ", "") in ("주문번호", "쇼핑몰주문번호")), None)
    if i_mall is None:
        i_mall = next((i for i, h in enumerate(heads) if "주문번호" in h), None)
    i_stat = next((i for i, h in enumerate(heads) if h.replace(" ", "") == "진행상태"), None)
    if i_stat is None:
        i_stat = next((i for i, h in enumerate(heads) if "상태" in h), None)
    i_rcv = next((i for i, h in enumerate(heads) if "받는분" in h or "수취인" in h), None)
    i_prd = next((i for i, h in enumerate(heads) if "상품명" in h), None)
    out = []
    for r in rows:
        line = " | ".join(r)
        m = PO_RE.search(line)
        if not m:
            continue
        mall_no = ""
        if i_mall is not None and i_mall < len(r):
            mm = re.search(r"\d{8,20}", r[i_mall])
            mall_no = mm.group(0) if mm else r[i_mall]
        if not mall_no and i_mall is None:
            nums = [x for x in re.findall(r"\d{10,20}", line) if x not in m.group(0).replace("-", "")]
            mall_no = nums[0] if nums else ""
        status = r[i_stat] if (i_stat is not None and i_stat < len(r)) else ""
        rcv = r[i_rcv].strip() if (i_rcv is not None and i_rcv < len(r)) else ""
        prd = r[i_prd].strip() if (i_prd is not None and i_prd < len(r)) else ""
        out.append(PO(po_no=m.group(0), mall_ord_no=mall_no, status=status, raw=line[:300], receiver=rcv, product=prd))
    b.run.info(f"발주조회: {len(out)}건 " + ", ".join(f"{p.po_no}[{p.status}]" for p in out[:15]))
    return out


def _check_row(page, po_no: str) -> bool:
    """발주 하나가 상품 여러 줄(침대+커버 등)로 보일 수 있음 → 그 발주번호의 체크칸을 전부 체크."""
    rows = page.locator("tbody tr").filter(has_text=po_no)
    if rows.count() == 0:
        return False
    ok = False
    for i in range(rows.count()):
        cb = rows.nth(i).locator("input[type=checkbox]")
        if cb.count() == 0:
            continue
        try:
            cb.first.check(force=True)
        except Exception:
            cb.first.click(force=True)
        time.sleep(0.3)
        ok = cb.first.is_checked() or ok
    return ok


def _open_release_dialog(b: Browser, page):
    click_button(page, ["출고요청"], what="출고요청 버튼")
    d = wait_v_dialog(page, 8)
    if d is None:
        raise StepError("출고요청 창이 안 열림")
    return d


def release(b: Browser, page, s: config.Settings, po_nos: list[str]) -> list[str]:
    """발주조회에서 po_nos만 체크해서 출고요청. 요청된 발주번호 목록 반환."""
    pos = {p.po_no: p for p in list_pos(b, page, s, "ff_release_before")}
    targets = list(dict.fromkeys(n for n in po_nos if n in pos and not pos[n].requested))
    if not targets:
        b.run.info("출고요청할 발주 없음 (이미 요청됐거나 목록에 없음)")
        return []
    for n in targets:
        if not _check_row(page, n):
            raise StepError(f"발주 {n} 체크 실패")
    # 다른 발주가 체크돼 있지 않은지 확인
    checked = page.evaluate("""() => [...document.querySelectorAll('tbody tr')].filter(tr=>{const c=tr.querySelector('input[type=checkbox]'); return c && c.checked;})
        .map(tr=>(tr.textContent.match(/O\\d{8}-\\d{5}/)||[''])[0])""")
    if sorted({x for x in checked if x}) != sorted(set(targets)):
        raise StepError(f"체크된 발주가 대상과 다름: {checked}")
    b.snap(page, "ff_release_checked")
    d = _open_release_dialog(b, page)
    b.snap(page, "ff_release_dialog")
    b.run.info(f"출고요청 창: {d.inner_text()[:200]}")
    # '선택 항목' 라디오
    picked = False
    for loc in (d.get_by_label(re.compile("선택")), d.locator(".v-radio, .v-selection-control").filter(has_text=re.compile("선택"))):
        try:
            if loc.count():
                el = loc.first
                try:
                    el.check(force=True)
                except Exception:
                    el.click(force=True)
                picked = True
                break
        except Exception:
            continue
    if not picked:
        b.run.warn("출고요청 창에 '선택' 옵션이 안 보임 → 창 내용 캡처 확인 필요")
        raise StepError("출고요청 창에서 '선택 항목' 옵션을 못 찾음 (전체 요청 방지를 위해 중단)")
    time.sleep(0.5)
    b.snap(page, "ff_release_dialog_selected")
    t1 = d.inner_text()
    with ConfirmOK(b):
        click_button(d, ["출고요청", "요청", "확인", "저장", "예"], what="출고요청 확인 버튼")
        time.sleep(2.5)
        # 새로 뜬 '정말 요청할까요?' 창만 확인 (같은 창을 두 번 누르지 않게)
        for dd in v_dialogs(page):
            try:
                t2 = dd.inner_text()
            except Exception:
                continue
            if t2 == t1:
                continue
            b.run.info(f"출고요청 후 창: {t2[:200]}")
            if re.search("하시겠|진행하|요청할", t2):
                click_button(dd, ["확인", "예"], what="출고요청 최종 확인")
                time.sleep(2)
            break
    b.snap(page, "ff_release_result")
    for dd in v_dialogs(page):
        try:
            click_button(dd, ["확인", "닫기"], timeout=2)
        except Exception:
            pass
    after = {p.po_no: p for p in list_pos(b, page, s, "ff_release_after")}
    done = [n for n in targets if n in after and after[n].requested]
    if not done:
        raise StepError("출고요청 후 상태가 바뀌지 않음 (캡처 확인 필요)")
    return done


def probe_release_dialog(b: Browser, page, s: config.Settings) -> None:
    """점검용: 아무것도 체크하지 않고 출고요청 창만 열어 구조 기록 후 취소."""
    try:
        list_pos(b, page, s, "probe_ff_po_list")
        before = len(b.dialogs)
        click_button(page, ["출고요청"], what="출고요청 버튼")
        d = wait_v_dialog(page, 6)
        b.snap(page, "probe_ff_release_dialog")
        if d is not None:
            b.run.info(f"점검: 출고요청 창 내용: {d.inner_text()[:300]}")
            try:
                click_button(d, ["취소", "닫기"], timeout=2)
            except StepError:
                x = d.locator("button:has(.mdi-close), button[aria-label*=Close], button[aria-label*=닫기]")
                if x.count():
                    x.first.click()
                else:
                    page.keyboard.press("Escape")
        elif len(b.dialogs) > before:
            b.run.info(f"점검: 출고요청 클릭 시 알림: {b.dialogs[-1]}")
        time.sleep(1)
        b.snap(page, "probe_ff_after_cancel")
    except Exception as e:
        b.run.warn(f"점검: 출고요청 창 확인 실패 - {e}")
