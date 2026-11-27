"""大号、置顶、必须手动关闭的 Win11 桌面异动提醒卡片（绝不自动消失）."""

from __future__ import annotations

import ctypes
import html
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import DATA_DIR, ROOT_DIR


def show_large_alert_dialog(
    title: str,
    body: str,
    *,
    verdict_code: str = "STRONG_BUY",
    verdict_label: str = "",
    trade_date: str = "",
    total_net_flow_yi: float | None = None,
    nt_est_low_yi: float | None = None,
    nt_est_high_yi: float | None = None,
    confidence: float | None = None,
    group_blocks: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """
    以独立无黑框进程弹出大号置顶提醒卡片，调用方无需阻塞。
    卡片没有自动消失计时器，必须用户手动点“我知道了”或“打开完整看板”或右上角 × 才会关闭。
    """
    payload = {
        "title": title,
        "body": body,
        "verdict_code": verdict_code,
        "verdict_label": verdict_label,
        "trade_date": trade_date,
        "total_net_flow_yi": total_net_flow_yi,
        "nt_est_low_yi": nt_est_low_yi,
        "nt_est_high_yi": nt_est_high_yi,
        "confidence": confidence,
        "group_blocks": group_blocks or [],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="alert_", suffix=".json", dir=str(DATA_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = str(pyw if pyw.exists() else exe)
    try:
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT_DIR)
        subprocess.Popen(
            [runner, "-m", "src.alert_dialog", tmp_path],
            cwd=str(ROOT_DIR),
            env=env,
            creationflags=creationflags,
        )
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _theme_for_verdict(code: str) -> tuple[str, str, str]:
    if code == "HEALTH_ALERT":
        return "#EA580C", "#FFF7ED", "#FDBA74"
    if code in ("STRONG_BUY", "MODERATE_BUY", "PRELIM_RESCUE"):
        return "#DC2626", "#FEF2F2", "#FECACA"
    if code in ("STRONG_SELL", "MODERATE_SELL", "PRELIM_SELL"):
        return "#16A34A", "#F0FDF4", "#BBF7D0"
    if code in ("ROTATION", "ANNOUNCEMENT", "PRELIM_WATCH"):
        return "#2563EB", "#EFF6FF", "#BFDBFE"
    return "#334155", "#F1F5F9", "#CBD5E1"


def _fmt_signed(v: float | None) -> str:
    if v is None:
        return "--"
    return f"{'+' if v > 0 else ''}{v:.1f} 亿"


def _fmt_nt_range(low: float | None, high: float | None, total_net: float | None = None) -> str:
    if low is None or high is None:
        return "待份额披露"
    if low < -0.05 and high > 0.05:
        return f"分歧 {low:+.1f} ~ {high:+.1f} 亿"
    if high <= 0.05 and low < -0.05:
        return f"估卖 {abs(min(0.0, high)):.1f} ~ {abs(low):.1f} 亿"
    if low >= -0.05 and high > 0.05:
        return f"估买 {max(0.0, low):.1f} ~ {high:.1f} 亿"
    return "基本未动"


def _nt_direction(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "flat"
    if low < -0.05 and high > 0.05:
        return "mixed"
    if low >= -0.05 and high > 0.05:
        return "buy"
    if high <= 0.05 and low < -0.05:
        return "sell"
    return "flat"


def _render_alert_html(p: dict[str, Any]) -> str:
    accent, tint, border = _theme_for_verdict(p.get("verdict_code", ""))
    title = html.escape(p.get("title") or "国家队动向提醒")
    body = html.escape(p.get("body") or "").replace("\n", "<br/>")
    v_label = html.escape(p.get("verdict_label") or "动向提醒")
    t_date = html.escape(p.get("trade_date") or "")
    conf = p.get("confidence")
    conf_html = f'<span class="conf-pill">信号强度 {round(conf)}</span>' if conf is not None else ""

    net_val = p.get("total_net_flow_yi")
    nt_low = p.get("nt_est_low_yi")
    nt_high = p.get("nt_est_high_yi")
    nt_str = _fmt_nt_range(nt_low, nt_high, net_val)
    net_str = _fmt_signed(net_val)
    net_color = "#DC2626" if (net_val or 0) > 0.05 else ("#16A34A" if (net_val or 0) < -0.05 else "#0F172A")
    nt_dir = _nt_direction(nt_low, nt_high)
    nt_color = "#DC2626" if nt_dir == "buy" else ("#16A34A" if nt_dir == "sell" else "#475569")

    kpis_html = ""
    if net_val is not None or nt_low is not None:
        kpis_html = f"""
        <div class="kpi-row">
          <div class="kpi">
            <div class="kpi-lbl">宽基全池净申赎</div>
            <div class="kpi-val" style="color:{net_color};">{net_str}</div>
          </div>
          <div class="kpi kpi-hi">
            <div class="kpi-lbl">疑似国家队估算</div>
            <div class="kpi-val" style="color:{nt_color};">{html.escape(nt_str)}</div>
          </div>
        </div>
        """

    blocks = p.get("group_blocks") or []
    active_blocks = [b for b in blocks if b.get("net_flow_yi") is not None and abs(b["net_flow_yi"]) >= 3.0]
    active_blocks.sort(key=lambda x: abs(x.get("net_flow_yi") or 0), reverse=True)
    blocks_html = ""
    if active_blocks:
        chips = []
        for b in active_blocks[:6]:
            nf = b["net_flow_yi"]
            b_dir = _nt_direction(b.get("nt_est_low_yi"), b.get("nt_est_high_yi"))
            c_cls = b_dir if b_dir in ("buy", "sell") else ""
            b_nt = _fmt_nt_range(b.get("nt_est_low_yi"), b.get("nt_est_high_yi"), nf)
            chips.append(
                f'<div class="bchip {c_cls}">'
                f'<div class="bchip-name">{html.escape(b.get("group_name", ""))}</div>'
                f'<div class="bchip-val">{html.escape(b_nt)}</div>'
                f'<div class="bchip-sub">全池 {_fmt_signed(nf)}</div>'
                f"</div>"
            )
        blocks_html = f'<div class="blocks-grid">{"".join(chips)}</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif;
    background: #F6F8FB;
    color: #0F172A;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
    user-select: text;
  }}
  .top-stripe {{ height: 6px; background: {accent}; flex-shrink: 0; }}
  .wrap {{
    flex: 1;
    padding: 18px 22px 14px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .header-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .verdict {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 99px;
    font-size: 13px;
    font-weight: 700;
    background: {tint};
    color: {accent};
    border: 1px solid {border};
  }}
  .conf-pill {{
    padding: 3px 9px;
    border-radius: 99px;
    font-size: 12px;
    font-weight: 600;
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    color: #475569;
  }}
  .date-txt {{ font-size: 13px; color: #64748B; font-weight: 600; margin-left: auto; }}
  h1 {{ font-size: 18px; font-weight: 800; line-height: 1.35; letter-spacing: -0.01em; }}
  .kpi-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .kpi {{
    background: #FFFFFF;
    border: 1px solid #E5EAF2;
    border-radius: 10px;
    padding: 10px 14px;
  }}
  .kpi-hi {{ background: #F8FAFF; border-color: #DBEAFE; }}
  .kpi-lbl {{ font-size: 12px; color: #64748B; font-weight: 600; }}
  .kpi-val {{ font-size: 20px; font-weight: 800; margin-top: 2px; font-variant-numeric: tabular-nums; }}
  .blocks-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
  .bchip {{
    border-radius: 9px;
    padding: 8px 10px;
    border: 1px solid #E2E8F0;
    background: #FFFFFF;
  }}
  .bchip.buy {{ background: #FFF5F5; border-color: #FECACA; }}
  .bchip.sell {{ background: #F0FDF4; border-color: #BBF7D0; }}
  .bchip-name {{ font-size: 12px; font-weight: 700; color: #0F172A; }}
  .bchip-val {{ font-size: 13px; font-weight: 800; margin-top: 2px; }}
  .bchip.buy .bchip-val {{ color: #DC2626; }}
  .bchip.sell .bchip-val {{ color: #16A34A; }}
  .bchip-sub {{ font-size: 11px; color: #64748B; margin-top: 1px; }}
  .summary {{
    background: #FFFFFF;
    border: 1px solid #E5EAF2;
    border-radius: 10px;
    padding: 11px 14px;
    font-size: 13.5px;
    color: #334155;
    line-height: 1.6;
  }}
  .footer {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 22px 16px;
    background: #FFFFFF;
    border-top: 1px solid #E5EAF2;
    flex-shrink: 0;
  }}
  .sticky-hint {{ font-size: 12px; color: #8A94A6; }}
  .btns {{ display: flex; gap: 10px; }}
  button {{
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 13.5px;
    font-weight: 700;
    cursor: pointer;
    font-family: inherit;
  }}
  .btn-open {{ background: #2563EB; color: #FFFFFF; }}
  .btn-open:hover {{ background: #1D4ED8; }}
  .btn-ok {{ background: #F1F5F9; color: #0F172A; border-color: #CBD5E1; }}
  .btn-ok:hover {{ background: #E2E8F0; }}
</style>
</head>
<body>
  <div class="top-stripe"></div>
  <div class="wrap">
    <div class="header-row">
      <span class="verdict">{v_label}</span>
      {conf_html}
      <span class="date-txt">{t_date}</span>
    </div>
    <h1>{title}</h1>
    {kpis_html}
    {blocks_html}
    <div class="summary">{body}</div>
  </div>
  <div class="footer">
    <span class="sticky-hint">此提醒不会自动关闭，需您手动确认</span>
    <div class="btns">
      <button class="btn-open" onclick="window.pywebview.api.open_dashboard()">打开完整看板</button>
      <button class="btn-ok" onclick="window.pywebview.api.dismiss()">我知道了</button>
    </div>
  </div>
</body>
</html>"""


def _fallback_win32_box(title: str, body: str) -> None:
    if sys.platform == "win32":
        # MB_OK (0) | MB_ICONINFORMATION (0x40) | MB_TOPMOST (0x40000) | MB_SETFOREGROUND (0x10000)
        ctypes.windll.user32.MessageBoxW(0, body, title, 0x00050040)


def _run_alert_main(payload_path: str) -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        fp = open(log_dir / "alert.log", "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = fp
        if sys.stderr is None:
            sys.stderr = fp

    p: dict[str, Any] = {}
    try:
        with open(payload_path, "r", encoding="utf-8") as f:
            p = json.load(f)
    finally:
        try:
            os.remove(payload_path)
        except Exception:
            pass

    try:
        import webview

        win_ref: dict[str, Any] = {"win": None}

        class AlertApi:
            def dismiss(self) -> None:
                if win_ref["win"]:
                    win_ref["win"].destroy()

            def open_dashboard(self) -> None:
                exe = Path(sys.executable)
                pyw = exe.with_name("pythonw.exe")
                runner = str(pyw if pyw.exists() else exe)
                creationflags = 0x08000000 if sys.platform == "win32" else 0
                subprocess.Popen(
                    [runner, str(ROOT_DIR / "app.py")],
                    cwd=str(ROOT_DIR),
                    creationflags=creationflags,
                )
                if win_ref["win"]:
                    win_ref["win"].destroy()

        is_health = p.get("verdict_code") == "HEALTH_ALERT"
        win_title = "数据源异常预警 · GJDJK" if is_health else "国家队动向异动提醒 · GJDJK"
        has_blocks = bool([b for b in (p.get("group_blocks") or []) if b.get("net_flow_yi") is not None and abs(b["net_flow_yi"]) >= 3.0])
        height = 490 if has_blocks else (400 if is_health else 360)

        win = webview.create_window(
            title=win_title,
            html=_render_alert_html(p),
            js_api=AlertApi(),
            width=600,
            height=height,
            resizable=False,
            on_top=True,
            background_color="#F6F8FB",
        )
        win_ref["win"] = win
        webview.start(gui="edgechromium", debug=False)
    except Exception as e:
        logging.basicConfig(filename=str(log_dir / "alert.log"), level=logging.INFO)
        logging.exception("Alert pywebview failed, falling back to MessageBoxW: %s", e)
        _fallback_win32_box(p.get("title", "国家队动向提醒"), p.get("body", ""))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _run_alert_main(sys.argv[1])
