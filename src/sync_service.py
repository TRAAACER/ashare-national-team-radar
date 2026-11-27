"""核心同步调度器：首次1年历史回填 + 开机自检完整日历补漏 + 跨进程租约互斥 + 分通道 Outbox 重试."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from src.analyzer import NationalTeamAnalyzer
from src.config import load_watchlist
from src.db import Database
from src.fetchers.holdings_ann import fetch_all_etf_top_holders, fetch_cninfo_announcements
from src.fetchers.market import (
    fetch_all_etf_daily,
    fetch_all_tail_metrics,
    fetch_benchmark_daily,
)
from src.fetchers.shares import (
    fetch_sse_shares_for_dates,
    fetch_szse_latest_snapshot,
    fetch_szse_shares_range,
)
from src.notifier import dispatch_notification, get_enabled_channels

logger = logging.getLogger(__name__)


def expected_latest_report_date(now: datetime | None = None) -> str:
    """
    按中国公募基金法定披露截止日推算当前应已公开的最新定期报告期：
      - 4月1日 ~ 8月31日：上年年报（YYYY-12-31，法定截止 3月31日）
      - 9月1日 ~ 次年3月31日：当年/上年半年报（YYYY-06-30，法定截止 8月31日）
    """
    dt = now or datetime.now()
    if 4 <= dt.month <= 8:
        return f"{dt.year - 1}-12-31"
    if dt.month >= 9:
        return f"{dt.year}-06-30"
    return f"{dt.year - 1}-06-30"


class SyncService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.cfg = load_watchlist()
        self.analyzer = NationalTeamAnalyzer(db)
        self._lock = threading.Lock()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._stop_requested = threading.Event()
        self._owner_id = f"sync-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.state: dict[str, Any] = {
            "is_running": False,
            "trigger": "",
            "phase": "idle",
            "progress": 100,
            "message": "就绪",
            "last_run_at": None,
            "last_error": None,
        }

    def request_stop(self) -> None:
        self._stop_requested.set()

    def wait_idle(self, timeout: float = 3.0) -> bool:
        return self._idle_event.wait(timeout=timeout)

    def _set_progress(self, phase: str, progress: int, message: str) -> None:
        self.state["phase"] = phase
        self.state["progress"] = max(0, min(100, progress))
        self.state["message"] = message

    def get_status(self) -> dict[str, Any]:
        latest = self.db.get_latest_daily_log()
        stages = self.db.get_log_stages()
        verified_cnt = sum(1 for v in stages.values() if v == "VERIFIED")
        partial_cnt = sum(1 for v in stages.values() if v == "SHARES_PARTIAL")
        health = self.evaluate_system_health()
        return {
            **self.state,
            "total_logs": len(stages),
            "verified_logs": verified_cnt,
            "partial_logs": partial_cnt,
            "latest_trade_date": latest["trade_date"] if latest else None,
            "latest_stage": latest["data_stage"] if latest else None,
            "health": health,
        }

    def evaluate_system_health(self, now: datetime | None = None) -> dict[str, Any]:
        """
        自动健康巡检：主动监测连续同步报错、连续多日份额待补全/核心池缺数、以及定期报告滞后，
        让用户无需自己盯着顶部状态栏，有真故障直接显著弹窗预警。
        """
        dt = now or datetime.now()
        today_str = dt.strftime("%Y-%m-%d")
        issues: list[dict[str, Any]] = []

        # 1. 检查最近 sync_runs 是否连续报错（跳过窗口关闭产生的 interrupted）
        runs = [r for r in self.db.get_recent_sync_runs(limit=8) if r.get("status") in ("ok", "error")]
        consec_errors = 0
        latest_err_msg = ""
        for r in runs:
            if r["status"] == "error":
                if consec_errors == 0:
                    latest_err_msg = str(r.get("message") or "")
                consec_errors += 1
            else:
                break
        if consec_errors >= 2:
            issues.append(
                {
                    "code": "SYNC_CONSECUTIVE_ERROR",
                    "severity": "error",
                    "title": f"数据同步已连续失败 {consec_errors} 次",
                    "detail": f"最近一次报错：{latest_err_msg or '未知错误'}",
                    "action_hint": "请检查本机网络/代理，或升级 akshare 与排查行情/交易所抓取接口。",
                }
            )

        # 2. 检查最近已完成交易日是否连续 >=2 天未核实（SHARES_PARTIAL / MARKET_PRELIM）
        recent_logs = self.db.get_daily_logs(limit=10)
        completed_logs = [
            lg for lg in recent_logs
            if (lg["trade_date"] < today_str) or (lg["trade_date"] == today_str and dt.hour >= 21)
        ]
        incomplete_streak: list[dict[str, Any]] = []
        for lg in completed_logs[:5]:
            if lg["data_stage"] != "VERIFIED":
                incomplete_streak.append(lg)
            else:
                break

        if len(incomplete_streak) >= 2:
            bad_dates = [lg["trade_date"] for lg in reversed(incomplete_streak)]
            sh_miss = sum(1 for lg in incomplete_streak if not lg.get("sh_shares_ready"))
            sz_miss = sum(1 for lg in incomplete_streak if not lg.get("sz_shares_ready"))
            ex_parts = []
            if sh_miss:
                ex_parts.append("上交所")
            if sz_miss:
                ex_parts.append("深交所")
            ex_txt = "、".join(ex_parts) if ex_parts else "部分核心宽基"
            latest_meta = incomplete_streak[0].get("meta") or {}
            miss_core = latest_meta.get("missing_core_codes") or []
            core_txt = f"（核心缺数代码：{', '.join(miss_core[:5])}）" if miss_core else ""
            issues.append(
                {
                    "code": "CONSECUTIVE_INCOMPLETE_SHARES",
                    "severity": "error",
                    "title": f"连续 {len(incomplete_streak)} 个交易日 ETF 份额未核实",
                    "detail": f"日期：{', '.join(bad_dates)} 持续处于待补全/未核实状态，主要缺口在 {ex_txt}{core_txt}。",
                    "action_hint": "很可能是交易所份额接口改版或 akshare 版本过期，建议点“强制全量重刷”确认，若仍异常请升级 akshare 或检修抓取脚本。",
                }
            )

        # 3. 检查 4月初/9月初定期报告底牌是否滞后于法定披露期（仅作柔和 info 提示，不天天弹错误窗）
        exp_rd = expected_latest_report_date(dt)
        latest_h = self.db.get_latest_holdings()
        db_max_rd = max((h["report_date"] for h in latest_h), default="")
        if db_max_rd and db_max_rd < exp_rd:
            issues.append(
                {
                    "code": "HOLDINGS_SOURCE_LAG",
                    "severity": "info",
                    "title": f"东财底牌尚未同步 {exp_rd} 定期报告",
                    "detail": f"当前最新实名底牌为 {db_max_rd}（系统每 3 天会自动重试拉取 {exp_rd} 报告，无需手动操作）。",
                    "action_hint": "东财数据中心若已同步新报告，程序将在下次巡检自动更新并重算；此前算法已启用时效衰减确保安全。",
                }
            )

        if any(i["severity"] == "error" for i in issues):
            status = "error"
        elif any(i["severity"] == "warning" for i in issues):
            status = "warning"
        else:
            status = "ok"

        err_titles = [i["title"] for i in issues if i["severity"] in ("error", "warning")]
        summary = "；".join(err_titles) if err_titles else "数据源与份额核实健康"
        return {"status": status, "summary": summary, "issues": issues, "expected_report_date": exp_rd}

    def _sync_holdings_if_needed(
        self,
        *,
        now: datetime,
        latest_market_date: str,
        etfs: list[dict[str, Any]],
        entities_cfg: list[dict[str, Any]],
        settings: dict[str, Any],
        force: bool = False,
    ) -> tuple[bool, bool, str]:
        """
        全自动定期报告底牌更新器（彻底免除用户 4 月初/9 月初手动记着点强制更新）：
          - 首次无底牌或 force=True：立即抓取；
          - 当法定应已公开的新年报/半年报尚未入库（db_max_rd < expected_latest_report_date）：每 3 天自动重试一次；
          - 平时每 7 天例行检查一次；
          - 一旦发现更新的报告期（new_max_rd > old_max_rd），返回 new_period_landed=True 触发全量历史重算！
        返回 (did_fetch, new_period_landed, latest_rd)
        """
        existing_holdings = self.db.get_latest_holdings()
        old_max_rd = max((h["report_date"] for h in existing_holdings), default="")
        exp_rd = expected_latest_report_date(now)

        last_check_str = str(settings.get("holdings_last_check_at") or "").strip()
        if not last_check_str and existing_holdings:
            last_check_str = str(existing_holdings[0].get("updated_at") or "")
        days_since_check = 999
        if last_check_str:
            try:
                days_since_check = max(0, (now - datetime.strptime(last_check_str[:10], "%Y-%m-%d")).days)
            except Exception:
                days_since_check = 999

        need_new_report = bool(old_max_rd) and (old_max_rd < exp_rd)
        should_fetch = (
            force
            or not existing_holdings
            or (need_new_report and days_since_check >= 3)
            or (days_since_check >= 7)
        )
        if not should_fetch:
            return False, False, old_max_rd

        self._set_progress("holdings", 87, "正在自动检查宽基 ETF 最新定期报告（年报/半年报）实名底牌...")
        latest_etf_rows = self.db.get_etf_daily_by_date(latest_market_date)
        latest_closes = {r["code"]: r["close"] for r in latest_etf_rows if r.get("close")}
        holders = fetch_all_etf_top_holders(etfs, entities_cfg, latest_closes)
        if holders:
            self.db.upsert_holdings(holders)
        self.db.update_settings({"holdings_last_check_at": now.strftime("%Y-%m-%d %H:%M:%S")})
        new_max_rd = max((h["report_date"] for h in self.db.get_latest_holdings()), default=old_max_rd)
        new_period_landed = bool(new_max_rd and old_max_rd and new_max_rd > old_max_rd)
        return True, new_period_landed, new_max_rd

    def _dispatch_health_alert(self, settings: dict[str, Any], now: datetime) -> bool:
        """当自检发现连续同步报错或连续多日份额未核实时，通过 Outbox（每日最多 1 次）弹出醒目故障预警卡片."""
        if not settings.get("notify_health_alerts", True):
            return False
        health = self.evaluate_system_health(now=now)
        err_issues = [i for i in health["issues"] if i["severity"] == "error"]
        if not err_issues:
            return False

        today_str = now.strftime("%Y-%m-%d")
        dedup_key = f"health:{today_str}"
        port = int(settings.get("server_port", 18688))
        title = f"【数据源异常预警】{err_issues[0]['title']}"
        body_lines = []
        for idx, it in enumerate(err_issues, 1):
            body_lines.append(f"{idx}. {it['title']}：{it['detail']}")
            body_lines.append(f"   处理建议：{it['action_hint']}")
        body = "\n".join(body_lines)
        ok_ch = self._deliver_via_outbox(
            dedup_keys=[(dedup_key, today_str, "HEALTH_ALERT")],
            title=title,
            body=body,
            settings=settings,
            launch_url=f"http://127.0.0.1:{port}",
            extra={
                "verdict_code": "HEALTH_ALERT",
                "verdict_label": "数据源异常预警",
                "trade_date": today_str,
            },
        )
        return bool(ok_ch)

    def run_in_background(self, trigger: str = "manual", force: bool = False) -> bool:
        if self.state["is_running"] or self._stop_requested.is_set():
            return False
        t = threading.Thread(
            target=self.run_smart_sync,
            kwargs={"trigger": trigger, "force": force},
            daemon=True,
        )
        t.start()
        return True

    def recompute_with_lock(self, min_log_date: str | None = None) -> dict[str, Any]:
        """在统一作业互斥锁（进程内 + SQLite 跨进程租约）保护下执行原地历史重算."""
        if not self._lock.acquire(blocking=False):
            return {"status": "busy", "message": "已有同步或重算任务正在运行"}
        lease_ok = False
        try:
            lease_ok = self.db.acquire_sync_lease(self._owner_id, ttl_seconds=600)
            if not lease_ok:
                return {"status": "busy", "message": "另一个进程正在执行同步任务"}
            self._idle_event.clear()
            self.state["is_running"] = True
            self.state["trigger"] = "recompute"
            self._set_progress("analyze", 60, "正在按新标尺重算历史日志...")
            n = self.analyzer.recompute_all(min_log_date=min_log_date)
            msg = f"标尺重算完成：更新 {n} 个交易日"
            self._set_progress("done", 100, msg)
            self.state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return {"status": "ok", "recomputed_days": n, "message": msg}
        except Exception as e:
            logger.exception("Recompute failed: %s", e)
            self.state["last_error"] = str(e)
            self._set_progress("error", 100, f"重算失败：{e}")
            return {"status": "error", "message": str(e)}
        finally:
            self.state["is_running"] = False
            self._idle_event.set()
            if lease_ok:
                try:
                    self.db.release_sync_lease(self._owner_id)
                except Exception:
                    pass
            self._lock.release()

    def run_smart_sync(self, trigger: str = "boot", force: bool = False) -> dict[str, Any]:
        """
        核心智能同步状态机：
        - 拿锁后全部逻辑严格位于 try/finally 之内，任何异常都确保释放进程锁与跨进程租约；
        - 基准指数每次拉取完整目标窗口（8 条指数 <1 秒），彻底消除长期关机后的交易日历缺口；
        - ETF 行情回补跨度依据最早缺失交易日自动延伸，避免只拉 45 天漏掉中段行情；
        - 按通道粒度的 SQLite Outbox 认领与失败重试，杜绝失败永久静默或跨进程双发。
        """
        if not self._lock.acquire(blocking=False):
            return {"status": "busy", "message": "已有同步任务正在运行"}

        lease_ok = False
        run_id: int | None = None
        try:
            lease_ok = self.db.acquire_sync_lease(self._owner_id, ttl_seconds=900)
            if not lease_ok:
                return {"status": "busy", "message": "另一个进程正在执行同步任务"}

            self._idle_event.clear()
            self.state["is_running"] = True
            self.state["trigger"] = trigger
            self.state["last_error"] = None
            run_id = self.db.start_sync_run(trigger, f"trigger={trigger}, force={force}")

            settings = self.db.get_settings()
            raw_bf = settings.get("backfill_days", 250)
            backfill_days = max(30, min(360, int(raw_bf)))

            etfs = self.cfg.get("etfs", [])
            benchmarks = self.cfg.get("benchmarks", [])
            entities_cfg = self.cfg.get("national_team_entities", [])
            sh_codes = {e["code"] for e in etfs if e["exchange"] == "SH"}
            sz_codes = {e["code"] for e in etfs if e["exchange"] == "SZ"}

            existing_stages = self.db.get_log_stages()
            is_initial_backfill = len(existing_stages) < 20
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # Step 1: 始终拉取完整目标窗口的 8 大基准指数（成本极低，彻底消灭长期关机日历断层）
            full_window_days = max(backfill_days + 45, 300)
            self._set_progress("benchmarks", 5, "正在获取 8 大宽基基准指数与完整交易日历...")
            for bm in benchmarks:
                if self._stop_requested.is_set():
                    raise RuntimeError("用户关闭窗口，已中止同步")
                try:
                    bm_rows = fetch_benchmark_daily(bm["code"], bm["name"], bm["market"], days=full_window_days)
                    self.db.upsert_benchmark_rows(bm_rows)
                except Exception as e:
                    logger.warning("Benchmark %s failed: %s", bm["code"], e)

            hs300_dates = [r["trade_date"] for r in self.db.get_all_benchmarks("000300", limit=None)]
            if not hs300_dates:
                raise RuntimeError("未能获取沪深300基准交易日历，请检查网络连接")

            window_dates = hs300_dates[-backfill_days:] if len(hs300_dates) > backfill_days else hs300_dates
            latest_market_date = window_dates[-1]

            if latest_market_date == today_str and (now.hour < 15 or (now.hour == 15 and now.minute < 15)):
                completed_dates = window_dates[:-1]
            else:
                completed_dates = window_dates

            if not completed_dates:
                msg = "尚无已完成收盘的交易日"
                self._set_progress("done", 100, msg)
                self.db.finish_sync_run(run_id, "ok", msg)
                return {"status": "skipped", "message": msg, "updated_dates": []}

            check_dates = completed_dates[1:] if len(completed_dates) > 1 else completed_dates
            missing_or_incomplete = [
                d for d in check_dates if existing_stages.get(d) != "VERIFIED"
            ]

            if not force and not is_initial_backfill:
                past_incomplete = [d for d in missing_or_incomplete if d < today_str]
                today_stage = existing_stages.get(today_str)
                today_needed = False
                if today_str in completed_dates:
                    if now.hour >= 20:
                        today_needed = today_stage != "VERIFIED"
                    elif now.hour > 15 or (now.hour == 15 and now.minute >= 15):
                        today_needed = today_stage is None
                if not past_incomplete and not today_needed:
                    self._sync_announcements(settings, is_initial=False)
                    # 即使日线份额已是最新，也自动检查 4月初年报 / 9月初半年报 底牌是否该更新
                    did_h, new_rd_landed, new_rd = self._sync_holdings_if_needed(
                        now=now,
                        latest_market_date=latest_market_date,
                        etfs=etfs,
                        entities_cfg=entities_cfg,
                        settings=settings,
                        force=False,
                    )
                    if new_rd_landed:
                        existing_dates = sorted(existing_stages.keys())
                        self.analyzer.recompute_all(min_log_date=existing_dates[0] if existing_dates else None)
                    # 同时检查近 10 日是否有之前失败待重试的通知通道
                    recent_cutoff = (now - timedelta(days=10)).strftime("%Y-%m-%d")
                    retried = self._dispatch_daily_alerts(
                        [d for d in check_dates[-7:] if d >= recent_cutoff], settings
                    )
                    health_alerted = self._dispatch_health_alert(settings, now)
                    msg = f"数据已是最新（核实至 {latest_market_date}），跳过重复抓取" + (
                        f"（自动更新至 {new_rd} 底牌并重算）" if new_rd_landed else ("（已例行检查底牌）" if did_h else "")
                    ) + (
                        f"（重试补发 {len(retried)} 日提醒）" if retried else ""
                    )
                    self._set_progress("done", 100, msg)
                    self.state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.db.finish_sync_run(run_id, "ok", msg)
                    return {
                        "status": "skipped",
                        "message": msg,
                        "updated_dates": [],
                        "notified_days": retried,
                        "health_alerted": health_alerted,
                    }

            # Step 2: 依据最早缺失交易日自动确定 ETF 日线回补跨度（彻底修复长期离线漏抓中段行情）
            if is_initial_backfill or force:
                market_days = full_window_days
            elif missing_or_incomplete:
                earliest_miss = missing_or_incomplete[0]
                try:
                    idx_first = hs300_dates.index(earliest_miss)
                    span_needed = len(hs300_dates) - idx_first + 35
                except ValueError:
                    span_needed = full_window_days
                market_days = min(full_window_days, max(45, span_needed))
            else:
                market_days = 45

            self._set_progress("market", 12, f"正在拉取 {len(etfs)} 只宽基 ETF 近 {market_days} 日行情...")

            def _mk_prog(done: int, total: int, text: str) -> None:
                pct = 12 + int(done / max(1, total) * 26)
                self._set_progress("market", pct, text)

            etf_daily_rows = fetch_all_etf_daily(etfs, days=market_days, progress_cb=_mk_prog)
            self.db.upsert_etf_daily_rows(etf_daily_rows)

            if self._stop_requested.is_set():
                raise RuntimeError("用户关闭窗口，已中止同步")

            # Step 3: 拉取近期 5 分钟 K 线（仅接受完整全天，过滤半日与截断首日）
            self._set_progress("tail", 40, "正在计算近期完整交易日尾盘 30 分钟脉冲特征...")

            def _tail_prog(done: int, total: int, text: str) -> None:
                pct = 40 + int(done / max(1, total) * 12)
                self._set_progress("tail", pct, text)

            tail_rows = fetch_all_tail_metrics(etfs, progress_cb=_tail_prog)
            self.db.upsert_etf_daily_rows(tail_rows)

            # Step 4: 精准补齐上交所 + 深交所官方日频份额
            if is_initial_backfill or force:
                share_dates = list(completed_dates)
            else:
                idx_map = {d: i for i, d in enumerate(completed_dates)}
                need_set = set(missing_or_incomplete)
                for d in list(need_set):
                    idx = idx_map.get(d)
                    if idx and idx > 0:
                        need_set.add(completed_dates[idx - 1])
                share_dates = sorted(need_set)

            if share_dates:
                self._set_progress("sse_shares", 54, f"正在抓取上交所 {len(share_dates)} 个交易日 ETF 份额...")

                def _sse_prog(done: int, total: int, text: str) -> None:
                    pct = 54 + int(done / max(1, total) * 22)
                    self._set_progress("sse_shares", pct, text)

                sse_rows = fetch_sse_shares_for_dates(share_dates, sh_codes, progress_cb=_sse_prog)
                self.db.upsert_etf_daily_rows(sse_rows)

                self._set_progress("szse_shares", 78, f"正在抓取深交所 {share_dates[0]} ~ {share_dates[-1]} ETF 份额...")

                def _szse_prog(done: int, total: int, text: str) -> None:
                    pct = 78 + int(done / max(1, total) * 8)
                    self._set_progress("szse_shares", pct, text)

                szse_rows = fetch_szse_shares_range(
                    share_dates[0], share_dates[-1], sz_codes, progress_cb=_szse_prog
                )
                self.db.upsert_etf_daily_rows(szse_rows)

                # 安全兜底：仅当深交所快照自带明确有效日期且与 latest_market_date 一致时才采纳
                szse_dates_got = {r["trade_date"] for r in szse_rows}
                if latest_market_date not in szse_dates_got and latest_market_date in share_dates and (
                    latest_market_date < today_str or now.hour >= 20
                ):
                    snap = fetch_szse_latest_snapshot(sz_codes, latest_market_date)
                    if snap:
                        self.db.upsert_etf_daily_rows(snap)

            # Step 5: 全自动多报告期定期报告实名底牌（首次、4月初/9月初新报告自动每3天重试、平时7天或强制刷新）
            self._sync_holdings_if_needed(
                now=now,
                latest_market_date=latest_market_date,
                etfs=etfs,
                entities_cfg=entities_cfg,
                settings=settings,
                force=force,
            )

            # Step 6: 同步巨潮国家队增持/权益公告（含事件分类）
            self._set_progress("announcements", 92, "正在检查中央汇金/国新/诚通最新公告...")
            new_anns = self._sync_announcements(settings, is_initial=is_initial_backfill)

            # Step 7: 原子发布派生资金流与每日复盘日志
            self._set_progress("analyze", 95, "正在执行分析引擎并原子发布每日日志...")
            min_log_date = completed_dates[1] if len(completed_dates) > 1 else completed_dates[0]
            recomputed = self.analyzer.recompute_all(min_log_date=min_log_date)

            # 统计真实核实 vs 待补全天数（绝不把未核实天数误报为“已核实”）
            post_stages = self.db.get_log_stages()
            win_set = set(check_dates)
            verified_count = sum(1 for d in win_set if post_stages.get(d) == "VERIFIED")
            partial_count = sum(1 for d in win_set if post_stages.get(d) == "SHARES_PARTIAL")
            prelim_count = sum(1 for d in win_set if post_stages.get(d) == "MARKET_PRELIM")

            # Step 8: 分通道 Outbox 去重提醒（仅对 VERIFIED 核实结论弹窗，首次全量初始化不刷屏）
            notified_days: list[str] = []
            health_alerted = False
            if not is_initial_backfill:
                recent_cutoff = (now - timedelta(days=10)).strftime("%Y-%m-%d")
                candidate_dates = sorted(
                    d for d in set(missing_or_incomplete + [latest_market_date]) if d >= recent_cutoff
                )
                notified_days = self._dispatch_daily_alerts(candidate_dates, settings)
                health_alerted = self._dispatch_health_alert(settings, now)

            msg = (
                f"同步完成：重算 {recomputed} 日，已核实 {verified_count} 日"
                + (f"，待补全 {partial_count} 日" if partial_count else "")
                + (f"，发现 {len(new_anns)} 条新公告" if new_anns else "")
                + (f"，送达 {len(notified_days)} 日异动提醒" if notified_days else "")
                + ("，触发数据源异常预警" if health_alerted else "")
            )
            self._set_progress("done", 100, msg)
            self.state["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.finish_sync_run(run_id, "ok", msg)
            return {
                "status": "ok",
                "message": msg,
                "recomputed_days": recomputed,
                "verified_count": verified_count,
                "partial_count": partial_count,
                "prelim_count": prelim_count,
                "new_announcements": len(new_anns),
                "notified_days": notified_days,
                "health_alerted": health_alerted,
            }
        except Exception as e:
            logger.exception("Sync failed: %s", e)
            self.state["last_error"] = str(e)
            self._set_progress("error", 100, f"同步失败：{e}")
            if run_id is not None:
                try:
                    self.db.finish_sync_run(run_id, "error", str(e))
                except Exception:
                    pass
            try:
                # 连续多次同步失败时自动触发数据源异常预警弹窗
                self._dispatch_health_alert(self.db.get_settings(), datetime.now())
            except Exception:
                pass
            return {"status": "error", "message": str(e)}
        finally:
            self.state["is_running"] = False
            self._idle_event.set()
            if lease_ok:
                try:
                    self.db.release_sync_lease(self._owner_id)
                except Exception:
                    pass
            self._lock.release()

    def _deliver_via_outbox(
        self,
        *,
        dedup_keys: list[tuple[str, str, str]],  # [(dedup_key, trade_date, stage), ...]
        title: str,
        body: str,
        settings: dict[str, Any],
        launch_url: str,
        extra: dict[str, Any] | None,
    ) -> list[str]:
        """
        基于 SQLite Outbox 的分通道原子认领、发送与状态落库：
        - 仅认领未送达（pending/failed）的通道，彻底消除并发双发；
        - 仅将成功送达的通道标记为 sent；失败通道保留 failed，下次巡检自动重试！
        返回本次至少成功送达的通道列表。
        """
        enabled = get_enabled_channels(settings)
        if not enabled or not dedup_keys:
            return []

        claimed_by_channel: dict[str, list[tuple[str, str, str]]] = {}
        for ch in enabled:
            claimed_keys: list[tuple[str, str, str]] = []
            for dk, td, stg in dedup_keys:
                if self.db.claim_notification_channel(dk, ch, td, stg, title, body):
                    claimed_keys.append((dk, td, stg))
            if claimed_keys:
                claimed_by_channel[ch] = claimed_keys

        if not claimed_by_channel:
            return []

        channels_to_send = list(claimed_by_channel.keys())
        res = dispatch_notification(
            settings,
            title,
            body,
            launch_url=launch_url,
            extra=extra,
            channels=channels_to_send,
        )

        ok_channels: list[str] = []
        for ch in channels_to_send:
            ok, msg = res.get(ch, (False, "未返回通道发送结果"))
            if ok:
                ok_channels.append(ch)
            for dk, td, stg in claimed_by_channel[ch]:
                self.db.complete_notification_channel(
                    dk, ch, ok, "" if ok else msg, trade_date=td, stage=stg, title=title, body=body
                )
        return ok_channels

    def _sync_announcements(self, settings: dict[str, Any], is_initial: bool) -> list[dict[str, Any]]:
        try:
            items = fetch_cninfo_announcements(lookback_days=730)
            newly_inserted = self.db.upsert_announcements(items)
            if newly_inserted:
                if is_initial:
                    self.db.mark_announcements_notified([a["ann_id"] for a in newly_inserted])
                elif settings.get("notify_announcements", True):
                    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
                    fresh = [a for a in newly_inserted if a["publish_date"] >= cutoff]
                    old_ids = [a["ann_id"] for a in newly_inserted if a["publish_date"] < cutoff]
                    if old_ids:
                        self.db.mark_announcements_notified(old_ids)
                    if fresh:
                        port = int(settings.get("server_port", 18688))
                        title = f"【官宣公告】监测到 {len(fresh)} 条国家队相关公告"
                        lines = [
                            f"- [{a['publish_date']}][{a.get('event_category', '主体动态')}] {a['matched_keyword']}：{a['title']}"
                            for a in fresh[:5]
                        ]
                        body = "\n".join(lines)
                        ok_ch = self._deliver_via_outbox(
                            dedup_keys=[(f"ann:{a['ann_id']}", a["publish_date"], "ANNOUNCEMENT") for a in fresh],
                            title=title,
                            body=body,
                            settings=settings,
                            launch_url=f"http://127.0.0.1:{port}",
                            extra={
                                "verdict_code": "ANNOUNCEMENT",
                                "verdict_label": "官方公告提醒",
                                "trade_date": fresh[0]["publish_date"],
                            },
                        )
                        if ok_ch:
                            self.db.mark_announcements_notified([a["ann_id"] for a in fresh])
            return newly_inserted
        except Exception as e:
            logger.warning("Announcement sync error: %s", e)
            return []

    def _dispatch_daily_alerts(self, candidate_dates: list[str], settings: dict[str, Any]) -> list[str]:
        """
        对新完成或升级为 VERIFIED 的近期交易日执行分通道 Outbox 提醒：
        - 历史日严格使用当日持久化 scale_json 的 alert_flow_yi，避免用今日标尺误判过去日期；
        - 仅在有通道实际送达成功时才算完成，失败通道下次自动重试。
        """
        enabled_channels = get_enabled_channels(settings)
        if not enabled_channels:
            return []

        auto_cal = bool(settings.get("auto_calibrate_thresholds", True))
        fallback_alert_yi = float(settings.get("alert_flow_yi", 150.0))
        alert_score_min = float(settings.get("alert_score_min", 72))
        alert_lamps_min = int(settings.get("alert_active_lamps_min", 3))
        daily_summary_on = bool(settings.get("notify_daily_summary", False))
        port = int(settings.get("server_port", 18688))

        alerts_to_send: list[tuple[str, dict[str, Any]]] = []
        for d in candidate_dates:
            log = self.db.get_daily_log(d)
            if not log:
                continue
            stage = log["data_stage"]
            # 只对交易所官方份额已核实（VERIFIED）的最终结论发弹窗，绝不用盘面未核实猜想打扰用户
            if stage != "VERIFIED":
                continue
            dedup_key = f"{d}:{stage}"
            # 只要任一已开启通道尚未成功送达，就保留在候选列表以支持失败重试
            if all(self.db.is_channel_sent(dedup_key, ch) for ch in enabled_channels):
                continue

            # 优先使用事件当日持久化标尺的 alert_flow_yi
            day_scale = log.get("scale") if isinstance(log.get("scale"), dict) else {}
            if auto_cal and day_scale.get("alert_flow_yi") is not None:
                alert_flow_yi = float(day_scale["alert_flow_yi"])
            elif auto_cal:
                alert_flow_yi = float(self.analyzer.get_current_scale().get("alert_flow_yi", fallback_alert_yi))
            else:
                alert_flow_yi = fallback_alert_yi

            lamps = log.get("lamps") or []
            active_lamps = sum(1 for l in lamps if l.get("state") in ("buy", "sell"))
            net_flow = log.get("total_net_flow_yi")
            conf = log.get("nt_confidence") or 0.0
            verdict = log.get("verdict_code") or "QUIET"

            is_anomaly = (
                verdict in ("STRONG_BUY", "STRONG_SELL", "ROTATION")
                or (net_flow is not None and abs(net_flow) >= alert_flow_yi)
                or (conf >= alert_score_min and active_lamps >= alert_lamps_min)
            )
            should_notify = is_anomaly or daily_summary_on
            if should_notify:
                alerts_to_send.append((dedup_key, log))

        if not alerts_to_send:
            return []

        if len(alerts_to_send) == 1:
            dedup_key, log = alerts_to_send[0]
            stage_cn = "份额已核实" if log["data_stage"] == "VERIFIED" else "收盘盘面初判"
            title = f"【{log['verdict_label']}】{log['trade_date']} ({stage_cn})"
            body = log.get("auto_summary") or ""
            ok_ch = self._deliver_via_outbox(
                dedup_keys=[(dedup_key, log["trade_date"], log["data_stage"])],
                title=title,
                body=body,
                settings=settings,
                launch_url=f"http://127.0.0.1:{port}",
                extra={
                    "trade_date": log["trade_date"],
                    "verdict_code": log["verdict_code"],
                    "verdict_label": log["verdict_label"],
                    "verdict": log["verdict_label"],
                    "total_net_flow_yi": log["total_net_flow_yi"],
                    "nt_est_low_yi": log["nt_est_low_yi"],
                    "nt_est_high_yi": log["nt_est_high_yi"],
                    "confidence": log["nt_confidence"],
                    "group_blocks": log.get("group_flows") or [],
                },
            )
            return [log["trade_date"]] if ok_ch else []

        # 多日补录命中多条异动 -> 合并成 1 条精炼摘要推送，不连环弹窗轰炸
        latest_log = alerts_to_send[-1][1]
        title = f"【开机补录异动汇总】近 {len(alerts_to_send)} 个交易日监测到国家队信号"
        lines = []
        for _, log in alerts_to_send:
            nf = f"{log['total_net_flow_yi']:+.1f}亿" if log.get("total_net_flow_yi") is not None else "盘面初判"
            lines.append(f"- {log['trade_date']} {log['verdict_label']}（全池 {nf}，信号强度 {log.get('nt_confidence', 0):.0f}）")
        body = "\n".join(lines)
        ok_ch = self._deliver_via_outbox(
            dedup_keys=[(dk, lg["trade_date"], lg["data_stage"]) for dk, lg in alerts_to_send],
            title=title,
            body=body,
            settings=settings,
            launch_url=f"http://127.0.0.1:{port}",
            extra={
                "trade_date": f"{alerts_to_send[0][1]['trade_date']} ~ {latest_log['trade_date']}",
                "verdict_code": latest_log["verdict_code"],
                "verdict_label": f"近 {len(alerts_to_send)} 日异动汇总",
                "total_net_flow_yi": latest_log.get("total_net_flow_yi"),
                "nt_est_low_yi": latest_log.get("nt_est_low_yi"),
                "nt_est_high_yi": latest_log.get("nt_est_high_yi"),
                "confidence": latest_log.get("nt_confidence"),
                "group_blocks": latest_log.get("group_flows") or [],
            },
        )
        return [log["trade_date"] for _, log in alerts_to_send] if ok_ch else []
