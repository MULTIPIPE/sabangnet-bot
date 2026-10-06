"""윈도우 알림 + 아이콘."""
from __future__ import annotations

import logging
from pathlib import Path

from . import config

_icon_cache: Path | None = None


def icon_image(size: int = 64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=size // 5, fill=(37, 99, 235, 255))
    # 상자 모양
    s = size
    d.polygon([(s*0.22, s*0.40), (s*0.50, s*0.26), (s*0.78, s*0.40), (s*0.50, s*0.54)], fill=(255, 255, 255, 255))
    d.polygon([(s*0.22, s*0.44), (s*0.48, s*0.58), (s*0.48, s*0.80), (s*0.22, s*0.66)], fill=(219, 234, 254, 255))
    d.polygon([(s*0.52, s*0.58), (s*0.78, s*0.44), (s*0.78, s*0.66), (s*0.52, s*0.80)], fill=(191, 219, 254, 255))
    return img


def icon_path() -> Path:
    global _icon_cache
    if _icon_cache and _icon_cache.exists():
        return _icon_cache
    p = config.local_dir() / "icon.ico"
    try:
        icon_image(256).save(p, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    except Exception:
        pass
    png = config.local_dir() / "icon.png"
    try:
        icon_image(128).save(png)
    except Exception:
        pass
    _icon_cache = p
    return p


AUMID = "SabangnetBot.Automation"
_aumid_ok: bool | None = None


def _register_aumid() -> bool:
    """알림에 '사방넷 자동화 봇' 이름/아이콘이 뜨도록 앱 ID 등록 (관리자 권한 불필요)."""
    global _aumid_ok
    if _aumid_ok is not None:
        return _aumid_ok
    try:
        import winreg
        icon_path()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{AUMID}") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, config.APP_TITLE)
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(config.local_dir() / "icon.png"))
        _aumid_ok = True
    except Exception:
        _aumid_ok = False
    return _aumid_ok


def toast(title: str, msg: str, urgent: bool = False) -> None:
    logging.getLogger("app").info(f"[알림] {title} | {msg}")
    if not config.IS_WIN:
        print(f"[알림] {title}: {msg}")
        return
    try:
        from winotify import Notification, audio
        app_id = AUMID if _register_aumid() else r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
        n = Notification(app_id=app_id, title=title, msg=msg[:250],
                         icon=str(config.local_dir() / "icon.png"),
                         duration="long" if urgent else "short")
        n.set_audio(audio.Default if urgent else audio.Silent, loop=False)
        n.show()
    except Exception as e:  # 알림 실패는 치명적이지 않음
        logging.getLogger("app").warning(f"알림 실패: {e}")
