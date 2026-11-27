"""本地 FastAPI 服务端：强类型参数校验、防 CSV 公式注入、安全作业锁与静态资源托管."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.autostart import get_autostart_info, is_autostart_enabled, normalize_schedule_time, set_autostart
from src.config import WEB_DIR, load_watchlist
from src.db import Database
from src.notifier import send_test_channel, validate_webhook_url
from src.sync_service import SyncService


def _csv_safe_text(val: Any) -> str:
    """对 CSV 文本列中和 Excel 公式注入起始符（不触碰真实数值列，避免破坏负数金额）."""
    if val is None:
        return ""
    s = str(val)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


class SyncRequest(BaseModel):
    force: bool = False


class NoteUpdateRequest(BaseModel):
    user_note: str = ""
    user_tags: str = ""


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    notify_win_toast: bool | None = None
    notify_daily_summary: bool | None = None
    notify_announcements: bool | None = None
    notify_health_alerts: bool | None = None
    feishu_enabled: bool | None = None
    feishu_webhook: str | None = None
    feishu_secret: str | None = None
    wecom_enabled: bool | None = None
    wecom_webhook: str | None = None
    personal_wechat_enabled: bool | None = None
    personal_wechat_provider: Literal["serverchan", "pushplus"] | None = None
    personal_wechat_key: str | None = None
    custom_webhook_enabled: bool | None = None
    custom_webhook_url: str | None = None
    auto_calibrate_thresholds: bool | None = None
    alert_flow_yi: float | None = Field(default=None, ge=10.0, le=2000.0)
    alert_score_min: float | None = Field(default=None, ge=0.0, le=100.0)
    alert_active_lamps_min: int | None = Field(default=None, ge=1, le=5)
    auto_start_enabled: bool | None = None
    auto_schedule_time: str | None = None
    backfill_days: int | None = Field(default=None, ge=30, le=360)
    server_port: int | None = Field(default=None, ge=1024, le=65535)

    @field_validator("auto_schedule_time")
    @classmethod
    def _check_sched_time(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", s):
            raise ValueError("定时巡检时间格式必须为 HH:MM（如 22:30）")
        return s

    @field_validator("feishu_webhook", "wecom_webhook", "custom_webhook_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return ""
        ok, err = validate_webhook_url(s)
        if not ok:
            raise ValueError(err)
        return s


class TestNotifyRequest(BaseModel):
    channel: Literal["win_toast", "feishu", "wecom", "personal_wechat", "custom_webhook"]
    settings_override: SettingsUpdateRequest | None = None


def create_app(db: Database, sync_service: SyncService) -> FastAPI:
    app = FastAPI(title="国家队动向监控 (GJDJK)", version="2.0.0")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        st = sync_service.get_status()
        ai = get_autostart_info()
        st["autostart_info"] = ai
        st["auto_start_registry"] = ai["enabled"]
        st["recent_runs"] = db.get_recent_sync_runs(limit=8)
        return st

    @app.post("/api/sync")
    def api_sync(req: SyncRequest) -> dict[str, Any]:
        started = sync_service.run_in_background(trigger="manual", force=req.force)
        return {
            "started": started,
            "message": "已开始后台同步更新" if started else "当前已有同步任务正在运行",
        }

    @app.get("/api/dashboard")
    def api_dashboard(
        days: int = Query(default=60, ge=5, le=300),
        trade_date: str | None = Query(default=None),
        benchmark: str = Query(default="000300"),
        flow_scope: str = Query(default="ALL"),
    ) -> dict[str, Any]:
        cfg = load_watchlist()
        benchmarks_meta = cfg.get("benchmarks", [])
        bm_meta_map = {b["code"]: b for b in benchmarks_meta}
        if benchmark not in bm_meta_map:
            benchmark = "000300"
        bm_info = bm_meta_map[benchmark]
        etfs_cfg = cfg.get("etfs", [])
        core_pool_count = sum(1 for e in etfs_cfg if e.get("huijin_core"))

        sel_group_id = bm_info.get("group")
        effective_scope = "ALL" if (flow_scope != "GROUP" or not sel_group_id or sel_group_id == "SOE") else "GROUP"

        latest = db.get_daily_log(trade_date) if trade_date else db.get_latest_daily_log()
        all_logs = db.get_daily_logs(limit=300)
        chronological = list(reversed(all_logs))
        trend_slice = chronological[-days:] if len(chronological) > days else chronological

        sel_bm_rows = {r["trade_date"]: r for r in db.get_all_benchmarks(benchmark, limit=None)}

        cum_flow = 0.0
        cum_nt_mid = 0.0
        missing_flow_days = 0
        trend: list[dict[str, Any]] = []
        for lg in trend_slice:
            d = lg["trade_date"]
            if effective_scope == "GROUP":
                g_match = next((g for g in (lg.get("group_flows") or []) if g.get("group_id") == sel_group_id), None)
                bar_flow = g_match.get("net_flow_yi") if g_match else None
                bar_nt_low = g_match.get("nt_est_low_yi") if g_match else None
                bar_nt_mid = g_match.get("nt_est_mid_yi") if g_match else None
                bar_nt_high = g_match.get("nt_est_high_yi") if g_match else None
            else:
                bar_flow = lg.get("total_net_flow_yi")
                bar_nt_low = lg.get("nt_est_low_yi")
                bar_nt_mid = lg.get("nt_est_mid_yi")
                bar_nt_high = lg.get("nt_est_high_yi")

            if bar_flow is None:
                missing_flow_days += 1
            else:
                cum_flow = round(cum_flow + bar_flow, 2)
            if bar_nt_mid is not None:
                cum_nt_mid = round(cum_nt_mid + bar_nt_mid, 2)

            bm_r = sel_bm_rows.get(d) or {}
            trend.append(
                {
                    "trade_date": d,
                    "data_stage": lg["data_stage"],
                    "total_net_flow_yi": bar_flow,
                    "nt_est_low_yi": bar_nt_low,
                    "nt_est_mid_yi": bar_nt_mid,
                    "nt_est_high_yi": bar_nt_high,
                    "cum_flow_yi": cum_flow,
                    "cum_nt_mid_yi": cum_nt_mid,
                    "benchmark_close": bm_r.get("close"),
                    "benchmark_pct": bm_r.get("pct_chg"),
                    "hs300_close": lg.get("hs300_close"),
                    "hs300_pct": lg.get("hs300_pct"),
                    "sh_index_close": lg.get("sh_index_close"),
                    "sh_index_pct": lg.get("sh_index_pct"),
                    "verdict_code": lg.get("verdict_code"),
                    "verdict_label": lg.get("verdict_label"),
                    "nt_confidence": lg.get("nt_confidence"),
                }
            )

        def _window_sum(n: int) -> dict[str, Any]:
            sub = [x for x in chronological[-n:] if x.get("total_net_flow_yi") is not None]
            return {
                "days": len(sub),
                "net_flow_yi": round(sum(x["total_net_flow_yi"] for x in sub), 1),
                "nt_mid_yi": round(sum((x.get("nt_est_mid_yi") or 0.0) for x in sub), 1),
                "buy_days": sum(1 for x in sub if x.get("verdict_code") in ("STRONG_BUY", "MODERATE_BUY")),
                "sell_days": sum(1 for x in sub if x.get("verdict_code") in ("STRONG_SELL", "MODERATE_SELL")),
                "rotation_days": sum(
                    1 for x in sub if x.get("verdict_code") == "ROTATION" or bool((x.get("rotation_desc") or "").strip())
                ),
            }

        etf_details = db.get_etf_daily_by_date(latest["trade_date"]) if latest else []
        available_dates = [x["trade_date"] for x in all_logs]
        latest_benchmarks = db.get_benchmarks_by_date(latest["trade_date"]) if latest else {}

        return {
            "latest": latest,
            "latest_benchmarks": latest_benchmarks,
            "etf_details": etf_details,
            "trend": trend,
            "missing_flow_days": missing_flow_days,
            "benchmark_selected": bm_info,
            "benchmarks_meta": benchmarks_meta,
            "flow_scope": effective_scope,
            "available_dates": available_dates[:260],
            "window_stats": {
                "d5": _window_sum(5),
                "d20": _window_sum(20),
                "d60": _window_sum(60),
                "d250": _window_sum(250),
            },
            "groups_meta": cfg.get("groups", []),
            "etf_total_count": len(etfs_cfg),
            "core_pool_count": core_pool_count,
            "recent_announcements": db.get_announcements(limit=5),
            "sync_status": sync_service.get_status(),
        }

    @app.get("/api/logs")
    def api_logs(
        verdict: str = Query(default="ALL"),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        keyword: str | None = Query(default=None),
        only_with_note: bool = Query(default=False),
        limit: int = Query(default=300, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        items = db.get_daily_logs(
            limit=limit,
            offset=offset,
            verdict_filter=verdict,
            start_date=start_date or None,
            end_date=end_date or None,
            keyword=keyword or None,
            only_with_note=only_with_note,
        )
        return {"total": len(items), "items": items}

    @app.get("/api/logs/export")
    def api_export_logs(
        fmt: str = Query(default="markdown"),
        verdict: str = Query(default="ALL"),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        keyword: str | None = Query(default=None),
        only_with_note: bool = Query(default=False),
    ) -> Response:
        items = db.get_daily_logs(
            limit=500,
            verdict_filter=verdict,
            start_date=start_date or None,
            end_date=end_date or None,
            keyword=keyword or None,
            only_with_note=only_with_note,
        )
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "交易日期",
                    "数据核实状态",
                    "上证涨跌(%)",
                    "沪深300涨跌(%)",
                    "定性结论",
                    "异常信号强度",
                    "全池净申赎(亿元)",
                    "疑似国家队估算下限(亿)",
                    "疑似国家队估算上限(亿)",
                    "跨宽基调仓路径",
                    "自动复盘摘要",
                    "个人复盘标签",
                    "个人复盘备注",
                ]
            )
            for it in items:
                writer.writerow(
                    [
                        it["trade_date"],
                        it["data_stage"],
                        it.get("sh_index_pct", ""),
                        it.get("hs300_pct", ""),
                        _csv_safe_text(it.get("verdict_label", "")),
                        it.get("nt_confidence", ""),
                        it.get("total_net_flow_yi", ""),
                        it.get("nt_est_low_yi", ""),
                        it.get("nt_est_high_yi", ""),
                        _csv_safe_text(it.get("rotation_desc", "")),
                        _csv_safe_text(it.get("auto_summary", "")),
                        _csv_safe_text(it.get("user_tags", "")),
                        _csv_safe_text(it.get("user_note", "")),
                    ]
                )
            content = ("\ufeff" + buf.getvalue()).encode("utf-8")
            return Response(
                content=content,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="gjdjk_daily_logs.csv"'},
            )

        # Markdown 复盘档案导出
        lines = [
            "# A股国家队动向每日监控日志",
            "",
            f"> 导出条数：{len(items)} 条",
            "",
        ]
        for it in items:
            nf = f"{it['total_net_flow_yi']:+.1f} 亿" if it.get("total_net_flow_yi") is not None else "待披露"
            nt_rng = (
                f"{it['nt_est_low_yi']:+.1f} ~ {it['nt_est_high_yi']:+.1f} 亿"
                if it.get("nt_est_low_yi") is not None
                else "待披露"
            )
            hs_pct = f"{it['hs300_pct']:+.2f}%" if it.get("hs300_pct") is not None else "-"
            lines.append(f"## {it['trade_date']} · {it.get('verdict_label', '')}（信号强度 {it.get('nt_confidence', 0):.0f}）")
            lines.append(f"- **状态**：`{it['data_stage']}` ｜ **沪深300**：{hs_pct} ｜ **全池净申赎**：{nf} ｜ **疑似国家队**：{nt_rng}")
            if it.get("rotation_desc"):
                lines.append(f"- **调仓路径**：{it['rotation_desc']}")
            lines.append(f"- **自动摘要**：{it.get('auto_summary', '')}")
            if it.get("user_tags") or it.get("user_note"):
                lines.append(f"- **个人批注**：{('[' + it['user_tags'] + '] ') if it.get('user_tags') else ''}{it.get('user_note', '')}")
            lines.append("")
        md_bytes = "\n".join(lines).encode("utf-8")
        return Response(
            content=md_bytes,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="gjdjk_daily_logs.md"'},
        )

    @app.get("/api/logs/{trade_date}")
    def api_log_detail(trade_date: str) -> dict[str, Any]:
        log = db.get_daily_log(trade_date)
        if not log:
            raise HTTPException(status_code=404, detail=f"未找到 {trade_date} 的日志")
        etfs = db.get_etf_daily_by_date(trade_date)
        return {"log": log, "etf_details": etfs}

    @app.put("/api/logs/{trade_date}/note")
    def api_update_note(trade_date: str, req: NoteUpdateRequest) -> dict[str, Any]:
        updated = db.update_user_note(trade_date, req.user_note.strip(), req.user_tags.strip())
        if not updated:
            raise HTTPException(status_code=404, detail=f"未找到 {trade_date} 的日志")
        return {"status": "ok", "log": updated}

    @app.get("/api/holdings")
    def api_holdings() -> dict[str, Any]:
        rows = db.get_latest_holdings()
        all_rows = db.get_all_holdings()
        all_periods = sorted({r["report_date"] for r in all_rows}, reverse=True)
        nt_rows = [r for r in rows if r.get("entity_label")]
        by_entity: dict[str, dict[str, Any]] = {}
        by_etf: dict[str, dict[str, Any]] = {}
        total_est_yi = 0.0
        report_dates = sorted({r["report_date"] for r in rows}, reverse=True)
        fetched_dates = sorted({r.get("updated_at") or "" for r in rows if r.get("updated_at")}, reverse=True)

        latest_rd = report_dates[0] if report_dates else None
        anchor_age_days: int | None = None
        if latest_rd:
            try:
                anchor_age_days = max(0, (datetime.now() - datetime.strptime(latest_rd, "%Y-%m-%d")).days)
            except Exception:
                anchor_age_days = None

        for r in nt_rows:
            val = float(r.get("est_value_yi") or 0.0)
            total_est_yi += val
            el = r["entity_label"]
            ent = by_entity.setdefault(
                el,
                {
                    "entity_label": el,
                    "entity_category": r.get("entity_category", ""),
                    "etf_count": 0,
                    "est_value_yi": 0.0,
                },
            )
            ent["etf_count"] += 1
            ent["est_value_yi"] = round(ent["est_value_yi"] + val, 2)

            code = r["code"]
            ef = by_etf.setdefault(
                code,
                {
                    "code": code,
                    "etf_name": r.get("etf_name", code),
                    "report_date": r["report_date"],
                    "nt_ratio_sum": 0.0,
                    "nt_value_yi": 0.0,
                    "holders": [],
                },
            )
            ef["nt_ratio_sum"] = round(ef["nt_ratio_sum"] + float(r.get("hold_ratio") or 0.0), 2)
            ef["nt_value_yi"] = round(ef["nt_value_yi"] + val, 2)
            ef["holders"].append(r)

        return {
            "latest_report_date": latest_rd,
            "last_fetched_at": fetched_dates[0] if fetched_dates else None,
            "anchor_age_days": anchor_age_days,
            "period_count": len(all_periods),
            "available_periods": all_periods,
            "total_nt_value_yi": round(total_est_yi, 2),
            "covered_etfs": len(by_etf),
            "by_entity": sorted(by_entity.values(), key=lambda x: x["est_value_yi"], reverse=True),
            "by_etf": sorted(by_etf.values(), key=lambda x: x["nt_value_yi"], reverse=True),
            "weight_overrides": db.get_nt_weight_overrides(),
        }

    @app.get("/api/announcements")
    def api_announcements(limit: int = Query(default=80, ge=1, le=200)) -> dict[str, Any]:
        items = db.get_announcements(limit=limit)
        return {"total": len(items), "items": items}

    @app.get("/api/settings")
    def api_get_settings() -> dict[str, Any]:
        s = db.get_settings()
        ai = get_autostart_info()
        s["auto_start_enabled"] = ai["enabled"]
        s["auto_schedule_time"] = ai["schedule_time"] if ai["enabled"] else normalize_schedule_time(s.get("auto_schedule_time"), "22:30")
        s["autostart_info"] = ai
        s["current_scale"] = sync_service.analyzer.get_current_scale()
        return s

    @app.put("/api/settings")
    def api_put_settings(req: SettingsUpdateRequest) -> dict[str, Any]:
        payload = req.model_dump(exclude_none=True)
        autostart_msg = ""
        cur_settings = db.get_settings()
        old_auto = bool(cur_settings.get("auto_calibrate_thresholds", True))
        if "auto_start_enabled" in payload or "auto_schedule_time" in payload:
            target_enabled = bool(
                payload["auto_start_enabled"] if "auto_start_enabled" in payload else is_autostart_enabled()
            )
            target_time = normalize_schedule_time(
                payload.get("auto_schedule_time") or cur_settings.get("auto_schedule_time"),
                "22:30",
            )
            ok, msg = set_autostart(target_enabled, schedule_time=target_time)
            autostart_msg = msg
            if not ok:
                payload["auto_start_enabled"] = is_autostart_enabled()
        saved = db.update_settings(payload)
        ai = get_autostart_info()
        saved["auto_start_enabled"] = ai["enabled"]
        saved["auto_schedule_time"] = ai["schedule_time"] if ai["enabled"] else normalize_schedule_time(saved.get("auto_schedule_time"), "22:30")
        saved["autostart_info"] = ai
        recompute_msg = ""
        if "auto_calibrate_thresholds" in payload and bool(payload["auto_calibrate_thresholds"]) != old_auto:
            dates = sorted(db.get_log_stages().keys())
            if dates:
                rec_res = sync_service.recompute_with_lock(min_log_date=dates[0])
                recompute_msg = rec_res.get("message", "")
        saved["current_scale"] = sync_service.analyzer.get_current_scale()
        return {
            "status": "ok",
            "settings": saved,
            "autostart_message": autostart_msg,
            "recompute_message": recompute_msg,
        }

    @app.post("/api/settings/test-notify")
    def api_test_notify(req: TestNotifyRequest) -> JSONResponse:
        settings = db.get_settings()
        if req.settings_override:
            settings.update(req.settings_override.model_dump(exclude_none=True))
        ok, msg = send_test_channel(req.channel, settings, port=int(settings.get("server_port", 18688)))
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)

    @app.get("/api/notifications")
    def api_notifications() -> dict[str, Any]:
        return {"items": db.get_recent_notifications(limit=30)}

    return app
