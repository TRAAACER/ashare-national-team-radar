"""一键构建 Win64 免安装便携版 Release 包（本地与 GitHub Actions 通用）."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.icon_gen import ensure_ico_file


USAGE_TXT = """================================================================
  A股国家队宽基 ETF 动向监控雷达 (GJDJK) · Win11 免安装便携版
================================================================

【快速上手】
1. 请先将整个压缩包完整解压到一个固定英文/中文目录（例如 D:\\GJDJK），
   切勿在压缩包预览窗口里直接双击运行！
2. 双击「GJDJK.exe」即可打开独立桌面看板（无需安装 Python）：
   - 首次运行会自动回填近 1 年历史行情与沪深官方份额（约 1~2 分钟）；
   - 点击右上角 × 关闭窗口即彻底退出，零后台常驻、不占内存。
3. 推荐开启每晚 22:30 自动盘后巡检（二选一）：
   - 方式 A：双击本目录的「开启晚间自动巡检.bat」；
   - 方式 B：打开 GJDJK.exe → 左侧「提醒与设置」→ 开启每日定时巡检并保存。
   （每晚 22:30 交易所公布官方份额后静默核实几秒即退出；若当晚关机错过，
    下次开机联网后自动补跑；仅在有核实异动或接口异常时弹大号提醒卡片。）

【个人数据与升级】
- 所有历史数据与个人复盘笔记均保存在本目录的 data\\gjdjk.db 中；
- 升级新版本时，只需保留旧版的 data\\gjdjk.db 覆盖到新目录即可无缝延续。
- 如需自定义宽基监控池，可直接编辑本目录的 config\\watchlist.yaml。

【免责声明】
本工具所有数据均来自上交所、深交所及公开财经接口，模型估算仅用于数据
监测与个人复盘研究，不构成任何投资建议。

项目主页：https://github.com/TRAAACER/ashare-national-team-radar
"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_release(version: str) -> Path:
    ver = version.strip() or "v3.1.1"
    ico_path = ensure_ico_file()

    dist_dir = ROOT_DIR / "dist"
    build_dir = ROOT_DIR / "build"
    pkg_name = f"ashare-national-team-radar-{ver}-win64"
    raw_out = dist_dir / "GJDJK"
    pkg_dir = dist_dir / pkg_name
    zip_path = dist_dir / f"{pkg_name}.zip"

    for p in (raw_out, pkg_dir):
        if p.exists():
            shutil.rmtree(p)
    if zip_path.exists():
        zip_path.unlink()

    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "GJDJK",
        "--icon",
        str(ico_path),
        "--add-data",
        f"{ROOT_DIR / 'config' / 'watchlist.yaml'};config",
        "--add-data",
        f"{ROOT_DIR / 'web'};web",
        "--collect-data",
        "akshare",
        "--collect-all",
        "webview",
        "--collect-data",
        "certifi",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols",
        "--hidden-import",
        "uvicorn.protocols.http",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "uvicorn.lifespan.off",
        "--hidden-import",
        "clr",
        "--hidden-import",
        "pythonnet",
        "--hidden-import",
        "clr_loader",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "scipy",
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "IPython",
        "--exclude-module",
        "notebook",
        "--exclude-module",
        "pytest",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        str(ROOT_DIR / "app.py"),
    ]

    print(f"[1/4] Building PyInstaller bundle for {ver}...")
    subprocess.run(pyinstaller_cmd, cwd=str(ROOT_DIR), check=True)
    if not (raw_out / "GJDJK.exe").exists():
        raise RuntimeError(f"Expected EXE not found at {raw_out / 'GJDJK.exe'}")

    print(f"[2/4] Assembling portable package directory {pkg_dir.name}...")
    raw_out.rename(pkg_dir)

    # 在 .exe 同级放置一份可编辑的 config/watchlist.yaml 与中文快捷脚本
    ext_cfg_dir = pkg_dir / "config"
    ext_cfg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT_DIR / "config" / "watchlist.yaml", ext_cfg_dir / "watchlist.yaml")
    shutil.copy2(ROOT_DIR / "LICENSE", pkg_dir / "LICENSE")
    shutil.copy2(ROOT_DIR / "README.md", pkg_dir / "README.md")

    (pkg_dir / "开启晚间自动巡检.bat").write_text(
        '@echo off\r\nstart "" "%~dp0GJDJK.exe" --enable-schedule --schedule-time 22:30\r\n',
        encoding="utf-8",
    )
    (pkg_dir / "关闭自动巡检.bat").write_text(
        '@echo off\r\nstart "" "%~dp0GJDJK.exe" --disable-schedule\r\n',
        encoding="utf-8",
    )
    (pkg_dir / "使用说明.txt").write_text(USAGE_TXT, encoding="utf-8")

    # 3. 实机冒烟测试独立 EXE（绝不发坏包！）
    print("[3/4] Running standalone EXE --self-test verification...")
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "selftest_ok.txt"
        res = subprocess.run(
            [str(pkg_dir / "GJDJK.exe"), "--self-test", "--self-test-out", str(marker)],
            cwd=str(pkg_dir),
            timeout=60,
        )
        if res.returncode != 0 or not marker.exists() or "SELF_TEST_OK" not in marker.read_text(encoding="utf-8"):
            raise RuntimeError(f"Standalone EXE self-test FAILED! exit={res.returncode}")
    print("      Standalone EXE --self-test PASSED!")

    # 确保打包目录干净（未残留测试 data/）
    test_data = pkg_dir / "data"
    if test_data.exists():
        shutil.rmtree(test_data)

    # 4. 打包成标准 ZIP 并生成 SHA256
    print(f"[4/4] Creating release archive {zip_path.name}...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in sorted(pkg_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(dist_dir).as_posix()
                zf.write(file_path, arcname)

    sha = _sha256_file(zip_path)
    (dist_dir / "SHA256SUMS.txt").write_text(f"{sha}  {zip_path.name}\n", encoding="utf-8")
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"DONE: {zip_path} ({size_mb:.1f} MB, SHA256={sha[:16]}...)")
    return zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v3.1.1", help="Release tag version, e.g. v3.1.1")
    args = parser.parse_args()
    build_release(args.version)
