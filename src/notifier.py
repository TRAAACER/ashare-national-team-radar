"""多通道通知推送引擎：Win11 大号置顶弹窗 + 飞书 + 企业微信 + 个人微信(Server酱/PushPlus HTTPS) + 自定义Webhook."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """校验 Webhook URL 协议（仅允许 http/https 且必须含有效主机名；允许局域网内网地址）."""
    u = (url or "").strip()
    if not u:
        return False, "未填写 Webhook 地址"
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "Webhook 地址必须以 http:// 或 https:// 开头且包含有效主机"
    return True, u


def _send_win_toast(
    title: str,
    body: str,
    launch_url: str = "",
    extra: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """弹出大号、置顶、必须手动关闭的 Win11 桌面提醒卡片."""
    try:
        from src.alert_dialog import show_large_alert_dialog

        ex = extra or {}
        return show_large_alert_dialog(
            title=title,
            body=body,
            verdict_code=str(ex.get("verdict_code", "STRONG_BUY")),
            verdict_label=str(ex.get("verdict_label") or ex.get("verdict") or "国家队动向提醒"),
            trade_date=str(ex.get("trade_date", "")),
            total_net_flow_yi=ex.get("total_net_flow_yi"),
            nt_est_low_yi=ex.get("nt_est_low_yi"),
            nt_est_high_yi=ex.get("nt_est_high_yi"),
            confidence=ex.get("confidence"),
            group_blocks=ex.get("group_blocks"),
        )
    except Exception as e:
        logger.warning("Win11 alert dialog failed: %s", e)
        return False, str(e)


def _send_feishu(webhook: str, secret: str, title: str, body: str) -> tuple[bool, str]:
    ok_u, u_or_err = validate_webhook_url(webhook)
    if not ok_u:
        return False, f"飞书 {u_or_err}"
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": body},
            ],
        },
    }
    if secret.strip():
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret.strip()}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        payload["timestamp"] = ts
        payload["sign"] = base64.b64encode(hmac_code).decode("utf-8")
    try:
        r = requests.post(u_or_err, json=payload, timeout=10)
        j = r.json()
        ok = r.status_code == 200 and (j.get("code") == 0 or j.get("StatusCode") == 0)
        return ok, "ok" if ok else str(j)
    except Exception as e:
        return False, str(e)


def _send_wecom(webhook: str, title: str, body: str) -> tuple[bool, str]:
    ok_u, u_or_err = validate_webhook_url(webhook)
    if not ok_u:
        return False, f"企业微信 {u_or_err}"
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"### {title}\n{body}",
        },
    }
    try:
        r = requests.post(u_or_err, json=payload, timeout=10)
        j = r.json()
        ok = r.status_code == 200 and j.get("errcode") == 0
        return ok, "ok" if ok else str(j)
    except Exception as e:
        return False, str(e)


def _send_personal_wechat(provider: str, key: str, title: str, body: str) -> tuple[bool, str]:
    key = key.strip()
    if not key:
        return False, "未填写个人微信推送 SendKey / Token"
    try:
        if provider == "pushplus":
            r = requests.post(
                "https://www.pushplus.plus/send",
                json={"token": key, "title": title, "content": body, "template": "markdown"},
                timeout=10,
            )
            j = r.json()
            ok = r.status_code == 200 and j.get("code") == 200
            return ok, "ok" if ok else str(j)
        else:
            # 默认 Server酱 Turbo
            r = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title, "desp": body},
                timeout=10,
            )
            j = r.json()
            ok = r.status_code == 200 and j.get("code") == 0
            return ok, "ok" if ok else str(j)
    except Exception as e:
        return False, str(e)


def _send_custom_webhook(url: str, title: str, body: str, extra: dict[str, Any] | None = None) -> tuple[bool, str]:
    ok_u, u_or_err = validate_webhook_url(url)
    if not ok_u:
        return False, f"自定义 {u_or_err}"
    payload = {
        "app": "GJDJK",
        "title": title,
        "body": body,
        "timestamp": int(time.time()),
        **(extra or {}),
    }
    try:
        r = requests.post(u_or_err, json=payload, timeout=10)
        ok = 200 <= r.status_code < 300
        return ok, "ok" if ok else f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)


def get_enabled_channels(settings: dict[str, Any]) -> list[str]:
    """返回当前配置中已启用且凭据完整的通道名称列表."""
    channels: list[str] = []
    if settings.get("notify_win_toast", True):
        channels.append("win_toast")
    if settings.get("feishu_enabled") and str(settings.get("feishu_webhook") or "").strip():
        channels.append("feishu")
    if settings.get("wecom_enabled") and str(settings.get("wecom_webhook") or "").strip():
        channels.append("wecom")
    if settings.get("personal_wechat_enabled") and str(settings.get("personal_wechat_key") or "").strip():
        channels.append("personal_wechat")
    if settings.get("custom_webhook_enabled") and str(settings.get("custom_webhook_url") or "").strip():
        channels.append("custom_webhook")
    return channels


def send_single_channel(
    channel: str,
    settings: dict[str, Any],
    title: str,
    body: str,
    *,
    launch_url: str = "",
    extra: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if channel == "win_toast":
        return _send_win_toast(title, body, launch_url=launch_url, extra=extra)
    if channel == "feishu":
        return _send_feishu(
            str(settings.get("feishu_webhook") or ""),
            str(settings.get("feishu_secret") or ""),
            title,
            body,
        )
    if channel == "wecom":
        return _send_wecom(str(settings.get("wecom_webhook") or ""), title, body)
    if channel == "personal_wechat":
        return _send_personal_wechat(
            str(settings.get("personal_wechat_provider") or "serverchan"),
            str(settings.get("personal_wechat_key") or ""),
            title,
            body,
        )
    if channel == "custom_webhook":
        return _send_custom_webhook(str(settings.get("custom_webhook_url") or ""), title, body, extra=extra)
    return False, f"未知通知通道: {channel}"


def dispatch_notification(
    settings: dict[str, Any],
    title: str,
    body: str,
    *,
    launch_url: str = "",
    extra: dict[str, Any] | None = None,
    channels: list[str] | None = None,
) -> dict[str, tuple[bool, str]]:
    """根据当前配置（或指定的待发通道子集）分发通知."""
    target_channels = channels if channels is not None else get_enabled_channels(settings)
    results: dict[str, tuple[bool, str]] = {}
    for ch in target_channels:
        results[ch] = send_single_channel(ch, settings, title, body, launch_url=launch_url, extra=extra)
    return results


def send_test_channel(channel: str, settings: dict[str, Any], port: int = 18688) -> tuple[bool, str]:
    """设置页一键测试指定通知通道."""
    title = "【预览效果】疑似大额护盘买入 · 测试提醒"
    body = "这是一条来自 GJDJK 的大号置顶提醒预览。正式运行时，仅在监测到国家队异动或新官宣公告时才会弹出，且不会自动消失，确保您一定能看到。"
    url = f"http://127.0.0.1:{port}"
    if channel == "win_toast":
        return _send_win_toast(
            title,
            body,
            launch_url=url,
            extra={
                "verdict_code": "STRONG_BUY",
                "verdict_label": "疑似大额护盘买入（预览）",
                "trade_date": "演示样本",
                "total_net_flow_yi": 186.5,
                "nt_est_low_yi": 105.0,
                "nt_est_high_yi": 152.0,
                "confidence": 84,
                "group_blocks": [
                    {"group_name": "沪深300", "net_flow_yi": 112.4, "nt_est_low_yi": 68.0, "nt_est_high_yi": 96.0},
                    {"group_name": "中证1000", "net_flow_yi": 46.2, "nt_est_low_yi": 26.0, "nt_est_high_yi": 38.0},
                    {"group_name": "上证50/180", "net_flow_yi": 27.9, "nt_est_low_yi": 15.0, "nt_est_high_yi": 22.0},
                ],
            },
        )
    if channel in ("feishu", "wecom", "personal_wechat", "custom_webhook"):
        return send_single_channel(channel, settings, title, body, launch_url=url)
    return False, f"未知通知通道: {channel}"
