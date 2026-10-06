"""실행기록(원드라이브 공유) · 처리내역 · 로그.

- 실행기록/YYYY-MM-DD.json : 오늘 어떤 PC가 무슨 작업을 했는지 → 다른 PC가 중복 실행 안 하게
- 처리내역.csv             : 주문별로 어디까지 처리했는지 → 같은 주문 두 번 출고요청 방지
- 로그/YYYY-MM-DD/시각_작업/ : 단계별 화면 캡처·로그 (문제 생기면 이걸 보고 고침)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import shutil
import zipfile
import threading
import time
from pathlib import Path

from . import config

_lock = threading.Lock()


def now() -> dt.datetime:
    return dt.datetime.now()


def today_str(d: dt.date | None = None) -> str:
    return (d or dt.date.today()).isoformat()


# ---------------------------------------------------------------- 실행기록
def _record_path(day: str) -> Path:
    return config.records_dir() / f"{day}.json"


def read_day(day: str | None = None) -> dict:
    p = _record_path(day or today_str())
    if not p.exists():
        return {}
    # 원드라이브가 올리는 중이면 잠깐 못 읽을 수 있음 → 몇 번 다시 시도 (빈 기록으로 착각하면 안 됨)
    for i in range(10):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.5)
    return {}


def _replace(tmp: Path, p: Path) -> None:
    """원드라이브가 파일을 잡고 있으면(동기화 중) 바꿔쓰기가 거부될 수 있음 → 최대 15초 재시도.
    (10/2 14:10:07 정시 발주 첫 시도가 기록 쓰기에서 멈춘 것으로 보임 — 20초 뒤 재시도로 정상 진행)"""
    for i in range(30):
        try:
            os.replace(tmp, p)
            return
        except OSError:
            time.sleep(0.5)
    os.replace(tmp, p)


def update_job(job: str, day: str | None = None, **kv) -> dict:
    """실행기록의 job 항목을 갱신 (원자적 쓰기)."""
    day = day or today_str()
    with _lock:
        data = read_day(day)
        cur = data.get(job, {})
        cur.update(kv)
        cur["pc"] = kv.get("pc", cur.get("pc") or config.pc_name())
        cur["updated"] = now().isoformat(timespec="seconds")
        data[job] = cur
        p = _record_path(day)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _replace(tmp, p)
        return cur


def job_state(job: str, day: str | None = None) -> dict:
    return read_day(day).get(job, {})


# ---------------------------------------------------------------- 처리내역
LEDGER_FIELDS = ["일시", "PC", "단계", "쇼핑몰", "쇼핑몰주문번호", "사방넷주문번호", "발주번호", "비고"]


def ledger_path() -> Path:
    return config.shared_dir() / "처리내역.csv"


def ledger_add(stage: str, mall_order_no: str, mall: str = "", sb_order_no: str = "",
               po_no: str = "", note: str = "") -> None:
    p = ledger_path()
    with _lock:
        new = not p.exists()
        with p.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
            if new:
                w.writeheader()
            w.writerow({
                "일시": now().isoformat(sep=" ", timespec="seconds"), "PC": config.pc_name(),
                "단계": stage, "쇼핑몰": mall, "쇼핑몰주문번호": mall_order_no,
                "사방넷주문번호": sb_order_no, "발주번호": po_no, "비고": note,
            })


def ledger_rows() -> list[dict]:
    p = ledger_path()
    if not p.exists():
        return []
    try:
        with p.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def ledger_orders(stage: str) -> set[str]:
    return {r["쇼핑몰주문번호"] for r in ledger_rows() if r.get("단계") == stage and r.get("쇼핑몰주문번호")}


# ---------------------------------------------------------------- 로그
class RunLog:
    """작업 1회분 로그 폴더. 단계별 캡처를 여기에 저장."""

    def __init__(self, job: str):
        t = now()
        self.dir = config.logs_root() / t.strftime("%Y-%m-%d") / f"{t.strftime('%H%M%S')}_{job}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.lines: list[str] = []
        self.logger = logging.getLogger(f"run.{job}.{t.strftime('%H%M%S')}")
        self.logger.setLevel(logging.INFO)
        fh = logging.FileHandler(self.dir / "log.txt", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        self.logger.addHandler(fh)
        self._fh = fh

    def info(self, msg: str) -> None:
        self.lines.append(f"{now().strftime('%H:%M:%S')} {msg}")
        self.logger.info(msg)
        logging.getLogger("app").info(msg)

    def warn(self, msg: str) -> None:
        self.lines.append(f"{now().strftime('%H:%M:%S')} ⚠ {msg}")
        self.logger.warning(msg)
        logging.getLogger("app").warning(msg)

    def next_name(self, name: str) -> Path:
        self.n += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
        return self.dir / f"{self.n:02d}_{safe}"

    def close(self) -> None:
        self.logger.removeHandler(self._fh)
        self._fh.close()


def cleanup_logs(keep_days: int = 14) -> None:
    root = config.logs_root()
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)
    for d in root.iterdir():
        try:
            if d.is_dir() and dt.date.fromisoformat(d.name) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except ValueError:
            pass


def setup_app_logging() -> None:
    lg = logging.getLogger("app")
    if lg.handlers:
        return
    lg.setLevel(logging.INFO)
    fh = logging.FileHandler(config.local_dir() / "app.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    lg.addHandler(fh)


# ---------------------------------------------------------------- 로그 보내기
def export_logs(days: int = 3) -> Path:
    """최근 days일 로그 폴더(png 포함) + 실행기록 + 처리내역 + 설정.json + app.log → 바탕화면 zip.
    계정/비밀번호(secrets.bin)는 넣지 않음."""
    desk = Path.home() / "Desktop"
    if not desk.exists():
        desk = Path.home()
    out = desk / f"사방넷봇_로그_{config.pc_name()}_{now():%Y%m%d_%H%M}.zip"
    root = config.shared_dir()
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for day_dir in sorted(config.logs_root().glob("20*")):
            if day_dir.is_dir() and day_dir.name >= cutoff:
                for f in day_dir.rglob("*"):
                    if f.is_file():
                        z.write(f, f"로그/{f.relative_to(config.logs_root())}")
        for f in config.records_dir().glob("*.json"):
            if f.stem >= cutoff:
                z.write(f, f"실행기록/{f.name}")
        for name in ("처리내역.csv", "설정.json"):
            f = root / name
            if f.exists():
                z.write(f, name)
        app_log = config.local_dir() / "app.log"
        if app_log.exists():
            z.write(app_log, "app.log")
        launcher_log = config.local_dir() / "launcher.log"
        if launcher_log.exists():
            z.write(launcher_log, "launcher.log")
        info = {"version": config.VERSION, "pc": config.pc_name(), "made": now().isoformat(timespec="seconds"),
                "shared_dir": str(root), "windows": os.environ.get("OS", ""), "user": os.environ.get("USERNAME", "")}
        z.writestr("정보.json", json.dumps(info, ensure_ascii=False, indent=2))
    return out
