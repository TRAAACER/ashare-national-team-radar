"""Win11 零后台常驻定时巡检管理（原生任务计划程序：默认每晚 21:30 交易所份额公布后运行，错过关机则下次开机自动补跑）."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.config import ROOT_DIR

logger = logging.getLogger(__name__)

# 新的任务计划程序名称（免管理员权限，零常驻后台）
TASK_NAME = "GJDJK_DailyCheck"
# 历史遗留开机启动注册表键（切换至晚间定时任务时自动清理，避免白天重复无效运行）
REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_REG_NAME = "GJDJK_NationalTeamMonitor"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def normalize_schedule_time(raw: str | None, default: str = "21:30") -> str:
    s = (raw or "").strip()
    if _TIME_RE.match(s):
        return s
    return default


def _get_pythonw_path() -> Path:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return pyw if pyw.exists() else exe


def get_startup_command() -> str:
    pyw = _get_pythonw_path()
    entry = ROOT_DIR / "app.py"
    return f'"{pyw}" "{entry}" --scheduled-check'


def _run_ps(script: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creationflags,
    )


def _remove_legacy_registry_run() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, LEGACY_REG_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def get_autostart_info() -> dict[str, Any]:
    """查询 Win11 任务计划程序定时巡检状态."""
    if sys.platform != "win32":
        return {"enabled": False, "schedule_time": "21:30", "mode": "none", "next_run": None, "task_name": TASK_NAME}

    ps = f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($null -eq $t) {{
  @{{ enabled = $false; mode = 'none' }} | ConvertTo-Json -Compress
}} else {{
  $info = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
  $trigTime = ''
  if ($t.Triggers -and $t.Triggers.Count -gt 0 -and $t.Triggers[0].StartBoundary) {{
    try {{
      $dt = [DateTime]::Parse($t.Triggers[0].StartBoundary)
      $trigTime = $dt.ToString('HH:mm')
    }} catch {{}}
  }}
  $nextRun = $null
  if ($info -and $info.NextRunTime -and $info.NextRunTime.Year -gt 2000) {{
    $nextRun = $info.NextRunTime.ToString('yyyy-MM-dd HH:mm')
  }}
  @{{
    enabled = ($t.State -ne 'Disabled')
    mode = 'task_scheduler'
    schedule_time = $trigTime
    start_when_available = [bool]$t.Settings.StartWhenAvailable
    next_run = $nextRun
    state = [string]$t.State
  }} | ConvertTo-Json -Compress
}}
"""
    try:
        res = _run_ps(ps, timeout=10.0)
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout.strip().splitlines()[-1])
            return {
                "enabled": bool(data.get("enabled")),
                "schedule_time": normalize_schedule_time(data.get("schedule_time"), "21:30"),
                "mode": data.get("mode", "none"),
                "start_when_available": bool(data.get("start_when_available", True)),
                "next_run": data.get("next_run"),
                "task_name": TASK_NAME,
            }
    except Exception as e:
        logger.warning("Query scheduled task failed: %s", e)
    return {"enabled": False, "schedule_time": "21:30", "mode": "none", "next_run": None, "task_name": TASK_NAME}


def is_autostart_enabled() -> bool:
    return bool(get_autostart_info().get("enabled"))


def set_autostart(enabled: bool, schedule_time: str = "21:30") -> tuple[bool, str]:
    """
    启用/禁用 Win11 原生每日盘后定时巡检任务（默认 21:30，开启 StartWhenAvailable 错过自动补跑），
    并自动清理旧版开机启动注册表项，确保零后台常驻且只在出结论后运行。
    """
    if sys.platform != "win32":
        return False, "当前非 Windows 系统"

    clean_time = normalize_schedule_time(schedule_time, "21:30")
    _remove_legacy_registry_run()

    if not enabled:
        ps_del = f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($null -ne $t) {{
  Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false
}}
Write-Output 'OK'
"""
        try:
            res = _run_ps(ps_del)
            if res.returncode == 0:
                return True, "已关闭每日自动定时巡检"
            return False, (res.stderr or res.stdout or "关闭计划任务失败").strip()
        except Exception as e:
            logger.warning("Disable scheduled task failed: %s", e)
            return False, str(e)

    pyw = str(_get_pythonw_path()).replace("'", "''")
    entry = str(ROOT_DIR / "app.py").replace("'", "''")
    workdir = str(ROOT_DIR).replace("'", "''")
    ps_reg = f"""
$ErrorActionPreference = 'Stop'
$act = New-ScheduledTaskAction -Execute '{pyw}' -Argument '"{entry}" --scheduled-check' -WorkingDirectory '{workdir}'
$trg = New-ScheduledTaskTrigger -Daily -At '{clean_time}'
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $act -Trigger $trg -Settings $set -Description 'A股国家队动向监控(GJDJK)：每日盘后官方份额公布后自动静默巡检，错过关机下次开机自动补跑，零后台常驻' -Force | Out-Null
Write-Output 'OK'
"""
    try:
        res = _run_ps(ps_reg)
        if res.returncode == 0 and "OK" in res.stdout:
            return True, f"已设置每日 {clean_time} 自动盘后巡检（错过关机时下次开机自动补跑，零后台常驻）"
        err = (res.stderr or res.stdout or "注册计划任务失败").strip()
        logger.warning("Register scheduled task failed: %s", err)
        return False, err
    except Exception as e:
        logger.warning("Register scheduled task exception: %s", e)
        return False, str(e)
