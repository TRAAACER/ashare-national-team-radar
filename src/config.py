"""项目路径、监控标的池与默认配置加载."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "watchlist.yaml"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "gjdjk.db"
WEB_DIR = ROOT_DIR / "web"

DEFAULT_SETTINGS: dict[str, Any] = {
    # 通知开关
    "notify_win_toast": True,
    "notify_daily_summary": False,  # False=仅异动提醒（防打扰），True=每日核实后必发精简日报
    "notify_announcements": True,   # 新官宣增持公告立刻高优推送
    "notify_health_alerts": True,   # 连续缺数/同步报错等数据源故障时自动弹出醒目预警
    # 飞书机器人 Webhook
    "feishu_enabled": False,
    "feishu_webhook": "",
    "feishu_secret": "",
    # 企业微信机器人 Webhook
    "wecom_enabled": False,
    "wecom_webhook": "",
    # 个人微信（Server酱 Turbo / PushPlus）
    "personal_wechat_enabled": False,
    "personal_wechat_provider": "serverchan",  # serverchan | pushplus
    "personal_wechat_key": "",
    # 自定义通用 Webhook（POST JSON）
    "custom_webhook_enabled": False,
    "custom_webhook_url": "",
    # 告警阈值与自动校准开关
    "auto_calibrate_thresholds": True,  # True=按总规模与近120日分位自动动态校准，False=固定使用下方手动数值
    "alert_flow_yi": 150.0,
    "alert_score_min": 72,
    "alert_active_lamps_min": 3,
    # 自动同步与桌面行为（默认每日晚间 21:30 盘后份额公布后定时巡检，错过自动开机补跑）
    "auto_start_enabled": True,
    "auto_schedule_time": "21:30",
    "holdings_last_check_at": "",
    "backfill_days": 250,
    "server_port": 18688,
}


def load_watchlist() -> dict[str, Any]:
    """读取 config/watchlist.yaml."""
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_etf_map() -> dict[str, dict[str, Any]]:
    cfg = load_watchlist()
    return {item["code"]: item for item in cfg.get("etfs", [])}


def get_group_map() -> dict[str, dict[str, Any]]:
    cfg = load_watchlist()
    return {item["id"]: item for item in cfg.get("groups", [])}
