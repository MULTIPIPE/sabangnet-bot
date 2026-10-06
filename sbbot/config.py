"""경로·설정·계정 저장.

- 공유 설정(설정.json)은 원드라이브 폴더(exe가 있는 폴더)에 둔다 → 노트북·회사PC가 같이 씀
- 계정 정보는 PC마다 따로, 윈도우 DPAPI로 암호화해서 %LOCALAPPDATA%\\사방넷봇 에 둔다 (원드라이브에 안 올라감)
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "사방넷봇"
APP_TITLE = "멀티파이프 사방넷 발주 자동화"
VERSION = "0.4.0"

IS_WIN = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)


# ---------------------------------------------------------------- 경로
def local_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_bin_dir() -> Path:
    p = local_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def chrome_profile_dir() -> Path:
    p = local_dir() / "chrome-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


_shared_override: Path | None = None


def set_shared_dir(p: Path) -> None:
    global _shared_override
    _shared_override = Path(p)


def shared_dir() -> Path:
    """원드라이브 공유 폴더 (원본 exe가 있는 폴더)."""
    if _shared_override is not None:
        p = _shared_override
    else:
        lc = load_local()
        if lc.src_exe and Path(lc.src_exe).exists():
            p = Path(lc.src_exe).parent
        elif FROZEN:
            p = Path(sys.executable).parent
        else:
            p = Path(__file__).resolve().parent.parent / "shared"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_root() -> Path:
    p = shared_dir() / "로그"
    p.mkdir(parents=True, exist_ok=True)
    return p


def records_dir() -> Path:
    p = shared_dir() / "실행기록"
    p.mkdir(parents=True, exist_ok=True)
    return p


def pc_name() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


# ---------------------------------------------------------------- 공유 설정
@dataclass
class Settings:
    order_time: str = "14:10"          # 발주·출고요청 시각
    invoice_time: str = "18:00"        # 송장 전송 시각 (2차 버전)
    invoice_enabled: bool = True       # 송장 자동전송 (풀필먼트 → 미니 → 쇼핑몰)
    weekdays: list = field(default_factory=lambda: [0, 1, 2, 3, 4])  # 월=0 ... 일=6
    release_deadline: str = "15:00"    # 3PL 출고 마감 (지나서 돌면 알림)
    collect_days: int = 6              # 미니 주문수집 기간(일)
    cutover: str = ""                  # 자동화 시작일(YYYY-MM-DD). 이 날짜 전에 수집된 주문은 건드리지 않음
    approve_before_release: bool = True  # 출고요청 직전에 사장님 확인 받기 (처음 며칠 권장)
    mini_decide: bool = True           # 미니 주문확정 처리
    mini_status_confirm: bool = True   # 미니 주문상태 '주문확인' 처리
    max_orders_per_run: int = 100      # 한 번에 이보다 많으면 멈추고 알림 (주문 밀릴 때 대비 100)
    show_browser: bool = False         # 브라우저 창 보이기 (점검용)
    retry_minutes: int = 15            # 실패 시 재시도 간격
    max_attempts: int = 3
    mini_admin_url: str = "https://sbadmin01.sbmini.co.kr"
    mini_login_url: str = "https://www.sbmini.co.kr/login/login-main"
    fulfill_url: str = "https://wms02.sbfulfillment.co.kr"
    schema: int = 2                    # 설정 파일 버전 (예전 기본값 자동 갱신용)


def settings_path() -> Path:
    return shared_dir() / "설정.json"


def load_settings() -> Settings:
    p = settings_path()
    s = Settings()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Settings)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
            if int(data.get("schema", 1)) < 2:  # v0.2 이전 기본값 갱신
                s.invoice_enabled = True
                if int(s.max_orders_per_run) < 100:
                    s.max_orders_per_run = 100
                s.schema = 2
        except Exception:
            pass
    return s


def save_settings(s: Settings) -> None:
    p = settings_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- PC별 설정
@dataclass
class LocalConfig:
    src_exe: str = ""        # 원드라이브에 있는 원본 exe 경로
    pc_priority: int = 1     # 1순위: 정시 실행 / 2순위: 5분 늦게 (다른 PC가 이미 했으면 건너뜀)
    autostart: bool = False


def local_config_path() -> Path:
    return local_dir() / "local.json"


def load_local() -> LocalConfig:
    p = local_config_path()
    c = LocalConfig()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(c, k):
                    setattr(c, k, v)
        except Exception:
            pass
    return c


def save_local(c: LocalConfig) -> None:
    local_config_path().write_text(json.dumps(asdict(c), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 계정 (DPAPI 암호화)
@dataclass
class Secrets:
    mini_id: str = ""
    mini_pw: str = ""
    mini_otp: str = ""        # 구글 OTP 비밀키 (등록 화면에 나오는 글자)
    ff_company: str = ""      # 풀필먼트 회사코드
    ff_id: str = ""
    ff_pw: str = ""

    def mini_ready(self) -> bool:
        return bool(self.mini_id and self.mini_pw)

    def ff_ready(self) -> bool:
        return bool(self.ff_company and self.ff_id and self.ff_pw)


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if not IS_WIN:
        return data  # 개발용(리눅스)에서는 그대로
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI 실패")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def secrets_path() -> Path:
    return local_dir() / "secrets.bin"


def load_secrets() -> Secrets:
    p = secrets_path()
    s = Secrets()
    if p.exists():
        try:
            raw = _dpapi(base64.b64decode(p.read_bytes()), encrypt=False)
            data = json.loads(raw.decode("utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception:
            pass
    return s


def save_secrets(s: Secrets) -> None:
    raw = json.dumps(asdict(s), ensure_ascii=False).encode("utf-8")
    secrets_path().write_bytes(base64.b64encode(_dpapi(raw, encrypt=True)))
