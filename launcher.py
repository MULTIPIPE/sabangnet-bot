"""사방넷봇 실행기 (작은 exe, 더블클릭/자동시작 대상).

하는 일
  1) 본체(core) 새 버전이 있는지 두 군데서 확인
     - 옆 폴더 app\\manifest.json  (사장님 원드라이브처럼 폴더로 배포하는 경우)
     - 인터넷(깃허브 릴리스)  (수강생 배포: exe 하나만 있으면 됨)
     둘 중 버전이 높은 쪽을 쓴다.
  2) 조각(core.001…)을 받아 합쳐 이 PC(%LOCALAPPDATA%\\사방넷봇\\bin)에 본체를 만들고(해시 검증) 실행
  3) 인터넷도 폴더도 안 되면 이 PC에 있는 최신 본체로 실행

인터넷 주소는 옆 폴더 app\\update.json 의 {"url": "..."} 로 바꿀 수 있다 ("" 이면 인터넷 확인 안 함).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = "사방넷봇"
DEFAULT_UPDATE_URL = "https://github.com/MULTIPIPE/sabangnet-bot/releases/latest/download"
UA = "sabangnet-bot-launcher"


def msgbox(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "사방넷 자동화 봇", 0x40)
    except Exception:
        pass


def local_dir() -> Path:
    p = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_bin() -> Path:
    p = local_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str) -> None:
    try:
        with open(local_dir() / "launcher.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def ver(v) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))
    except Exception:
        return ()


def read_local_manifest(app_dir: Path, wait: float) -> dict | None:
    """옆 폴더 app\\manifest.json. 폴더가 없으면(수강생 설치) 기다리지 않음."""
    if not app_dir.exists():
        return None
    if not (app_dir / "manifest.json").exists() and not any(app_dir.glob("core.*")):
        return None  # 폴더 배포가 아님(수강생 설치) → 기다릴 것 없음
    end = time.time() + wait
    while True:
        try:
            return json.loads((app_dir / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            if time.time() >= end:
                return None
            time.sleep(5)


def update_url(app_dir: Path) -> str:
    try:
        d = json.loads((app_dir / "update.json").read_text(encoding="utf-8"))
        return str(d.get("url", "")).rstrip("/")
    except Exception:
        return DEFAULT_UPDATE_URL


def fetch(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def read_remote_manifest(url: str) -> dict | None:
    if not url:
        return None
    for _ in range(2):
        try:
            return json.loads(fetch(f"{url}/manifest.json", 15).decode("utf-8"))
        except Exception as e:
            log(f"인터넷 manifest 실패: {e}")
            time.sleep(3)
    return None


def assemble(parts: list[Path], sha256: str, dst: Path) -> bool:
    tmp = dst.with_suffix(".part")
    h = hashlib.sha256()
    with open(tmp, "wb") as fo:
        for part in parts:
            with open(part, "rb") as fi:
                while True:
                    buf = fi.read(1024 * 1024)
                    if not buf:
                        break
                    h.update(buf)
                    fo.write(buf)
    if h.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        log("해시 불일치")
        return False
    os.replace(tmp, dst)
    return True


def build_from_local(app_dir: Path, man: dict, core: Path) -> bool:
    for _ in range(3):
        try:
            if assemble([app_dir / n for n in man["parts"]], man["sha256"], core):
                return True
        except Exception as e:
            log(f"폴더 조각 합치기 실패: {e}")
        time.sleep(10)  # 원드라이브가 조각을 아직 받는 중일 수 있음
    return False


def build_from_remote(url: str, man: dict, core: Path) -> bool:
    dl = local_bin() / "dl"
    dl.mkdir(exist_ok=True)
    parts = []
    try:
        for name in man["parts"]:
            dst = dl / name
            data = None
            for _ in range(3):
                try:
                    data = fetch(f"{url}/{name}", 120)
                    break
                except Exception as e:
                    log(f"다운로드 실패 {name}: {e}")
                    time.sleep(5)
            if data is None:
                return False
            dst.write_bytes(data)
            parts.append(dst)
        ok = assemble(parts, man["sha256"], core)
    except Exception as e:
        log(f"인터넷 본체 만들기 실패: {e}")
        ok = False
    for p in parts:
        try:
            p.unlink()
        except OSError:
            pass
    return ok


def newest_local_core() -> Path | None:
    cores = sorted(local_bin().glob("core_*.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cores[0] if cores else None


def core_path(man: dict) -> Path:
    return local_bin() / f"core_{man['version']}_{man['sha256'][:10]}.exe"


def main() -> int:
    args = sys.argv[1:]
    # --src 로 원본 위치를 넘겨받았으면 그걸 기준으로 (자동 시작 시 로컬 복사본이 실행됨)
    src_launcher = Path(sys.executable).resolve()
    if "--src" in args:
        i = args.index("--src")
        if i + 1 < len(args):
            src_launcher = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    app_dir = src_launcher.parent / "app"
    wait = 300 if "--tray" in args else 20  # 부팅 직후엔 원드라이브를 기다려줌
    url = update_url(app_dir)

    local_man = read_local_manifest(app_dir, wait)
    remote_man = read_remote_manifest(url)
    cands = []
    if local_man:
        cands.append((ver(local_man.get("version")), "local", local_man))
    if remote_man:
        cands.append((ver(remote_man.get("version")), "remote", remote_man))
    cands.sort(key=lambda c: (c[0], c[1] == "local"), reverse=True)  # 버전 높은 쪽, 같으면 폴더 쪽
    log(f"후보: {[(c[1], c[2].get('version')) for c in cands]}")

    core = None
    for _, where, man in cands:
        c = core_path(man)
        if c.exists():
            core = c
            break
        ok = build_from_local(app_dir, man, c) if where == "local" else build_from_remote(url, man, c)
        if ok:
            log(f"본체 준비: {where} v{man.get('version')}")
            core = c
            break
    if core is None:
        core = newest_local_core()
        if core is not None:
            log(f"이 PC의 최신 본체로 실행: {core.name}")
    if core is None:
        msgbox("봇 본체를 받지 못했습니다.\n인터넷 연결(또는 원드라이브 동기화)을 확인한 뒤 다시 실행해주세요.")
        return 1

    # 자동 시작용으로 이 실행기도 이 PC에 복사해 둠
    try:
        me = Path(sys.executable).resolve()
        mine = local_bin() / "launcher.exe"
        if me != mine.resolve() and (not mine.exists() or mine.stat().st_size != me.stat().st_size):
            tmp = mine.with_suffix(".tmp")
            tmp.write_bytes(me.read_bytes())
            os.replace(tmp, mine)
    except Exception:
        pass
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([str(core), *args, "--src", str(src_launcher)], env=env,
                     creationflags=0x00000008 | 0x00000200, close_fds=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
