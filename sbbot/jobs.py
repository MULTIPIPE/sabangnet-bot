"""작업 묶음: 발주(14:10) / 출고요청 승인 / 점검 / 송장(2차)."""
from __future__ import annotations

import datetime as dt
import time
import json
import traceback
from dataclasses import asdict

from . import config, fulfill, mini, notify, records
from .browser import Browser, StepError


def ensure_cutover() -> dt.datetime:
    """자동화 시작 시점 = 봇을 처음 켠 시점 (실행기록 중 가장 이른 시각). 한 번 정해지면 공용 설정에 저장."""
    s = config.load_settings()
    d = mini.parse_dt(s.cutover)
    if d:
        return d
    first = records.now().replace(second=0, microsecond=0)
    try:
        for f in config.records_dir().glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for job in data.values():
                for k in ("started", "updated", "finished"):
                    t = mini.parse_dt(job.get(k))
                    if t and t < first:
                        first = t.replace(second=0)
    except Exception:
        pass
    s.cutover = first.strftime("%Y-%m-%d %H:%M")
    config.save_settings(s)
    return first


def cutover_dt(s: config.Settings) -> dt.datetime:
    """자동화 시작 시점. 이 시각 이후에 들어온(주문된) 주문만 봇이 처리."""
    return mini.parse_dt(s.cutover) or ensure_cutover()


def _hm(t: str) -> dt.time:
    h, m = t.split(":")
    return dt.time(int(h), int(m))


def _now_iso() -> str:
    return records.now().isoformat(timespec="seconds")


def _fail(job: str, run: records.RunLog, e: Exception, title: str) -> dict:
    if isinstance(e, StepError):
        msg = str(e)
    else:
        msg = f"예상 못한 오류: {e}"
        run.warn(traceback.format_exc())
    run.warn(f"실패: {msg}")
    records.update_job(job, status="failed", finished=_now_iso(), error=msg[:500], log=str(run.dir))
    notify.toast(title, msg, urgent=True)
    return {"ok": False, "summary": msg}


# ------------------------------------------------------------------ 발주·출고요청
def run_order(trigger: str = "schedule") -> dict:
    s = config.load_settings()
    sec = config.load_secrets()
    cut = cutover_dt(s)
    run = records.RunLog("발주")
    run.info(f"발주 작업 준비 ({trigger})")
    try:
        st = records.job_state("order")
        attempts = int(st.get("attempts", 0)) + 1
        records.update_job("order", status="running", started=_now_iso(), attempts=attempts,
                           trigger=trigger, error="", log=str(run.dir), heartbeat=_now_iso())
        run.info(f"발주 작업 시작 ({trigger}, {attempts}번째, 자동화 시작 시점 {cut:%Y-%m-%d %H:%M})")
        headless = not s.show_browser
        try:
            res = _order_flow(run, s, sec, cut, headless)
        except StepError as e:
            if headless and ("수집이 시작되지" in str(e) or "수집이 10분 넘게" in str(e) or "2단계 인증이 떴습니다" in str(e)):
                run.warn(f"숨김 모드에서 막힘({str(e)[:40]}…) → 크롬 창을 띄워 다시 시도")
                res = _order_flow(run, s, sec, cut, False)
            else:
                raise
        return res
    except Exception as e:
        return _fail("order", run, e, "발주 자동화 실패")
    finally:
        run.close()


def _order_flow(run: records.RunLog, s: config.Settings, sec: config.Secrets, cut: dt.datetime, headless: bool) -> dict:
    late = records.now().time() > _hm(s.release_deadline)
    with Browser(run, headless=headless) as b:
        # 1) 미니
        page = mini.login(b, s, sec)
        crow = mini.collect(b, page, s)
        bad_collect = [r["mall"] for r in crow if "비정상" in (r.get("status") or "")]
        unmapped: list = []
        if s.mini_decide:
            d = mini.decide(b, page, s, cut)
            unmapped = d["unmapped"]
        records.update_job("order", heartbeat=_now_iso())
        conf_all: list = []
        if s.mini_status_confirm:
            try:
                r = mini.status_confirm(b, page, s, cut)
                conf_all = r["all"]
            except Exception as e:
                run.warn(f"주문확인 단계 문제: {e} → 풀필먼트 수집은 계속 진행")
                b.snap(page, "mini_confirm_error")
                conf_all = [o for o in mini.confirm_list(b, page, s, cut) if o.after(cut)]
        else:
            conf_all = [o for o in mini.confirm_list(b, page, s, cut) if o.after(cut)]
        mini.logout(b, s)
        known = records.ledger_orders("미니확정")
        for o in conf_all:
            if o.mall_ord_no and o.mall_ord_no not in known:
                records.ledger_add("미니확정", o.mall_ord_no, o.mall, o.ord_no, note=o.product[:40])
        records.update_job("order", heartbeat=_now_iso())

        # 2) 풀필먼트
        fpage = fulfill.login(b, s, sec)
        fulfill.collect(b, fpage, s)
        released = records.ledger_orders("출고요청")
        expected = records.ledger_orders("미니확정") - released
        pos = fulfill.list_pos(b, fpage, s)
        # 사장님(10/6): 수집으로 발주가 등록된 뒤 발주조회에 뜨기까지 1분 넘게 걸리기도 함 → 기대 주문이 다 보일 때까지 최대 4분 기다림
        t_end = time.time() + 240
        while expected and time.time() < t_end:
            got = {p.mall_ord_no for p in pos}
            if all(x in got for x in expected):
                break
            run.info(f"발주조회에 아직 안 보이는 주문 {len([x for x in expected if x not in got])}건 → 20초 뒤 다시 조회")
            time.sleep(20)
            pos = fulfill.list_pos(b, fpage, s)
        targets = [p for p in pos if p.mall_ord_no in expected and not p.requested]
        dups = fulfill.find_duplicates(targets, pos)
        if dups:
            for t in targets:
                if t.po_no in dups:
                    e = dups[t.po_no]
                    run.warn(f"중복 의심: {t.po_no}({t.receiver}, {t.product}) ↔ 기존 {e.po_no}[{e.status}] → 출고요청 안 함")
                    records.ledger_add("중복의심", t.mall_ord_no, po_no=t.po_no, note=f"기존 {e.po_no}")
            targets = [t for t in targets if t.po_no not in dups]
        today_tag = "O" + dt.date.today().strftime("%Y%m%d")
        unexpected = [p for p in pos if not p.requested and p.mall_ord_no not in expected
                      and p.po_no.startswith(today_tag)]
        seen_po = {r["발주번호"] for r in records.ledger_rows() if r.get("단계") == "발주생성"}
        for p in targets:
            if p.po_no not in seen_po:
                records.ledger_add("발주생성", p.mall_ord_no, po_no=p.po_no, note=p.status)
        got = {p.mall_ord_no for p in pos}
        missing = sorted(x for x in expected if x not in got)
        if missing:
            run.warn(f"미니엔 있는데 풀필먼트 발주가 안 보이는 주문: {missing}")
        if unexpected:
            run.warn(f"오늘 생긴 발주 중 봇이 모르는 것(출고요청 안 함): {[p.po_no for p in unexpected]}")
        if len(targets) > s.max_orders_per_run:
            raise StepError(f"출고요청 대상이 {len(targets)}건으로 너무 많아 멈춤 (최대 {s.max_orders_per_run})")

        notes = []
        if bad_collect:
            notes.append(f"미니 주문수집 비정상종료({', '.join(bad_collect)}) — 새 주문이 빠졌을 수 있음, 미니에서 직접 수집 확인")
        if unmapped:
            notes.append(f"매핑 필요 {len(unmapped)}건: " + ", ".join(o.label() for o in unmapped[:5]))
        if missing:
            notes.append(f"발주 안 보임 {len(missing)}건")
        if unexpected:
            notes.append(f"모르는 발주 {len(unexpected)}건")
        if dups:
            notes.append(f"중복 의심 {len(dups)}건(이미 있는 발주와 같은 사람·상품 — 출고요청 안 함, 풀필먼트에서 확인)")
        if late:
            notes.append(f"출고마감({s.release_deadline}) 지나서 실행됨")
        tail = (" · " + " · ".join(notes)) if notes else ""

        if not targets:
            summary = "새로 출고요청할 주문 없음" + tail
            records.update_job("order", status="done", finished=_now_iso(), summary=summary, pending=[])
            run.info(summary)
            notify.toast("발주 자동화 확인 필요" if bad_collect else "발주 자동화 완료", summary,
                         urgent=bool(unmapped or missing or dups or bad_collect))
            return {"ok": True, "summary": summary}

        if s.approve_before_release:
            pending = [asdict(p) for p in targets]
            summary = f"출고요청 대기 {len(targets)}건 (승인 필요)" + tail
            records.update_job("order", status="awaiting_approval", pending=pending, summary=summary)
            run.info(summary)
            notify.toast("출고요청 승인 필요", f"{len(targets)}건 발주 준비됨. 봇 창에서 [출고요청 실행]을 눌러주세요.", urgent=True)
            return {"ok": True, "summary": summary, "pending": pending}

        done = fulfill.release(b, fpage, s, list(dict.fromkeys(p.po_no for p in targets)))
        by_po = {p.po_no: p for p in targets}
        for n in done:
            records.ledger_add("출고요청", by_po[n].mall_ord_no, po_no=n)
    summary = f"출고요청 {len(done)}건 완료" + tail
    records.update_job("order", status="done", finished=_now_iso(), summary=summary, pending=[])
    run.info(summary)
    notify.toast("발주 자동화 확인 필요" if bad_collect else "발주 자동화 완료", summary, urgent=bool(notes))
    return {"ok": True, "summary": summary}


def run_release(po_nos: list[str]) -> dict:
    """승인 후 출고요청만 실행."""
    s = config.load_settings()
    sec = config.load_secrets()
    run = records.RunLog("출고요청")
    records.update_job("order", status="running", heartbeat=_now_iso(), log=str(run.dir))
    try:
        st = records.job_state("order")
        pend = {p["po_no"]: p for p in (st.get("pending") or [])}
        with Browser(run, headless=not s.show_browser) as b:
            fpage = fulfill.login(b, s, sec)
            done = fulfill.release(b, fpage, s, po_nos)
        for n in done:
            records.ledger_add("출고요청", pend.get(n, {}).get("mall_ord_no", ""), po_no=n, note="승인 후")
        summary = f"출고요청 {len(done)}건 완료 (승인)"
        left = [p for k, p in pend.items() if k not in done]
        records.update_job("order", status="done" if not left else "awaiting_approval",
                           finished=_now_iso(), summary=summary, pending=left)
        notify.toast("출고요청 완료", summary)
        return {"ok": True, "summary": summary}
    except Exception as e:
        records.update_job("order", status="awaiting_approval")
        return _fail("order", run, e, "출고요청 실패")
    finally:
        run.close()


# ------------------------------------------------------------------ 점검 (아무것도 바꾸지 않음)
def _check_mini(b: Browser, s: config.Settings, sec: config.Secrets, cut: dt.datetime, report: list) -> None:
    page = mini.login(b, s, sec)
    report.append("✔ 미니 로그인")
    rows = mini.collect(b, page, s, check_only=True)
    report.append("✔ 미니 주문수집 화면: " + " / ".join(f"{r['mall']} {r['last']} {r['status']}" for r in rows))
    report.append(f"  사방넷 클라이언트: {'켜짐' if mini._client_alive() else '응답 없음(발주 시간에 자동 실행 시도)'}")
    d = mini.decide(b, page, s, cut, check_only=True)
    report.append(f"✔ 미니 확정관리: 확정 대기 {len(d['ready'])}건, 매핑 필요 {len(d['unmapped'])}건, 시작 전 주문 {len(d['old'])}건")
    for o in d["unmapped"]:
        report.append(f"   - 매핑 필요: {o.label()}")
    r = mini.status_confirm(b, page, s, cut, check_only=True, probe_popup=True)
    report.append(f"✔ 미니 확인처리: 시작 이후 {len(r['all'])}건, 주문확인 대상 {len(r['targets'])}건")
    try:
        w = mini.probe_waybill(b, page, s)
        report.append(f"✔ 미니 쇼핑몰운송장송신 화면: {w}")
    except Exception as e:
        report.append(f"✘ 미니 쇼핑몰운송장송신 화면: {e}")
    mini.logout(b, s)


def run_check() -> dict:
    s = config.load_settings()
    sec = config.load_secrets()
    cut = cutover_dt(s)
    run = records.RunLog("점검")
    report: list = []
    ok = True
    need_2fa = False
    headless = not s.show_browser
    try:
        with Browser(run, headless=headless) as b:
            try:
                _check_mini(b, s, sec, cut, report)
            except Exception as e:
                if headless and "2단계 인증이 떴습니다" in str(e):
                    need_2fa = True
                else:
                    ok = False
                    report.append(f"✘ 미니: {e}")
                    run.warn(traceback.format_exc())
            try:
                fpage = fulfill.login(b, s, sec)
                report.append("✔ 풀필먼트 로그인")
                fulfill.collect(b, fpage, s, check_only=True)
                info = fulfill._link_row(b, fpage, s, "ff_link_counts")
                report.append(f"✔ 풀필먼트 주문·배송처리 화면 (송장 등록 {info.get('inv_registered')} / 미등록 {info.get('inv_missing')} / 전송완료 {info.get('inv_sent')})")
                fulfill.probe_release_dialog(b, fpage, s)
                report.append("✔ 풀필먼트 발주조회·출고요청 창 구조 기록 (요청은 안 함)")
            except Exception as e:
                ok = False
                report.append(f"✘ 풀필먼트: {e}")
                run.warn(traceback.format_exc())
        if need_2fa:
            # 크롬 창을 띄워서 사장님이 알림톡 인증을 한 번 하게 함 (같은 인터넷이면 90일 유지)
            run.info("미니 2단계 인증 필요 → 크롬 창을 띄워 인증 대기")
            with Browser(run, headless=False) as b2:
                try:
                    _check_mini(b2, s, sec, cut, report)
                except Exception as e:
                    ok = False
                    report.append(f"✘ 미니: {e}")
                    run.warn(traceback.format_exc())
    except Exception as e:
        ok = False
        report.append(f"✘ 브라우저: {e}")
    text = "\n".join(report)
    (run.dir / "점검결과.txt").write_text(text, encoding="utf-8")
    run.info("점검 결과\n" + text)
    run.close()
    bad = [r for r in report if r.startswith("✘")]
    notify.toast("점검 완료" if ok else "점검: 문제 있음", bad[0] if bad else "미니·풀필먼트 로그인과 화면 모두 정상", urgent=not ok)
    return {"ok": ok, "summary": text}


# ------------------------------------------------------------------ 송장 전송 (18:00)
def run_invoice(trigger: str = "schedule") -> dict:
    """3PL이 찍은 송장: 풀필먼트 → 미니 (송장전송) → 스마트스토어·쿠팡 (쇼핑몰운송장송신)."""
    s = config.load_settings()
    sec = config.load_secrets()
    run = records.RunLog("송장")
    run.info(f"송장 작업 준비 ({trigger})")
    try:
        st = records.job_state("invoice")
        attempts = int(st.get("attempts", 0)) + 1
        records.update_job("invoice", status="running", started=_now_iso(), attempts=attempts,
                           trigger=trigger, error="", log=str(run.dir), heartbeat=_now_iso())
        run.info(f"송장 전송 시작 ({trigger}, {attempts}번째)")
        with Browser(run, headless=not s.show_browser) as b:
            fpage = fulfill.login(b, s, sec)
            r1 = fulfill.send_invoices(b, fpage, s)
            records.update_job("invoice", heartbeat=_now_iso())
            page = mini.login(b, s, sec)
            r2 = mini.send_waybills(b, page, s)
            mini.logout(b, s)
        notes = []
        if r1.get("info", {}).get("inv_failed"):
            notes.append(f"풀필먼트→미니 전송실패 {r1['info']['inv_failed']}건")
        if r2["targets"] and r2["sent"] < r2["targets"]:
            notes.append(f"쇼핑몰 송신 확인 안 된 {r2['targets'] - r2['sent']}건")
        if r2["targets"] == 0 and r1["sent"] == 0:
            summary = "보낼 송장 없음"
        else:
            summary = f"송장 {r2['sent']}건 쇼핑몰 전송 완료 (풀필먼트→미니 {r1['sent']}건)"
        if notes:
            summary += " · " + " · ".join(notes)
        records.update_job("invoice", status="done", finished=_now_iso(), summary=summary)
        run.info(summary)
        notify.toast("송장 전송 완료", summary, urgent=bool(notes))
        return {"ok": True, "summary": summary}
    except Exception as e:
        return _fail("invoice", run, e, "송장 전송 실패")
    finally:
        run.close()
