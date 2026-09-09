"""打发行 zip：release/Quant-He桌面版 -> release/Quant-He桌面版_v{VER}.zip
排除 logs/ 与 outputs/final5（大缓存），保留 factors/risk 产物与 .env。"""
import os, sys, zipfile
from pathlib import Path

VER = sys.argv[1] if len(sys.argv) > 1 else "1.8"
src = Path("release/Quant-He桌面版")
out = Path(f"release/Quant-He桌面版_v{VER}.zip")
n = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(src):
        if "outputs" in Path(root).parts:
            dirs[:] = [d for d in dirs if d in ("factors", "risk")]
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(src)
            if rel.parts[0] == "logs" or rel.parts[:2] == ("outputs", "final5"):
                continue
            z.write(p, rel)
            n += 1
print("文件数", n, "大小 MB", round(out.stat().st_size / 1048576, 1))

z = zipfile.ZipFile(out)
names = z.namelist()
for key in ["_internal/web/static/index.html", "_internal/web/static/app.js",
            ".env", "outputs/risk/risk_report_20260909_0028.csv", "config/combo.yaml"]:
    hit = [x for x in names if x.replace(os.sep, "/").endswith(key)]
    print("OK " if hit else "缺失", key)
print("final5 泄漏:", sum(1 for x in names if "final5" in x))
print("zip 完整:", z.testzip() is None)
