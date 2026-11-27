"""
国家队动向监控 (GJDJK) Win11 零后台常驻主入口。

两种极简生命周期（彻底告别托盘常驻与内存占用）：
  1. pythonw.exe app.py                     # 双击快捷方式打开独立浅色桌面窗，点右上角 × 优雅落盘并彻底关闭全部进程！
  2. pythonw.exe app.py --scheduled-check   # 每晚 21:30 官方份额公布后自动巡检（错过关机则下次开机补跑）→ 仅在有核实结论异动或数据源异常时弹大卡片，无事直接退出(0 MB)！
  3. python app.py --sync-only              # 命令行立即执行一次同步检查并退出
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

import requests
import uvicorn

from src.config import DATA_DIR
from src.db import Database
from src.icon_gen import ensure_ico_file
from src.server import create_app
from src.sync_service import SyncService
from src.window import launch_desktop_window, launch_edge_fallback


def setup_logging() -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        stdio_fp = open(log_dir / "stdio.log", "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = stdio_fp
        if sys.stderr is None:
            sys.stderr = stdio_fp
    log_file = log_dir / "app.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _is_already_running(port: int) -> bool:
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.get(f"http://127.0.0.1:{port}/api/status", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _wait_for_server(port: int, timeout_sec: float = 10.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _is_already_running(port):
            return True
        time.sleep(0.2)
    return False


def _wait_for_network(max_wait_sec: float = 25.0) -> bool:
    """联网快速探活：晚上在线时 0.1 秒立即返回；错过任务刚开机补跑时最多平滑等待 25 秒待网卡就绪."""
    import socket

    deadline = time.time() + max(0.0, max_wait_sec)
    hosts = [("223.5.5.5", 53), ("114.114.114.114", 53), ("hq.sinajs.cn", 80)]
    while True:
        for host, port in hosts:
            try:
                with socket.create_connection((host, port), timeout=1.5):
                    return True
            except OSError:
                continue
        if time.time() >= deadline:
            return False
        time.sleep(1.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股国家队动向监控 Win11 零后台桌面版")
    parser.add_argument(
        "--scheduled-check",
        action="store_true",
        help="每日盘后定时巡检（默认每晚21:30，错过关机则下次开机补跑）：自动联网探活→核实份额与底牌→仅在核实异动或接口异常时弹窗→立刻退出",
    )
    parser.add_argument("--boot-check", action="store_true", help="兼容旧参数，同 --scheduled-check")
    parser.add_argument("--delay", type=int, default=0, help="启动前额外等待秒数（默认 0，改用智能联网探活）")
    parser.add_argument("--sync-only", action="store_true", help="命令行执行一次智能同步并退出")
    parser.add_argument("--force", action="store_true", help="配合 --sync-only 强制全量重刷")
    parser.add_argument("--server-only", action="store_true", help="仅启动本地 Web 服务（调试用）")
    parser.add_argument("--alert-payload", type=str, default="", help="内部参数：以独立进程展示大号置顶提醒卡片")
    parser.add_argument("--enable-schedule", action="store_true", help="注册每晚 21:30 自动定时巡检任务并退出")
    parser.add_argument("--disable-schedule", action="store_true", help="取消每日自动定时巡检任务并退出")
    parser.add_argument("--schedule-time", type=str, default="21:30", help="配合 --enable-schedule 指定巡检时间（默认 21:30）")
    parser.add_argument("--self-test", action="store_true", help="快速自检打包资源、配置与核心模块并退出")
    parser.add_argument("--self-test-out", type=str, default="", help="可选：将自检结果写入指定文件（供无控制台 EXE 验证）")
    args = parser.parse_args()

    # 独立提醒弹窗子进程模式（便携版 EXE 与源码版通用）
    if args.alert_payload:
        from src.alert_dialog import _run_alert_main

        _run_alert_main(args.alert_payload)
        return

    # 打包资源与模块自检模式（不污染正式库）
    if args.self_test:
        import tempfile
        from pathlib import Path
        import akshare  # noqa: F401
        import webview  # noqa: F401
        from src.config import WEB_DIR, load_watchlist
        from src.analyzer import NationalTeamAnalyzer

        cfg = load_watchlist()
        assert len(cfg.get("etfs", [])) >= 30, "watchlist.yaml missing ETFs"
        echarts_file = WEB_DIR / "vendor" / "echarts.min.js"
        assert echarts_file.exists() and echarts_file.stat().st_size > 500_000, f"Missing echarts: {echarts_file}"
        with tempfile.TemporaryDirectory() as td:
            tmp_db = Database(db_path=Path(td) / "selftest.db")
            _ = NationalTeamAnalyzer(tmp_db)
            _ = create_app(tmp_db, SyncService(tmp_db))
        if args.self_test_out:
            Path(args.self_test_out).write_text("SELF_TEST_OK\n", encoding="utf-8")
        if sys.stdout is not None:
            print("SELF_TEST_OK")
        return

    # 快捷开关 Win11 每日定时巡检任务（供便携版 .bat 双击调用）
    if args.enable_schedule or args.disable_schedule:
        from src.autostart import normalize_schedule_time, set_autostart

        enable_flag = bool(args.enable_schedule)
        t_str = normalize_schedule_time(args.schedule_time, "21:30")
        ok, msg = set_autostart(enable_flag, schedule_time=t_str)
        try:
            db_tmp = Database()
            db_tmp.update_settings({"auto_start_enabled": ok if enable_flag else False, "auto_schedule_time": t_str})
        except Exception:
            pass
        if sys.stdout is not None:
            print(msg)
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, msg, "国家队动向监控 · 定时巡检设置", 0x00000040 if ok else 0x00000030)
        return

    setup_logging()
    logger = logging.getLogger("gjdjk.app")
    ensure_ico_file()

    db = Database()
    recovered = db.mark_stale_sync_runs_interrupted()
    if recovered:
        logger.info("Recovered %d interrupted sync_runs record(s) from previous exit.", recovered)

    settings = db.get_settings()
    port = int(settings.get("server_port", 18688))
    url = f"http://127.0.0.1:{port}"
    sync_service = SyncService(db)

    # 模式 A：命令行手动同步
    if args.sync_only:
        res = sync_service.run_smart_sync(trigger="manual", force=args.force)
        logger.info("Sync-only result: %s", res)
        print(res)
        return

    # 模式 B：每日晚间定时巡检 / 错过开机补跑（零常驻！）
    if args.scheduled_check or args.boot_check:
        if args.delay > 0:
            logger.info("Scheduled check waiting %ds...", args.delay)
            time.sleep(args.delay)
        if not _wait_for_network(max_wait_sec=25.0):
            logger.warning("Network not reachable after 25s; attempting sync anyway so health monitor can track.")
        trig = "scheduled" if args.scheduled_check else "boot"
        res = sync_service.run_smart_sync(trigger=trig, force=False)
        logger.info("Scheduled check finished cleanly (exiting with 0 background RAM): %s", res)
        return

    # 模式 C：按需打开独立桌面主窗口（关窗即优雅落盘并彻底退出！）
    if _is_already_running(port):
        launch_desktop_window(url)
        return

    fastapi_app = create_app(db, sync_service)
    uv_config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    uv_server = uvicorn.Server(uv_config)
    server_thread = threading.Thread(target=uv_server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(port, timeout_sec=12.0):
        logger.error("Local server failed to start on port %d", port)
        return

    threading.Thread(
        target=sync_service.run_smart_sync,
        kwargs={"trigger": "open", "force": False},
        daemon=True,
    ).start()

    if args.server_only:
        logger.info("Running in --server-only mode at %s", url)
        try:
            while server_thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            sync_service.request_stop()
            uv_server.should_exit = True
            sync_service.wait_idle(timeout=2.5)
        return

    try:
        import webview

        webview.create_window(
            title="国家队动向监控 · GJDJK",
            url=url,
            width=1380,
            height=880,
            min_size=(980, 660),
            background_color="#F4F6FA",
            text_select=True,
        )
        webview.start(gui="edgechromium", debug=False)
    except Exception as e:
        logger.warning("pywebview main window failed (%s), falling back to Edge --app", e)
        proc = launch_edge_fallback(url)
        if proc:
            proc.wait()
    finally:
        logger.info("Main window closed; waiting briefly for active DB commits before exit.")
        sync_service.request_stop()
        uv_server.should_exit = True
        sync_service.wait_idle(timeout=2.5)
        server_thread.join(timeout=1.0)
        os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
