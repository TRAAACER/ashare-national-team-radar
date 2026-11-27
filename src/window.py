"""独立 Win11 桌面应用窗口启动器（优先 pywebview WebView2，无缝降级 Edge --app）."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import DATA_DIR, ROOT_DIR


def _find_edge_exe() -> str | None:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def launch_desktop_window(url: str) -> subprocess.Popen | None:
    """
    以独立进程方式打开 Win11 桌面窗口：
    - 避免把 WebView2 / Chromium 内存常驻在托盘进程中；
    - 关闭窗口即释放界面内存，后台托盘继续静默守护。
    """
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = str(pyw if pyw.exists() else exe)
    try:
        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT_DIR)
        return subprocess.Popen(
            [runner, "-m", "src.window", url],
            cwd=str(ROOT_DIR),
            env=env,
            creationflags=creationflags,
        )
    except Exception:
        return launch_edge_fallback(url)


def launch_edge_fallback(url: str) -> subprocess.Popen | None:
    edge = _find_edge_exe()
    if edge:
        profile_dir = DATA_DIR / "edge_app_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(
            [
                edge,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--window-size=1360,860",
            ]
        )
    webbrowser.open(url)
    return None


def _run_webview_main(url: str) -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        log_fp = open(log_dir / "window.log", "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log_fp
        if sys.stderr is None:
            sys.stderr = log_fp
    try:
        import webview

        webview.create_window(
            title="国家队动向监控 · GJDJK",
            url=url,
            width=1360,
            height=860,
            min_size=(980, 660),
            background_color="#F4F6FA",
            text_select=True,
        )
        webview.start(gui="edgechromium", debug=False)
    except Exception as e:
        logging.basicConfig(filename=str(log_dir / "window.log"), level=logging.INFO)
        logging.exception("pywebview failed, falling back to Edge --app: %s", e)
        launch_edge_fallback(url)


if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18688"
    _run_webview_main(target_url)
