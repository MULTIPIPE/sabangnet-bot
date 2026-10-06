"""빌드 결과를 배포용으로 묶는다.

python3 package.py <출력폴더>
  <출력폴더>/app/core.001…  manifest.json   ← 폴더 배포(사장님 원드라이브 app\\) 와 깃허브 릴리스 자산 공용
  <출력폴더>/사방넷봇.exe                     ← 실행기 (dist/launcher.exe 가 있을 때)
  <출력폴더>/사방넷봇.zip                     ← 수강생 배포용: 폴더 '사방넷봇' 안에 실행기 하나
조각은 9MB (깃허브 업로드 도구 한도 10MB)
"""
import hashlib, json, sys, time, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from sbbot.config import VERSION

out = Path(sys.argv[1]); app = out / "app"; app.mkdir(parents=True, exist_ok=True)
for f in app.glob("core.*"):
    f.unlink()
core = Path("dist/SabangnetBot.exe").read_bytes()
CH = 9 * 1024 * 1024
parts = []
for i in range(0, len(core), CH):
    name = f"core.{len(parts)+1:03d}"
    (app / name).write_bytes(core[i:i+CH]); parts.append(name)
man = {"version": VERSION, "sha256": hashlib.sha256(core).hexdigest(), "size": len(core), "parts": parts,
       "built": time.strftime("%Y-%m-%d %H:%M")}
(app / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
launcher = Path("dist/launcher.exe")
if launcher.exists():
    (out / "사방넷봇.exe").write_bytes(launcher.read_bytes())
    with zipfile.ZipFile(out / "사방넷봇.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(launcher, "사방넷봇/사방넷봇.exe")
print(man)
