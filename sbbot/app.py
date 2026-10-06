"""프로그램 본체: 창(설정/상태) + 트레이 + 시간 되면 알아서 실행.

실행 방식
- 원드라이브의 사방넷봇.exe(실행기)가 app\\ 조각을 합쳐 이 PC(%LOCALAPPDATA%\\사방넷봇\\bin)에 본체를 만들고 실행
  (원드라이브에 새 버전이 올라오면 알아서 갈아탄다)
- 윈도우 로그인 시 자동 시작(레지스트리 Run) → 트레이에 상주 → 평일 14:10 발주, 18:00 송장(2차)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import config, jobs, notify, records

log = logging.getLogger("app")
WEEK = ["월", "화", "수", "목", "금", "토", "일"]
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ------------------------------------------------------------------ 한 번만 실행 / 자동시작 / 업데이트
_mutex = None


def acquire_single_instance(wait: float = 0) -> bool:
    global _mutex
    if not config.IS_WIN:
        return True
    import ctypes
    end = time.time() + wait
    while True:
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SabangnetBot_single")
        if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            _mutex = h
            return True
        ctypes.windll.kernel32.CloseHandle(h)
        if time.time() >= end:
            return False
        time.sleep(1)


def release_single_instance() -> None:
    global _mutex
    if _mutex and config.IS_WIN:
        import ctypes
        ctypes.windll.kernel32.ReleaseMutex(_mutex)
        ctypes.windll.kernel32.CloseHandle(_mutex)
        _mutex = None


def show_flag() -> Path:
    return config.local_dir() / "show.flag"


def set_autostart(on: bool) -> None:
    if not config.IS_WIN:
        return
    import winreg
    lc = config.load_local()
    # 원드라이브가 늦게 켜져도 되도록 이 PC에 복사된 실행기(launcher.exe)를 등록
    local_launcher = config.local_bin_dir() / "launcher.exe"
    target = local_launcher if local_launcher.exists() else Path(lc.src_exe or sys.executable)
    value = f'"{target}" --tray' + (f' --src "{lc.src_exe}"' if lc.src_exe else "")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, config.APP_NAME, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(k, config.APP_NAME)
            except FileNotFoundError:
                pass
    lc.autostart = on
    config.save_local(lc)


def autostart_enabled() -> bool:
    if not config.IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, config.APP_NAME)
            return True
    except OSError:
        return False


def _spawn(exe: Path, args: list[str]) -> None:
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), *args], env=env, creationflags=flags, close_fds=True)


DEFAULT_UPDATE_URL = "https://github.com/MULTIPIPE/sabangnet-bot/releases/latest/download"


def update_url() -> str:
    """인터넷 업데이트 주소 (옆 폴더 app\\update.json 으로 바꿀 수 있음, "" 이면 안 봄)."""
    try:
        d = json.loads((config.shared_dir() / "app" / "update.json").read_text(encoding="utf-8"))
        return str(d.get("url", "")).rstrip("/")
    except Exception:
        return DEFAULT_UPDATE_URL


def manifest() -> dict | None:
    """옆 폴더 app\\manifest.json (폴더 배포용)."""
    try:
        return json.loads((config.shared_dir() / "app" / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def remote_manifest() -> dict | None:
    url = update_url()
    if not url:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(f"{url}/manifest.json", headers={"User-Agent": "sabangnet-bot", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.info(f"인터넷 업데이트 확인 실패: {str(e)[:80]}")
        return None


def best_manifest() -> dict | None:
    """폴더·인터넷 중 버전이 높은 쪽 (같으면 폴더)."""
    cands = [m for m in (manifest(), remote_manifest()) if m]
    if not cands:
        return None
    return max(cands, key=lambda m: _ver(m.get("version", "")))


def newer_source() -> Path | None:
    """새 버전 본체가 있으면 실행기(런처) 경로를 돌려준다 (런처가 받아서 바꿔 끼움)."""
    if not (config.FROZEN and config.IS_WIN):
        return None
    man = best_manifest()
    lc = config.load_local()
    if not man or not lc.src_exe or not Path(lc.src_exe).exists():
        return None
    if _ver(man.get("version", "")) <= _ver(config.VERSION):
        return None
    want = f"core_{man['version']}_{man['sha256'][:10]}"
    if Path(sys.executable).stem == want:
        return None
    log.info(f"새 버전 발견: v{man['version']}")
    return Path(lc.src_exe)


def cleanup_old_bins() -> None:
    if not config.FROZEN:
        return
    cur = Path(sys.executable).resolve()
    for f in config.local_bin_dir().glob("core_*.exe"):
        if f.resolve() != cur:
            try:
                f.unlink()
            except OSError:
                pass


# ------------------------------------------------------------------ 스케줄러
def _hm(t: str) -> dt.time:
    h, m = t.strip().split(":")
    return dt.time(int(h), int(m))


def _parse(ts: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts) if ts else None
    except Exception:
        return None


class Runner:
    """작업은 한 번에 하나만."""

    def __init__(self, on_done=None):
        self.thread: threading.Thread | None = None
        self.current = ""
        self.on_done = on_done

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, name: str, fn, *args) -> bool:
        if self.busy:
            return False
        self.current = name

        def work():
            res = None
            try:
                res = fn(*args)
            except Exception as e:
                log.error(traceback.format_exc())
                res = {"ok": False, "summary": str(e)}
            finally:
                self.current = ""
                if self.on_done:
                    try:
                        self.on_done(name, res)
                    except Exception:
                        pass
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True


def _ver(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v))
    except Exception:
        return ()


def early_run(st: dict, due: dt.datetime) -> bool:
    """오늘 기록이 정해진 시각(due) 전에 시작된 수동 실행인지.
    (오전에 [지금 발주 실행]을 눌렀다고 14:10 정시 작업이 빠지면 안 됨)"""
    started = _parse(st.get("started"))
    if not started:
        return False
    if st.get("trigger") in ("manual", "cli", "remote"):
        return started < due
    # 정시 실행이었는데 그 뒤 실행 시각을 늦게 바꾼 경우(30분 넘게 차이) → 새 시각에 한 번 더 (10/6: 11:38로 당겨 돌린 뒤 14:10 복구)
    return started < due - dt.timedelta(minutes=30)


def order_deadline_passed(s: config.Settings, now: dt.datetime | None = None) -> bool:
    """발주 시각 < 출고마감일 때, 출고마감이 지났는지. (지났으면 자동 발주는 오늘 안 돔)"""
    now = now or dt.datetime.now()
    try:
        if _hm(s.order_time) >= _hm(s.release_deadline):
            return False  # 사장님이 일부러 마감 뒤로 잡은 경우는 막지 않음
        return now.time() >= _hm(s.release_deadline)
    except Exception:
        return False


class Scheduler(threading.Thread):
    def __init__(self, runner: Runner, on_update_ready=None):
        super().__init__(daemon=True)
        self.runner = runner
        self.stop = threading.Event()
        self.on_update_ready = on_update_ready
        self._last_update_check = 0.0
        self._reminded = set()

    def run(self):
        time.sleep(15)  # 부팅 직후 네트워크 안정 대기
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error(traceback.format_exc())
            self.stop.wait(20)

    def due_time(self, s: config.Settings, t: str) -> dt.datetime:
        lc = config.load_local()
        d = dt.datetime.combine(dt.date.today(), _hm(t))
        return d + dt.timedelta(minutes=5 if int(lc.pc_priority) == 2 else 0)

    def should_run(self, job: str, due: dt.datetime, s: config.Settings) -> bool:
        now = dt.datetime.now()
        if now < due:
            return False
        st = records.job_state(job)
        status = st.get("status")
        if status in ("done", "failed") and early_run(st, due):
            # 정해진 시각 전에 [지금 실행]으로 돌린 기록 → 정해진 시각 작업은 따로 한 번 돈다
            if job == "order" and order_deadline_passed(s, now):
                return False
            records.update_job(job, status="", attempts=0, error="",
                               summary=f"{st.get('summary', '')} (정해진 시각 전 수동 실행 — 정시 작업은 따로 실행)")
            return True
        if status in ("done", "awaiting_approval", "skipped"):
            return False
        if job == "order" and order_deadline_passed(s, now):
            # 출고 마감이 지났으면 뒤늦게 돌지 않음 → 다음 평일 발주 때 같이 처리 (출고일은 똑같음)
            if status != "running":
                self.mark_missed(st, s)
            return False
        if status == "running":
            hb = _parse(st.get("heartbeat")) or _parse(st.get("updated"))
            if st.get("pc") != config.pc_name() and hb and now - hb < dt.timedelta(minutes=20):
                return False  # 다른 PC가 돌리는 중
            if st.get("pc") == config.pc_name() and self.runner.busy:
                return False
        if status == "failed":
            if int(st.get("attempts", 0)) >= int(s.max_attempts):
                return False
            last = _parse(st.get("finished")) or _parse(st.get("updated"))
            if last and now - last < dt.timedelta(minutes=int(s.retry_minutes)):
                return False
        return True

    def mark_missed(self, st: dict, s: config.Settings) -> None:
        """오늘 발주를 마감 전에 못 돌렸을 때: 한 번만 알리고, 오늘은 더 안 돔."""
        key = f"missed-{dt.date.today()}"
        if key in self._reminded:
            return
        self._reminded.add(key)
        if not st.get("status"):
            records.update_job("order", status="skipped",
                               summary=f"출고마감({s.release_deadline}) 전에 PC가 꺼져 있어 오늘은 건너뜀 → 다음 평일에 같이 처리")
            notify.toast("오늘 발주 자동화 건너뜀",
                         f"{s.order_time}~{s.release_deadline} 사이에 PC가 꺼져 있었습니다. "
                         f"다음 평일 {s.order_time}에 밀린 주문까지 같이 처리합니다. 급하면 [지금 발주 실행]을 누르세요.",
                         urgent=True)
        elif st.get("status") == "failed":
            notify.toast("오늘 발주 실패 — 재시도 중단",
                         f"출고마감({s.release_deadline})이 지나 재시도를 멈췄습니다. 로그를 확인해주세요.", urgent=True)

    def remote_command(self) -> bool:
        """원드라이브 `사방넷봇\\명령\\` 폴더에 `발주.txt` / `송장.txt` / `점검.txt` 가 생기면 그 작업을 바로 실행.
        (10/6 사장님 승인: Claude가 사장님 손 빌리지 않고 봇을 돌리기 위한 것)
        - 파일 내용에 PC 이름이 있으면 그 PC만, 비어 있으면 1순위 PC만 실행
        - 실행 직전에 파일을 지움 → 두 PC가 같이 돌거나 두 번 도는 일 없음
        - 실행기록에는 trigger='remote'로 남음 (정시 작업은 그와 별개로 돈다)"""
        d = config.shared_dir() / "명령"
        if not d.exists():
            return False
        table = {"발주": lambda: jobs.run_order("remote"), "송장": lambda: jobs.run_invoice("remote"), "점검": jobs.run_check}
        for name, fn in table.items():
            f = d / f"{name}.txt"
            if not f.exists():
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # 내용: 비어 있음(1순위 PC) / PC이름 / "pc=이름" / "min=버전"(그 버전 이상인 봇만 실행 — 업데이트 전 봇이 먼저 집어가지 않게)
            target, min_ver = "", ""
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("pc="):
                    target = line[3:].strip()
                elif line.lower().startswith("min="):
                    min_ver = line[4:].strip()
                else:
                    target = line
            if min_ver and _ver(config.VERSION) < _ver(min_ver):
                continue
            me = config.pc_name().upper()
            if target and target.upper() != me:
                continue
            if not target and int(config.load_local().pc_priority or 1) != 1:
                continue
            try:
                f.unlink()
            except OSError:
                continue  # 다른 PC가 먼저 가져감
            log.info(f"원격 명령 실행: {name}")
            notify.toast("원격 명령", f"{name} 작업을 시작합니다.")
            return self.runner.start(name, fn)
        return False

    def tick(self):
        if not self.runner.busy and config.load_secrets().mini_ready():
            try:
                if self.remote_command():
                    return
            except Exception:
                log.warning(traceback.format_exc())
        # 새 버전 확인 (10분마다, 쉬고 있을 때만)
        if not self.runner.busy and time.time() - self._last_update_check > 600:
            self._last_update_check = time.time()
            try:
                nxt = newer_source()
                if nxt and self.on_update_ready:
                    self.on_update_ready(nxt)
                    return
            except Exception:
                log.warning(traceback.format_exc())
        if self.runner.busy:
            return
        if not config.load_secrets().mini_ready():
            return  # 계정 입력 전
        s = config.load_settings()
        today = dt.date.today()
        if today.weekday() not in [int(x) for x in s.weekdays]:
            return
        # 출고요청 승인 대기: '확인받기'를 끈 상태면 알아서 출고요청, 켜져 있으면 마감 전 리마인드
        st = records.job_state("order")
        if st.get("status") == "awaiting_approval" and not s.approve_before_release:
            pos = [p.get("po_no") for p in (st.get("pending") or []) if p.get("po_no")]
            if pos and st.get("pc", config.pc_name()) == config.pc_name():
                log.info(f"승인 대기 {len(pos)}건 → 확인받기 꺼짐 → 자동 출고요청")
                self.runner.start("출고요청", jobs.run_release, pos)
                return
        if st.get("status") == "awaiting_approval":
            remind_at = dt.datetime.combine(today, _hm(s.release_deadline)) - dt.timedelta(minutes=10)
            key = f"remind-{today}"
            if dt.datetime.now() >= remind_at and key not in self._reminded:
                self._reminded.add(key)
                notify.toast("출고요청 승인 대기 중", f"출고마감 {s.release_deadline} 전입니다. 봇 창에서 [출고요청 실행]을 눌러주세요.", urgent=True)
        if self.should_run("order", self.due_time(s, s.order_time), s):
            self.runner.start("발주", jobs.run_order, "schedule")
            return
        if s.invoice_enabled and self.should_run("invoice", self.due_time(s, s.invoice_time), s):
            self.runner.start("송장", jobs.run_invoice, "schedule")


def missed_days_notice() -> None:
    """직전 평일에 발주 작업이 안 돌았으면 알림."""
    s = config.load_settings()
    d = dt.date.today()
    for _ in range(7):
        d -= dt.timedelta(days=1)
        if d.weekday() in [int(x) for x in s.weekdays]:
            break
    else:
        return
    from .mini import parse_dt
    c = parse_dt(s.cutover)
    if not c or c.date() > d:
        return
    st = records.job_state("order", records.today_str(d))
    if st.get("status") not in ("done", "skipped"):  # skipped는 그날 이미 알림
        notify.toast("지난 발주 자동화 미실행", f"{d.month}/{d.day}({WEEK[d.weekday()]}) 발주 작업이 완료되지 않았습니다. "
                     f"오늘 실행 때 최근 주문을 같이 수집합니다.", urgent=True)


# ------------------------------------------------------------------ 진입점
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--tray", action="store_true")
    ap.add_argument("--src", default="")
    ap.add_argument("--updated", action="store_true")
    ap.add_argument("--run", choices=["order", "check", "release", "invoice"], default=None)
    ap.add_argument("--po", default="")
    ap.add_argument("--shared", default="")
    args, _ = ap.parse_known_args(argv[1:])

    if args.shared:
        config.set_shared_dir(Path(args.shared))
    if args.src:
        lc = config.load_local()
        lc.src_exe = args.src
        config.save_local(lc)

    records.setup_app_logging()
    notify.icon_path()
    log.info(f"시작 v{config.VERSION} args={argv[1:]} exe={sys.executable}")

    if args.run:
        fn = {"order": lambda: jobs.run_order("cli"), "check": jobs.run_check,
              "release": lambda: jobs.run_release([x for x in args.po.split(",") if x]),
              "invoice": jobs.run_invoice}[args.run]
        res = fn()
        print(res.get("summary"))
        return 0 if res.get("ok") else 1

    if not acquire_single_instance(wait=20 if args.updated else 0):
        # 이미 떠 있으면 그 창을 보여달라고 신호만 남김
        show_flag().write_text("1")
        return 0
    if args.updated:
        notify.toast("사방넷 봇 업데이트", f"새 버전(v{config.VERSION})으로 바뀌었습니다.")
    if config.FROZEN and autostart_enabled():
        try:
            set_autostart(True)  # 자동 실행 경로를 지금 버전으로 갱신
        except Exception:
            pass
    try:
        from .gui import run_gui
        run_gui(start_hidden=args.tray, updated=args.updated)
    finally:
        release_single_instance()
    return 0
