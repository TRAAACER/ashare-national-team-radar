"""核心分析引擎 v3：
1. 严格分层池：清晰区分 12 只汇金宽基核心池（huijin_core）、广义国家队高控盘池与 31 只全宽基池
2. 时点一致（Point-in-Time）底牌锚点 + 报告年龄与累计份额周转率衰减（弱证据允许下限归零）
3. 升级版双锚自适应标尺：基于「|净流|/AUM 比率」滚动 120 日分位 + 15~60 日平滑渐进权重（消除冷启动台阶跳变），每日标尺随日志持久化
4. 严格数据核实完备性：全核心池缺一不可 + 沪深 ≥90% 覆盖才升格 VERIFIED，区分拆合份与缺失
5. 同方向巨无霸共振（单巨头组不自共振）+ 金额加权尾盘脉冲 + 调仓标签与净买卖方向解耦并存
6. 全量派生结果与每日日志在单次短事务中原子发布
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any

from src.config import get_etf_map, get_group_map, load_watchlist
from src.db import Database


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _days_between(d1: str, d2: str) -> int:
    return abs((datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days)


def _signed_days(d_from: str, d_to: str) -> int:
    return (datetime.strptime(d_to, "%Y-%m-%d") - datetime.strptime(d_from, "%Y-%m-%d")).days


def _quantile(vals: list[float], q: float) -> float:
    """零依赖线性插值分位数（0<=q<=1）."""
    if not vals:
        return 0.0
    s = sorted(vals)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(len(s) - 1, lo + 1)
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _compute_adaptive_thresholds(
    *,
    auto_enabled: bool,
    static_cfg: dict[str, Any],
    pool_aum_yi: float,
    core_aum_yi: float,
    hist_pool_ratios: list[float],
    hist_core_ratios: list[float],
) -> dict[str, Any]:
    """
    升级版双锚自适应标尺（只使用过去历史，零未来函数）：
      - 锚一（规模锚）：按当日全池与 12 只汇金核心池总规模（AUM）固定比例伸缩；
      - 锚二（比率分位锚）：对近 120 交易日的「|净流| / 当日 AUM」比率求 55%/80% 分位，再映射回今日 AUM，
        彻底消除跨规模周期（2.3万亿→6千亿）下原始金额分位触顶饱和问题；
      - 平滑热身：历史样本数 15→60 日期间，分位锚权重由 0% 线性渐增至 50%，消除第 25 日台阶跳变。
    """
    if not auto_enabled or pool_aum_yi <= 500 or core_aum_yi <= 200:
        p_med = float(static_cfg.get("medium_flow_yi", 80.0))
        p_str = float(static_cfg.get("strong_flow_yi", 180.0))
        c_med = float(static_cfg.get("core_medium_yi", 50.0))
        c_str = float(static_cfg.get("core_strong_yi", 120.0))
        rot = float(static_cfg.get("rotation_side_yi", 45.0))
        return {
            "mode": "manual",
            "medium_flow_yi": round(p_med, 1),
            "strong_flow_yi": round(p_str, 1),
            "core_medium_yi": round(c_med, 1),
            "core_strong_yi": round(c_str, 1),
            "rotation_side_yi": round(rot, 1),
            "alert_flow_yi": round(p_str * 0.88, 1),
            "pool_aum_yi": round(pool_aum_yi, 1),
            "core_aum_yi": round(core_aum_yi, 1),
            "hist_weight": 0.0,
            "core_strong_aum_pct": round(c_str / core_aum_yi * 100.0, 2) if core_aum_yi > 0 else 0.0,
            "pool_strong_aum_pct": round(p_str / pool_aum_yi * 100.0, 2) if pool_aum_yi > 0 else 0.0,
        }

    # 锚一：总规模比例锚（12只严格汇金核心池 & 31只全宽基池）
    aum_c_med = core_aum_yi * 0.0038
    aum_c_str = core_aum_yi * 0.0095
    aum_p_med = pool_aum_yi * 0.0032
    aum_p_str = pool_aum_yi * 0.0080

    # 锚二：滚动 120 日「|净流|/AUM 比率」分位锚，权重随样本数 15..60 日平滑过渡至 0.50
    n_hist = min(len(hist_core_ratios), len(hist_pool_ratios))
    w_hist = _clamp((n_hist - 15) / 45.0, 0.0, 0.50)

    if w_hist > 0.0:
        w_cr = hist_core_ratios[-120:]
        w_pr = hist_pool_ratios[-120:]
        q_c_med = _clamp(_quantile(w_cr, 0.55) * core_aum_yi, aum_c_med * 0.55, aum_c_med * 1.75)
        q_c_str = _clamp(_quantile(w_cr, 0.80) * core_aum_yi, aum_c_str * 0.55, aum_c_str * 1.75)
        q_p_med = _clamp(_quantile(w_pr, 0.55) * pool_aum_yi, aum_p_med * 0.55, aum_p_med * 1.75)
        q_p_str = _clamp(_quantile(w_pr, 0.80) * pool_aum_yi, aum_p_str * 0.55, aum_p_str * 1.75)
        raw_c_med = (1.0 - w_hist) * aum_c_med + w_hist * q_c_med
        raw_c_str = (1.0 - w_hist) * aum_c_str + w_hist * q_c_str
        raw_p_med = (1.0 - w_hist) * aum_p_med + w_hist * q_p_med
        raw_p_str = (1.0 - w_hist) * aum_p_str + w_hist * q_p_str
    else:
        raw_c_med, raw_c_str = aum_c_med, aum_c_str
        raw_p_med, raw_p_str = aum_p_med, aum_p_str

    c_med = round(_clamp(raw_c_med, 25.0, 120.0), 1)
    c_str = round(_clamp(raw_c_str, max(65.0, c_med * 1.85), 240.0), 1)
    p_med = round(_clamp(raw_p_med, 40.0, 180.0), 1)
    p_str = round(_clamp(raw_p_str, max(100.0, p_med * 1.85), 360.0), 1)
    rot = round(_clamp(p_med * 0.55, 25.0, 90.0), 1)
    return {
        "mode": "auto",
        "medium_flow_yi": p_med,
        "strong_flow_yi": p_str,
        "core_medium_yi": c_med,
        "core_strong_yi": c_str,
        "rotation_side_yi": rot,
        "alert_flow_yi": round(p_str * 0.90, 1),
        "pool_aum_yi": round(pool_aum_yi, 1),
        "core_aum_yi": round(core_aum_yi, 1),
        "hist_weight": round(w_hist, 2),
        "core_strong_aum_pct": round(c_str / core_aum_yi * 100.0, 2) if core_aum_yi > 0 else 0.0,
        "pool_strong_aum_pct": round(p_str / pool_aum_yi * 100.0, 2) if pool_aum_yi > 0 else 0.0,
    }


def _describe_volume_ratio(ratio: float) -> str:
    """把量比翻译成直观大白话."""
    if ratio >= 2.2:
        return f"巨量暴增（常态{ratio:.1f}倍）"
    if ratio >= 1.55:
        return f"显著放量 +{round((ratio - 1) * 100)}%"
    if ratio >= 1.25:
        return f"温和放量 +{round((ratio - 1) * 100)}%"
    if ratio >= 0.80:
        return "量能常态"
    return f"缩量 -{round((1 - ratio) * 100)}%"


class NationalTeamAnalyzer:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.cfg = load_watchlist()
        self.etf_map = get_etf_map()
        self.group_map = get_group_map()
        self.thresholds = self.cfg.get("thresholds", {})
        # 严格 12 只汇金宽基核心池（不混入创业板 159915 或国新央企 560170）
        self.huijin_core_codes: set[str] = {
            c for c, m in self.etf_map.items() if bool(m.get("huijin_core"))
        }
        self.latest_scale: dict[str, Any] = {}

    def _is_huijin_core(self, code: str) -> bool:
        """严格按配置白名单识别中央汇金高控盘宽基核心池（12只）."""
        return code in self.huijin_core_codes

    @staticmethod
    def _resolve_point_in_time_anchor(
        code: str,
        trade_date: str,
        holdings_timeline: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """
        严格时点一致（Point-in-Time）底牌查询：
        仅返回 published_at（或 report_date）<= trade_date 的最新定期报告快照，绝不把未来报告倒灌到历史日期。
        """
        snaps = holdings_timeline.get(code)
        if not snaps:
            return None
        chosen: dict[str, Any] | None = None
        for snap in snaps:
            eff_date = snap.get("published_at") or snap["report_date"]
            if eff_date <= trade_date:
                chosen = snap
            else:
                break
        return chosen

    def recompute_all(
        self,
        target_dates: set[str] | None = None,
        min_log_date: str | None = None,
    ) -> int:
        """
        重算所有 ETF 的份额变动、净申赎、20日中位数成交额量比、DB 5日尾盘基线，
        并按原子短事务发布派生字段与每日复盘日志。
        """
        raw_rows = self.db.get_all_etf_daily()
        if not raw_rows:
            return 0

        by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in raw_rows:
            by_code[r["code"]].append(r)

        derived_updates: list[dict[str, Any]] = []
        enriched_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        earliest_code_date: dict[str, str] = {}
        # 记录每只 ETF 的份额序列用于计算锚点期以来的累计周转率
        shares_series_by_code: dict[str, list[tuple[str, float, float]]] = defaultdict(list)

        for code, series in by_code.items():
            series.sort(key=lambda x: x["trade_date"])
            amt_window: list[float] = []
            tail_amt_window: list[float] = []
            prev_share_row: dict[str, Any] | None = None
            prev_close_val: float | None = None

            for row in series:
                close = row.get("close") or 0.0
                if close > 0 and code not in earliest_code_date:
                    earliest_code_date[code] = row["trade_date"]

                # 若原始拉取因窗口首日缺前收导致 pct_chg 为 None，则按库内前一日收盘价精确补算
                pct_chg = row.get("pct_chg")
                if pct_chg is None and prev_close_val and prev_close_val > 0 and close > 0:
                    pct_chg = round((close - prev_close_val) / prev_close_val * 100.0, 3)
                    row["pct_chg"] = pct_chg
                if close > 0:
                    prev_close_val = float(close)

                amt = row.get("amount") or 0.0
                # 用前 20 日中位数作常态成交基线，避免单日 10 倍护盘暴量拉歪后面 20 天的量比
                if len(amt_window) >= 5:
                    med20 = float(median(amt_window[-20:]))
                    amt_ratio = (amt / med20) if med20 > 0 else 1.0
                else:
                    med20 = amt if amt > 0 else None
                    amt_ratio = 1.0 if amt > 0 else None
                if amt > 0:
                    amt_window.append(amt)

                # DB 持久化的近 5 个完整日尾盘基线（避免每次 6 日拉取重新冷启动）
                tail_amt = row.get("tail_amount")
                tail_share = row.get("tail_share")
                tail_ma5: float | None = None
                tail_ratio: float | None = row.get("tail_ratio")
                if tail_amt is not None and tail_share is not None and tail_amt >= 0:
                    if len(tail_amt_window) >= 2:
                        base_t = sum(tail_amt_window[-5:]) / len(tail_amt_window[-5:])
                        tail_ma5 = round(base_t, 2) if base_t > 0 else None
                        tail_ratio = round(tail_amt / base_t, 3) if base_t > 0 else None
                    elif tail_ratio and tail_ratio > 0 and tail_amt > 0:
                        # 兼容仅含单日尾盘样本的冷启动/单元测试场景
                        tail_ma5 = round(tail_amt / tail_ratio, 2)
                    else:
                        tail_ma5 = None
                        tail_ratio = None
                    if tail_amt > 0:
                        tail_amt_window.append(float(tail_amt))
                else:
                    tail_ma5 = None
                    tail_ratio = None

                total_shares = row.get("total_shares")
                nav = row.get("nav") or close
                share_delta: float | None = None
                net_flow_yi: float | None = None
                flow_status = "missing_shares"

                if total_shares and total_shares > 0:
                    if prev_share_row is None:
                        flow_status = "missing_prev"
                    else:
                        gap_days = _days_between(prev_share_row["trade_date"], row["trade_date"])
                        prev_shares = prev_share_row["total_shares"]
                        prev_close = prev_share_row.get("close") or close
                        if gap_days > 12:
                            flow_status = "gap_too_large"
                        elif prev_shares and prev_shares > 0:
                            ratio_chg = abs(total_shares / prev_shares - 1.0)
                            aum_now = total_shares * close if close > 0 else 0.0
                            aum_prev = prev_shares * prev_close if prev_close > 0 else 0.0
                            is_split = (
                                ratio_chg > 0.35
                                and aum_prev > 0
                                and abs(aum_now / aum_prev - 1.0) < 0.08
                            )
                            if is_split:
                                flow_status = "split"
                                share_delta = None
                                net_flow_yi = None
                            elif nav > 0:
                                share_delta = total_shares - prev_shares
                                net_flow_yi = round(share_delta * nav / 1e8, 3)
                                flow_status = "ok"
                            else:
                                flow_status = "missing_price"
                    shares_series_by_code[code].append(
                        (row["trade_date"], float(total_shares), abs(share_delta or 0.0))
                    )
                    prev_share_row = row

                row["amount_ma20"] = round(med20, 2) if med20 is not None else None
                row["amount_ratio"] = round(amt_ratio, 3) if amt_ratio is not None else None
                row["share_delta"] = round(share_delta, 2) if share_delta is not None else None
                row["net_flow_yi"] = net_flow_yi
                row["flow_status"] = flow_status
                row["tail_ma5"] = tail_ma5
                row["tail_ratio"] = tail_ratio

                derived_updates.append(
                    {
                        "trade_date": row["trade_date"],
                        "code": code,
                        "pct_chg": row.get("pct_chg"),
                        "amount_ma20": row["amount_ma20"],
                        "amount_ratio": row["amount_ratio"],
                        "share_delta": row["share_delta"],
                        "net_flow_yi": row["net_flow_yi"],
                        "flow_status": row["flow_status"],
                        "tail_ma5": row["tail_ma5"],
                        "tail_ratio": row["tail_ratio"],
                    }
                )
                enriched_by_date[row["trade_date"]].append(row)

        # 加载全部 8 条基准指数完整历史（不设 420 截断上限），计算 60 日价格区间位置 (0-100%)
        bm_by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        bm_pos_by_date: dict[str, dict[str, float]] = defaultdict(dict)
        for bm in self.cfg.get("benchmarks", []):
            series = self.db.get_all_benchmarks(bm["code"], limit=None)
            closes: list[float] = []
            for row in series:
                bm_by_date[row["trade_date"]][bm["code"]] = row
                c = row.get("close")
                if c and c > 0:
                    closes.append(float(c))
                    win = closes[-60:]
                    lo, hi = min(win), max(win)
                    pos = round((c - lo) / (hi - lo) * 100.0, 1) if hi > lo else 50.0
                    bm_pos_by_date[row["trade_date"]][bm["code"]] = pos

        holdings_timeline = self.db.get_holdings_timeline()
        settings = self.db.get_settings()
        auto_calibrate = bool(settings.get("auto_calibrate_thresholds", True))
        dates_sorted = sorted(enriched_by_date.keys())
        full_universe_mode = len(earliest_code_date) >= 15

        # 预构建每只 ETF 的锚点周转率辅助索引
        turnover_helper: dict[str, dict[str, Any]] = {}
        for code, pts in shares_series_by_code.items():
            # pts: sorted list of (trade_date, total_shares, abs_delta)
            cum_abs: list[float] = []
            running = 0.0
            d_list: list[str] = []
            s_list: list[float] = []
            for d_str, tot_s, abs_d in pts:
                running += abs_d
                d_list.append(d_str)
                s_list.append(tot_s)
                cum_abs.append(running)
            turnover_helper[code] = {"dates": d_list, "shares": s_list, "cum_abs": cum_abs}

        # 按时间正序遍历全序列：计算每日 AUM、比率分位双锚标尺、连买/连卖天数记忆
        core_flow_history: dict[str, float] = {}
        day_scales: dict[str, dict[str, Any]] = {}
        hist_pool_ratios: list[float] = []
        hist_core_ratios: list[float] = []
        for d in dates_sorted:
            day_rows = enriched_by_date[d]
            pool_aum = sum(
                (r["total_shares"] * r["close"] / 1e8)
                for r in day_rows
                if r.get("total_shares") and r.get("close")
            )
            core_aum = sum(
                (r["total_shares"] * r["close"] / 1e8)
                for r in day_rows
                if r.get("total_shares") and r.get("close") and self._is_huijin_core(r["code"])
            )
            scale = _compute_adaptive_thresholds(
                auto_enabled=auto_calibrate,
                static_cfg=self.thresholds,
                pool_aum_yi=pool_aum,
                core_aum_yi=core_aum,
                hist_pool_ratios=hist_pool_ratios,
                hist_core_ratios=hist_core_ratios,
            )
            day_scales[d] = scale

            pool_vals = [r["net_flow_yi"] for r in day_rows if r.get("net_flow_yi") is not None]
            core_vals = [
                r["net_flow_yi"]
                for r in day_rows
                if r.get("net_flow_yi") is not None and self._is_huijin_core(r["code"])
            ]
            if core_vals:
                c_sum = round(sum(core_vals), 2)
                core_flow_history[d] = c_sum
                if core_aum > 200:
                    hist_core_ratios.append(abs(c_sum) / core_aum)
            if pool_vals and pool_aum > 500:
                hist_pool_ratios.append(abs(sum(pool_vals)) / pool_aum)

        if dates_sorted and dates_sorted[-1] in day_scales:
            self.latest_scale = day_scales[dates_sorted[-1]]

        if target_dates:
            dates_to_process = [d for d in dates_sorted if d in target_dates]
        elif min_log_date:
            dates_to_process = [d for d in dates_sorted if d >= min_log_date]
        else:
            dates_to_process = dates_sorted

        # 维护连续核心净买/净卖天数与 5 日累计记忆
        core_dates = sorted(core_flow_history.keys())
        streak_map: dict[str, dict[str, Any]] = {}
        buy_streak = 0
        sell_streak = 0
        rolling5: list[float] = []
        for d in core_dates:
            val = core_flow_history[d]
            step_min = max(12.0, day_scales[d]["core_medium_yi"] * 0.45)
            if val >= step_min:
                buy_streak += 1
                sell_streak = 0
            elif val <= -step_min:
                sell_streak += 1
                buy_streak = 0
            else:
                buy_streak = 0
                sell_streak = 0
            rolling5.append(val)
            streak_map[d] = {
                "buy_streak": buy_streak,
                "sell_streak": sell_streak,
                "core_5d_yi": round(sum(rolling5[-5:]), 1),
            }

        daily_logs_to_write: list[dict[str, Any]] = []
        for d in dates_to_process:
            day_rows = enriched_by_date[d]
            if len(day_rows) < 5:
                continue
            log_obj = self._analyze_single_day(
                trade_date=d,
                rows=day_rows,
                benchmarks=bm_by_date.get(d, {}),
                bm_positions=bm_pos_by_date.get(d, {}),
                holdings_timeline=holdings_timeline,
                turnover_helper=turnover_helper,
                earliest_code_date=earliest_code_date,
                full_universe_mode=full_universe_mode,
                streak_info=streak_map.get(d, {"buy_streak": 0, "sell_streak": 0, "core_5d_yi": 0.0}),
                day_scale=day_scales[d],
            )
            daily_logs_to_write.append(log_obj)

        # 单次原子事务发布派生字段 + 每日日志 + 安全窗口裁剪
        return self.db.publish_analysis_results(
            derived_rows=derived_updates,
            daily_logs=daily_logs_to_write,
            min_log_date=min_log_date,
        )

    def get_current_scale(self) -> dict[str, Any]:
        """读取最新已持久化标尺（与数据库当前设置模式一致时直接返回，否则按库内序列重算）."""
        settings = self.db.get_settings()
        auto_calibrate = bool(settings.get("auto_calibrate_thresholds", True))
        expected_mode = "auto" if auto_calibrate else "manual"
        latest = self.db.get_latest_daily_log()
        if latest and isinstance(latest.get("scale"), dict) and latest["scale"].get("mode") == expected_mode:
            return latest["scale"]

        raw_rows = self.db.get_all_etf_daily()
        if not raw_rows:
            return _compute_adaptive_thresholds(
                auto_enabled=False,
                static_cfg=self.thresholds,
                pool_aum_yi=0.0,
                core_aum_yi=0.0,
                hist_pool_ratios=[],
                hist_core_ratios=[],
            )
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in raw_rows:
            by_date[r["trade_date"]].append(r)
        dates_sorted = sorted(by_date.keys())
        hist_pr: list[float] = []
        hist_cr: list[float] = []
        last_scale: dict[str, Any] = {}
        for d in dates_sorted:
            rows = by_date[d]
            p_aum = sum((r["total_shares"] * r["close"] / 1e8) for r in rows if r.get("total_shares") and r.get("close"))
            c_aum = sum(
                (r["total_shares"] * r["close"] / 1e8)
                for r in rows
                if r.get("total_shares") and r.get("close") and self._is_huijin_core(r["code"])
            )
            last_scale = _compute_adaptive_thresholds(
                auto_enabled=auto_calibrate,
                static_cfg=self.thresholds,
                pool_aum_yi=p_aum,
                core_aum_yi=c_aum,
                hist_pool_ratios=hist_pr,
                hist_core_ratios=hist_cr,
            )
            p_vals = [r["net_flow_yi"] for r in rows if r.get("net_flow_yi") is not None]
            c_vals = [
                r["net_flow_yi"]
                for r in rows
                if r.get("net_flow_yi") is not None and self._is_huijin_core(r["code"])
            ]
            if p_vals and p_aum > 500:
                hist_pr.append(abs(sum(p_vals)) / p_aum)
            if c_vals and c_aum > 200:
                hist_cr.append(abs(sum(c_vals)) / c_aum)
        return last_scale

    @staticmethod
    def _anchor_turnover_ratio(
        code: str,
        report_date: str,
        trade_date: str,
        turnover_helper: dict[str, dict[str, Any]],
    ) -> float:
        """计算自报告期 report_date 至 trade_date 之间累计绝对份额变动占报告期总份额的比率."""
        info = turnover_helper.get(code)
        if not info or not info["dates"]:
            return 0.0
        dates = info["dates"]
        shares = info["shares"]
        cum_abs = info["cum_abs"]

        # 找到 <= report_date 的最近索引（若早于本地历史起点，退化为最早日期）
        idx_anchor = 0
        for i, d_s in enumerate(dates):
            if d_s <= report_date:
                idx_anchor = i
            else:
                break
        # 找到 <= trade_date 的最近索引
        idx_now = -1
        for i, d_s in enumerate(dates):
            if d_s <= trade_date:
                idx_now = i
            else:
                break
        if idx_now <= idx_anchor or shares[idx_anchor] <= 0:
            return 0.0
        cum_delta = max(0.0, cum_abs[idx_now] - cum_abs[idx_anchor])
        # 同时考虑相对总份额净收缩/扩张幅度
        net_ratio = abs(shares[idx_now] - shares[idx_anchor]) / shares[idx_anchor]
        return max(cum_delta / shares[idx_anchor], net_ratio)

    @staticmethod
    def _pigeonhole_forced_sell_fraction(
        code: str,
        report_date: str,
        nt_shares_anchor: float,
        trade_date: str,
        curr_total_shares: float | None,
        share_delta: float | None,
        turnover_helper: dict[str, dict[str, Any]],
    ) -> float:
        """
        零未来函数的「鸽笼库存底线」校验（仅针对单边净赎回日）：
        若截至报告期 report_date 国家队实名持有 H_0 份，而当日净赎回（share_delta < 0）
        将基金总份额砸穿了 min(H_0, 此前区间最低总份额)，则击穿的新低缺口在数学上 100% 必然来自国家队减持
        （因为 H_t <= S_t 恒成立），绝不因高换手衰减而被过度打折。
        """
        if (
            not nt_shares_anchor
            or nt_shares_anchor <= 0
            or not curr_total_shares
            or curr_total_shares <= 0
            or share_delta is None
            or share_delta >= 0
        ):
            return 0.0
        info = turnover_helper.get(code)
        prev_min_s = float("inf")
        if info and info["dates"]:
            for d_s, tot_s in zip(info["dates"], info["shares"]):
                if d_s < report_date:
                    continue
                if d_s >= trade_date:
                    break
                if tot_s > 0 and tot_s < prev_min_s:
                    prev_min_s = tot_s
        eff_cap = min(nt_shares_anchor, prev_min_s)
        if curr_total_shares >= eff_cap:
            return 0.0
        forced_sh = min(abs(share_delta), eff_cap - curr_total_shares)
        return _clamp(forced_sh / abs(share_delta), 0.0, 1.0)

    def _analyze_single_day(
        self,
        trade_date: str,
        rows: list[dict[str, Any]],
        benchmarks: dict[str, dict[str, Any]],
        bm_positions: dict[str, float],
        holdings_timeline: dict[str, list[dict[str, Any]]],
        turnover_helper: dict[str, dict[str, Any]],
        earliest_code_date: dict[str, str],
        full_universe_mode: bool,
        streak_info: dict[str, Any],
        day_scale: dict[str, Any],
    ) -> dict[str, Any]:
        # 1. 严格逐代码完备性检查（全 12 只汇金核心池齐备 + 沪深均 >= 90% 覆盖才允许 VERIFIED）
        row_by_code = {r["code"]: r for r in rows}
        expected_codes: list[str] = []
        for code in self.etf_map:
            first_d = earliest_code_date.get(code)
            if first_d and first_d <= trade_date:
                expected_codes.append(code)
            elif full_universe_mode and code in self.huijin_core_codes:
                expected_codes.append(code)
        if not expected_codes:
            expected_codes = [r["code"] for r in rows]

        def _is_code_share_complete(c: str) -> bool:
            r = row_by_code.get(c)
            if r is None:
                return False
            if r.get("share_source") == "szse_snapshot":
                return False
            return r.get("flow_status") in ("ok", "split")

        exp_sh = [c for c in expected_codes if self.etf_map.get(c, {}).get("exchange") == "SH"]
        exp_sz = [c for c in expected_codes if self.etf_map.get(c, {}).get("exchange") == "SZ"]
        exp_core = [c for c in expected_codes if c in self.huijin_core_codes]

        sh_ok = [c for c in exp_sh if _is_code_share_complete(c)]
        sz_ok = [c for c in exp_sz if _is_code_share_complete(c)]
        missing_core_codes = sorted(c for c in exp_core if not _is_code_share_complete(c))
        missing_codes = sorted(c for c in expected_codes if not _is_code_share_complete(c))

        sh_ready = len(exp_sh) > 0 and (len(sh_ok) / len(exp_sh)) >= 0.90
        sz_ready = len(exp_sz) > 0 and (len(sz_ok) / len(exp_sz)) >= 0.90
        core_ready = len(exp_core) > 0 and len(missing_core_codes) == 0
        has_any_shares = (len(sh_ok) + len(sz_ok)) >= max(3, int(len(expected_codes) * 0.30))
        completeness_pct = round((len(sh_ok) + len(sz_ok)) / max(1, len(expected_codes)) * 100.0, 1)

        if sh_ready and sz_ready and core_ready:
            data_stage = "VERIFIED"
        elif sh_ready or sz_ready or has_any_shares:
            data_stage = "SHARES_PARTIAL"
        else:
            data_stage = "MARKET_PRELIM"

        # 2. 基准指数与 60 日价格区间位置
        sh_bm = benchmarks.get("000001") or {}
        hs300_bm = benchmarks.get("000300") or {}
        zz1000_bm = benchmarks.get("000852") or {}
        cyb_bm = benchmarks.get("399006") or {}
        hs300_pct = hs300_bm.get("pct_chg")
        sh_pct = sh_bm.get("pct_chg")
        market_pct = hs300_pct if hs300_pct is not None else (sh_pct if sh_pct is not None else 0.0)
        hs300_pos60 = bm_positions.get("000300", 50.0)

        pos_low = float(self.thresholds.get("price_pos_low", 40.0))
        pos_high = float(self.thresholds.get("price_pos_high", 75.0))
        pos_ext_high = float(self.thresholds.get("price_pos_extreme_high", 85.0))

        # 3. 成交额中位数倍率（核心巨无霸 + 12 只严格汇金核心池）
        all_giants = [r for r in rows if self.etf_map.get(r["code"], {}).get("core_giant")]
        core_pool_rows = [r for r in rows if self._is_huijin_core(r["code"])]
        vol_strong = float(self.thresholds.get("volume_strong_ratio", 1.80))
        vol_med = float(self.thresholds.get("volume_medium_ratio", 1.35))

        tot_amt_now = sum((r.get("amount") or 0.0) for r in all_giants)
        tot_amt_base = sum((r.get("amount_ma20") or 0.0) for r in all_giants if r.get("amount_ma20"))
        weighted_vol_ratio = (tot_amt_now / tot_amt_base) if tot_amt_base > 0 else 1.0

        core_amt_now = sum((r.get("amount") or 0.0) for r in core_pool_rows)
        core_amt_base = sum((r.get("amount_ma20") or 0.0) for r in core_pool_rows if r.get("amount_ma20"))
        core_vol_ratio = (core_amt_now / core_amt_base) if core_amt_base > 0 else weighted_vol_ratio
        effective_vol_ratio = max(weighted_vol_ratio, core_vol_ratio)
        vol_desc = _describe_volume_ratio(weighted_vol_ratio)

        # 4. 严格 12 只汇金核心池净申赎 & 头部同步共振
        core_flow_rows = [r for r in core_pool_rows if r.get("net_flow_yi") is not None]
        core_net_flow_yi = round(sum(r["net_flow_yi"] for r in core_flow_rows), 2) if core_flow_rows else None

        hs300_giants = [r for r in rows if r["code"] in {"510300", "510310", "510330", "159919"}]
        if core_net_flow_yi is not None:
            g300_buy = sum(1 for r in hs300_giants if (r.get("net_flow_yi") or 0.0) >= 1.5)
            g300_sell = sum(1 for r in hs300_giants if (r.get("net_flow_yi") or 0.0) <= -1.5)
            core_buy = sum(1 for r in core_flow_rows if r["net_flow_yi"] >= 1.5)
            core_sell = sum(1 for r in core_flow_rows if r["net_flow_yi"] <= -1.5)
            core_cnt = max(1, len(core_flow_rows))
            dominant_buy = core_buy >= core_sell
            dom_cnt = core_buy if dominant_buy else core_sell
            dom_ratio = dom_cnt / core_cnt
            g300_dom = g300_buy if dominant_buy else g300_sell
            g300_total = max(1, len(hs300_giants))
            sync_active = g300_dom >= 3 or dom_ratio >= 0.55
        else:
            # 无份额盘面初判方向：仅“低位/下跌放量”猜托盘、“高位上涨放量”猜派发；
            # 低位政策利好暴涨放量（如 60日低位暴涨 +4%）在份额出来前保持中性待确认，绝不盲猜派发！
            g300_buy = g300_sell = core_buy = core_sell = 0
            vol_spike_300 = sum(1 for r in hs300_giants if (r.get("amount_ratio") or 1.0) >= 1.45)
            vol_spike_core = sum(1 for r in core_pool_rows if (r.get("amount_ratio") or 1.0) >= 1.45)
            g300_total = max(1, len(hs300_giants))
            core_cnt = max(1, len(core_pool_rows))
            g300_dom = vol_spike_300
            dom_cnt = vol_spike_core
            dom_ratio = vol_spike_core / core_cnt
            if (hs300_pos60 <= pos_low or market_pct <= -0.50) and market_pct <= 0.50:
                prelim_dir = "buy"
            elif hs300_pos60 >= pos_high and market_pct >= 0.0:
                prelim_dir = "sell"
            else:
                prelim_dir = "neutral"
            dominant_buy = prelim_dir == "buy"
            sync_active = vol_spike_300 >= 3 or dom_ratio >= 0.45

        # 5. 按 8 大宽基方向归集 + 逐 ETF 时点一致锚点衰减与同方向共振估算
        group_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            meta = self.etf_map.get(r["code"], {})
            gid = r.get("group_id") or meta.get("group") or "OTHER"
            group_buckets[gid].append(r)

        anchor_dates_used: set[str] = set()
        anchor_ages_used: list[int] = []

        def _estimate_etf_nt_range(
            r: dict[str, Any],
            same_dir_sync: float,
            idx_pos60: float,
            idx_pct: float,
        ) -> tuple[float, float]:
            nf = r.get("net_flow_yi")
            if nf is None or abs(nf) < 0.05:
                return 0.0, 0.0
            code = r["code"]
            meta = self.etf_map.get(code, {})
            prior_w = float(meta.get("base_nt_weight", 0.25))

            snap = self._resolve_point_in_time_anchor(code, trade_date, holdings_timeline)
            if snap and snap.get("has_nt_match"):
                obs_w = float(snap["nt_ratio"])
                rd = snap["report_date"]
                anchor_dates_used.add(rd)
                age_days = max(0, _signed_days(rd, trade_date))
                anchor_ages_used.append(age_days)
                turnover_r = self._anchor_turnover_ratio(code, rd, trade_date, turnover_helper)
                freshness = _clamp(
                    math.exp(-age_days / 240.0) * math.exp(-0.45 * min(turnover_r, 4.0)),
                    0.25,
                    1.0,
                )
                # 老化/高周转时向保守结构先验收缩，且绝不把低占比 ETF（如 510100 5.83%）往上拉歪
                shrink_target = 0.62 if meta.get("huijin_core") else min(prior_w, max(obs_w, 0.20))
                eff_w = freshness * obs_w + (1.0 - freshness) * shrink_target
                uncertainty_extra = (1.0 - freshness) * 0.22
            else:
                eff_w = prior_w
                uncertainty_extra = 0.16

            amt_r = r.get("amount_ratio") or 1.0
            # 同方向共振 + 量能 + 60 日价格区间位置修正
            boost = 0.0
            if same_dir_sync >= 0.75:
                boost += 0.14
            elif same_dir_sync >= 0.50:
                boost += 0.06
            if amt_r >= vol_med:
                boost += 0.10
            elif amt_r < 0.85:
                boost -= 0.14

            if nf > 0:
                if idx_pos60 <= pos_low or idx_pct <= -0.5:
                    boost += 0.10
                elif idx_pos60 >= pos_ext_high:
                    boost -= 0.28
                elif idx_pos60 >= pos_high:
                    boost -= 0.15
            else:
                if idx_pos60 >= pos_high or idx_pct >= 0.6:
                    boost += 0.12
                elif idx_pos60 <= pos_low:
                    boost -= 0.15

            # 允许弱证据或高老化情景下限降至 0.0（不再硬性保底 8%）
            lo_mult = 0.52 + boost * 0.75 - uncertainty_extra
            hi_mult = 0.88 + boost * 0.75 + uncertainty_extra * 0.55
            lo_f = _clamp(eff_w * lo_mult, 0.0, 0.82)
            hi_f = _clamp(eff_w * hi_mult, max(lo_f + 0.08, min(0.08, eff_w)), 0.95)

            # 鸽笼库存底线校正：当单边大卖把 ETF 总份额砸穿实名持仓 H_0 新低时，
            # 击穿缺口数学上必然由国家队卖出，防止周转率衰减把下限折得过低
            if nf < 0 and snap and snap.get("has_nt_match") and (snap.get("nt_shares") or 0.0) > 0:
                forced_frac = self._pigeonhole_forced_sell_fraction(
                    code=code,
                    report_date=snap["report_date"],
                    nt_shares_anchor=float(snap["nt_shares"]),
                    trade_date=trade_date,
                    curr_total_shares=r.get("total_shares"),
                    share_delta=r.get("share_delta"),
                    turnover_helper=turnover_helper,
                )
                if forced_frac > 0.0:
                    lo_f = max(lo_f, _clamp(0.75 * forced_frac + 0.08, 0.0, 0.86))
                    hi_f = max(hi_f, _clamp(0.92 * forced_frac + 0.05, lo_f + 0.06, 0.96))

            v1, v2 = nf * lo_f, nf * hi_f
            return (min(v1, v2), max(v1, v2))

        group_flows: list[dict[str, Any]] = []
        total_nt_low = 0.0
        total_nt_high = 0.0
        any_nt_calc = False

        for g_cfg in self.cfg.get("groups", []):
            gid = g_cfg["id"]
            g_rows = group_buckets.get(gid, [])
            flow_rows = [r for r in g_rows if r.get("net_flow_yi") is not None]
            g_net = round(sum(r["net_flow_yi"] for r in flow_rows), 2) if flow_rows else None
            g_amt_yi = round(sum((r.get("amount") or 0.0) for r in g_rows) / 1e8, 2)
            g_med_base = sum((r.get("amount_ma20") or 0.0) for r in g_rows if r.get("amount_ma20"))
            g_vol_ratio = round((sum((r.get("amount") or 0.0) for r in g_rows) / g_med_base), 2) if g_med_base > 0 else 1.0

            # 组内同方向巨无霸共振（至少 2 只巨头参与才构成共振，消除单巨头组“自我共振”和反向沾光）
            giants = [
                r for r in g_rows
                if self.etf_map.get(r["code"], {}).get("core_giant") and r.get("net_flow_yi") is not None
            ]
            g_pos = sum(1 for r in giants if r["net_flow_yi"] >= 0.8)
            g_neg = sum(1 for r in giants if r["net_flow_yi"] <= -0.8)
            sync_pos = round(g_pos / len(giants), 2) if len(giants) >= 2 else 0.0
            sync_neg = round(g_neg / len(giants), 2) if len(giants) >= 2 else 0.0
            sync_ratio = max(sync_pos, sync_neg)

            bm_code = g_cfg.get("benchmark", "000300")
            bm_row = benchmarks.get(bm_code) or {}
            idx_pct = bm_row.get("pct_chg")
            idx_pos60 = bm_positions.get(bm_code, hs300_pos60)

            g_nt_low: float | None = None
            g_nt_high: float | None = None
            g_nt_mid: float | None = None
            if flow_rows:
                lo_s = 0.0
                hi_s = 0.0
                for r in flow_rows:
                    etf_sync = sync_pos if r["net_flow_yi"] > 0 else sync_neg
                    l_v, h_v = _estimate_etf_nt_range(r, etf_sync, idx_pos60, idx_pct or 0.0)
                    lo_s += l_v
                    hi_s += h_v
                g_nt_low = round(min(lo_s, hi_s), 1)
                g_nt_high = round(max(lo_s, hi_s), 1)
                g_nt_mid = round((g_nt_low + g_nt_high) / 2.0, 1)
                total_nt_low += g_nt_low
                total_nt_high += g_nt_high
                any_nt_calc = True

            group_flows.append(
                {
                    "group_id": gid,
                    "group_name": g_cfg["name"],
                    "benchmark_code": bm_code,
                    "purity": g_cfg.get("purity", ""),
                    "color": g_cfg.get("color", "#2563EB"),
                    "etf_count": len(g_rows),
                    "shares_ready": len(flow_rows),
                    "index_pct": idx_pct,
                    "index_pos60": idx_pos60,
                    "net_flow_yi": g_net,
                    "nt_est_low_yi": g_nt_low,
                    "nt_est_mid_yi": g_nt_mid,
                    "nt_est_high_yi": g_nt_high,
                    "amount_yi": g_amt_yi,
                    "avg_amount_ratio": g_vol_ratio,
                    "volume_desc": _describe_volume_ratio(g_vol_ratio),
                    "giant_sync_ratio": sync_ratio,
                }
            )

        # 6. 全池与核心池总计 + 跨宽基换仓（调仓标签与净买卖方向解耦）
        valid_group_flows = [g for g in group_flows if g["net_flow_yi"] is not None]
        if valid_group_flows:
            total_net_flow_yi = round(sum(g["net_flow_yi"] for g in valid_group_flows), 2)
            gross_inflow_yi = round(sum(max(0.0, g["net_flow_yi"]) for g in valid_group_flows), 2)
            gross_outflow_yi = round(sum(abs(min(0.0, g["net_flow_yi"])) for g in valid_group_flows), 2)
        else:
            total_net_flow_yi = None
            gross_inflow_yi = None
            gross_outflow_yi = None

        if any_nt_calc:
            nt_low = round(min(total_nt_low, total_nt_high), 1)
            nt_high = round(max(total_nt_low, total_nt_high), 1)
            nt_mid = round((nt_low + nt_high) / 2.0, 1)
        else:
            nt_low = nt_mid = nt_high = None

        strong_yi = float(day_scale["strong_flow_yi"])
        medium_yi = float(day_scale["medium_flow_yi"])
        core_strong_yi = float(day_scale["core_strong_yi"])
        core_medium_yi = float(day_scale["core_medium_yi"])
        rot_side = float(day_scale["rotation_side_yi"])
        rot_offset = float(self.thresholds.get("rotation_offset_ratio", 0.45))

        has_rotation = False
        is_pure_rotation = False
        rotation_matched_yi = 0.0
        rotation_desc = ""
        if (
            total_net_flow_yi is not None
            and core_net_flow_yi is not None
            and gross_inflow_yi is not None
            and gross_outflow_yi is not None
            and gross_inflow_yi >= rot_side
            and gross_outflow_yi >= rot_side
            and abs(total_net_flow_yi) <= max(rot_offset, 0.55) * (gross_inflow_yi + gross_outflow_yi)
        ):
            has_rotation = True
            rotation_matched_yi = round(min(gross_inflow_yi, gross_outflow_yi), 1)
            out_groups = sorted([g for g in valid_group_flows if g["net_flow_yi"] <= -8.0], key=lambda x: x["net_flow_yi"])[:2]
            in_groups = sorted([g for g in valid_group_flows if g["net_flow_yi"] >= 8.0], key=lambda x: x["net_flow_yi"], reverse=True)[:2]
            out_txt = "、".join(f"{g['group_name']}({g['net_flow_yi']:+.1f}亿)" for g in out_groups) or "大盘宽基"
            in_txt = "、".join(f"{g['group_name']}({g['net_flow_yi']:+.1f}亿)" for g in in_groups) or "中小/新宽基"
            rotation_desc = f"{out_txt} → {in_txt}（匹配对冲约 {rotation_matched_yi:.0f}亿）"
            # 仅当净方向接近中性时才将主结论定性为纯调仓；若存在明显单向大额净买/净卖，保留净方向结论并叠加调仓提示
            is_pure_rotation = (
                abs(core_net_flow_yi) < core_strong_yi
                and abs(total_net_flow_yi) < strong_yi
                and abs(total_net_flow_yi) <= 0.30 * (gross_inflow_yi + gross_outflow_yi)
            )

        # 7. 五维透明信号灯
        scale_tag = "自动标尺" if day_scale.get("mode") == "auto" else "固定标尺"
        streak_5d_thresh = round(core_strong_yi * 1.75, 1)

        # --- 灯1: 汇金核心池净申赎（严格 12 只） ---
        if total_net_flow_yi is None or core_net_flow_yi is None:
            lamp_shares = {
                "id": "shares_flow",
                "name": "汇金核心池净申赎",
                "state": "na",
                "score": 0,
                "value_text": "待交易所披露",
                "threshold_text": f"{scale_tag}：核心 ±{core_medium_yi:.0f}亿 / ±{core_strong_yi:.0f}亿",
                "detail": "今日交易所 ETF 份额尚未更新，当前仅作盘面量能初判。",
            }
        else:
            abs_core = abs(core_net_flow_yi)
            if is_pure_rotation:
                l1_state = "neutral"
                l1_score = round(_clamp(58 + (gross_inflow_yi + gross_outflow_yi) / 4.0, 58, 88), 1)
                l1_detail = f"汇金12只核心池 {core_net_flow_yi:+.1f}亿，全池双向大额切换（流入+{gross_inflow_yi:.1f}亿 / 流出-{gross_outflow_yi:.1f}亿）。"
            elif abs_core >= core_strong_yi:
                l1_state = "buy" if core_net_flow_yi > 0 else "sell"
                l1_score = round(_clamp(84 + (abs_core - core_strong_yi) / 10.0, 84, 100), 1)
                l1_detail = f"汇金12只核心池净申赎 {core_net_flow_yi:+.1f}亿（全宽基池 {total_net_flow_yi:+.1f}亿），突破当日{scale_tag}大额线 ±{core_strong_yi:.0f}亿。"
            elif abs_core >= core_medium_yi:
                l1_state = "buy" if core_net_flow_yi > 0 else "sell"
                l1_score = round(60 + (abs_core - core_medium_yi) / max(1.0, core_strong_yi - core_medium_yi) * 24, 1)
                l1_detail = f"汇金12只核心池净申赎 {core_net_flow_yi:+.1f}亿（全宽基池 {total_net_flow_yi:+.1f}亿），达到当日{scale_tag}温和线 ±{core_medium_yi:.0f}亿。"
            elif abs(total_net_flow_yi) >= strong_yi:
                l1_state = "neutral"
                l1_score = 46.0
                l1_detail = f"全宽基池 {total_net_flow_yi:+.1f}亿，但汇金12只核心池仅 {core_net_flow_yi:+.1f}亿，增量多在创业板/科创/A500等混合资金池。"
            else:
                l1_state = "neutral"
                l1_score = round(_clamp(abs_core / max(1.0, core_medium_yi) * 42, 5, 42), 1)
                l1_detail = f"汇金12只核心池净申赎 {core_net_flow_yi:+.1f}亿（全池 {total_net_flow_yi:+.1f}亿），处于常态申赎区间。"

            if has_rotation and not is_pure_rotation:
                l1_detail += f"（同时伴随跨宽基对冲调仓约 {rotation_matched_yi:.0f}亿）"

            b_st = streak_info.get("buy_streak", 0)
            s_st = streak_info.get("sell_streak", 0)
            c5d = streak_info.get("core_5d_yi", 0.0)
            if b_st >= 3 and c5d >= streak_5d_thresh:
                l1_detail += f"（已连续 {b_st} 日核心净买入，近5日累计 {c5d:+.1f}亿）"
                l1_score = min(100.0, l1_score + 8.0)
            elif s_st >= 3 and c5d <= -streak_5d_thresh:
                l1_detail += f"（已连续 {s_st} 日核心净卖出，近5日累计 {c5d:+.1f}亿）"
                l1_score = min(100.0, l1_score + 8.0)

            lamp_shares = {
                "id": "shares_flow",
                "name": "汇金核心池净申赎",
                "state": l1_state,
                "score": round(l1_score, 1),
                "value_text": f"核心 {core_net_flow_yi:+.1f}亿 / 全池 {total_net_flow_yi:+.1f}亿",
                "threshold_text": f"{scale_tag}：核心 ±{core_medium_yi:.0f}亿 / ±{core_strong_yi:.0f}亿",
                "detail": l1_detail,
            }

        # --- 灯2: 头部同步共振 ---
        if core_net_flow_yi is not None:
            if sync_active:
                l2_state = "buy" if dominant_buy else "sell"
                l2_score = round(_clamp(56 + g300_dom * 9 + dom_ratio * 24, 60, 100), 1)
            else:
                l2_state = "neutral"
                l2_score = round(_clamp(dom_ratio * 50 + g300_dom * 7, 10, 50), 1)
            sync_val_text = f"300四巨头 {g300_dom}/{g300_total} · 核心 {dom_cnt}/{core_cnt}"
            sync_detail = (
                f"沪深300四巨头中 {g300_buy} 只净申购、{g300_sell} 只净赎回；"
                f"汇金核心池 {core_cnt} 只中 {core_buy} 只净申购、{core_sell} 只净赎回。"
            )
        else:
            if sync_active:
                l2_state = prelim_dir
                l2_score = round(_clamp(58 + g300_dom * 9 + dom_ratio * 20, 60, 94), 1)
            else:
                l2_state = "neutral"
                l2_score = round(_clamp(dom_ratio * 55, 10, 46), 1)
            sync_val_text = f"300放量 {g300_dom}/{g300_total} · 核心 {dom_cnt}/{core_cnt}"
            sync_detail = f"份额未披露，按同步放量统计：沪深300四巨头中有 {g300_dom}/{g300_total} 只显著放量，核心池 {dom_cnt}/{core_cnt} 只显著放量。"

        lamp_sync = {
            "id": "giant_sync",
            "name": "头部同步共振",
            "state": l2_state,
            "score": l2_score,
            "value_text": sync_val_text,
            "threshold_text": "300四巨头 ≥3/4 同步",
            "detail": sync_detail,
        }

        # --- 灯3: 全天量能热度 ---
        inferred_dir = (
            ("buy" if core_net_flow_yi >= 0 else "sell")
            if core_net_flow_yi is not None
            else prelim_dir
        )
        if effective_vol_ratio >= vol_strong:
            l3_state = inferred_dir
            l3_score = round(_clamp(80 + (effective_vol_ratio - vol_strong) * 18, 80, 100), 1)
            l3_detail = f"核心宽基成交 {tot_amt_now/1e8:.0f}亿元，相对近20日中位数{vol_desc}（显著放量确认大资金介入）。"
        elif effective_vol_ratio >= vol_med:
            l3_state = inferred_dir
            l3_score = round(58 + (effective_vol_ratio - vol_med) / max(0.1, vol_strong - vol_med) * 22, 1)
            l3_detail = f"核心宽基成交 {tot_amt_now/1e8:.0f}亿元，相对近20日中位数{vol_desc}。"
        else:
            l3_state = "neutral"
            l3_score = round(_clamp(effective_vol_ratio / vol_med * 40, 8, 42), 1)
            l3_detail = f"核心宽基成交 {tot_amt_now/1e8:.0f}亿元（近20日中位数的 {weighted_vol_ratio:.2f} 倍，{vol_desc}），未出现大资金集中扫货放量。"

        lamp_volume = {
            "id": "volume_heat",
            "name": "全天量能热度",
            "state": l3_state,
            "score": l3_score,
            "value_text": f"{vol_desc} ({tot_amt_now/1e8:.0f}亿)",
            "threshold_text": f"较20日中位数 +{round((vol_med-1)*100)}% / +{round((vol_strong-1)*100)}%",
            "detail": l3_detail,
        }

        # --- 灯4: 尾盘 30 分钟脉冲（金额加权总尾盘比，杜绝小体量 ETF 简单平均失真）---
        tail_rows = [r for r in all_giants if r.get("tail_share") is not None and r.get("tail_amount") is not None]
        tail_share_hi = float(self.thresholds.get("tail_share_high", 0.22))
        tail_ratio_hi = float(self.thresholds.get("tail_ratio_high", 1.80))
        if len(tail_rows) >= 4:
            tot_tail = sum(r["tail_amount"] for r in tail_rows)
            tot_day_t = sum((r.get("amount") or 0.0) for r in tail_rows)
            avg_tail_share = (tot_tail / tot_day_t) if tot_day_t > 0 else sum(r["tail_share"] for r in tail_rows) / len(tail_rows)

            base_rows = [r for r in tail_rows if r.get("tail_ma5") and r["tail_ma5"] > 0]
            if base_rows:
                sum_tail_b = sum(r["tail_amount"] for r in base_rows)
                sum_ma5_b = sum(r["tail_ma5"] for r in base_rows)
                avg_tail_ratio = (sum_tail_b / sum_ma5_b) if sum_ma5_b > 0 else 1.0
            else:
                ratio_rows = [r for r in tail_rows if r.get("tail_ratio") is not None]
                if ratio_rows and sum(r["tail_amount"] for r in ratio_rows) > 0:
                    w_tot = sum(r["tail_amount"] for r in ratio_rows)
                    avg_tail_ratio = sum(r["tail_ratio"] * r["tail_amount"] for r in ratio_rows) / w_tot
                else:
                    avg_tail_ratio = 1.0

            if avg_tail_share >= tail_share_hi or avg_tail_ratio >= tail_ratio_hi:
                l4_state = inferred_dir
                l4_score = round(_clamp(66 + max(avg_tail_share - tail_share_hi, 0) * 180 + max(avg_tail_ratio - tail_ratio_hi, 0) * 18, 66, 98), 1)
                l4_detail = f"14:30~15:00 尾盘成交占全天 {avg_tail_share*100:.1f}%（金额加权为近期尾盘基线 {avg_tail_ratio:.2f} 倍），存在明显尾盘突击动作。"
            else:
                l4_state = "neutral"
                l4_score = round(_clamp((avg_tail_share / tail_share_hi) * 40, 10, 44), 1)
                l4_detail = f"14:30~15:00 尾盘成交占全天 {avg_tail_share*100:.1f}%（金额加权为近期尾盘基线 {avg_tail_ratio:.2f} 倍），尾盘节奏平稳。"
            lamp_tail = {
                "id": "tail_pulse",
                "name": "尾盘30分脉冲",
                "state": l4_state,
                "score": l4_score,
                "value_text": f"占比 {avg_tail_share*100:.1f}% · {avg_tail_ratio:.2f}倍",
                "threshold_text": f"占比≥{tail_share_hi*100:.0f}% 或 ≥{tail_ratio_hi:.1f}倍",
                "detail": l4_detail,
            }
        else:
            lamp_tail = {
                "id": "tail_pulse",
                "name": "尾盘30分脉冲",
                "state": "na",
                "score": 0,
                "value_text": "历史日线模式",
                "threshold_text": f"占比≥{tail_share_hi*100:.0f}% 或 ≥{tail_ratio_hi:.1f}倍",
                "detail": "5分钟线保留最近约5~6个完整交易日，更早历史日期自动按其余四维重归一化计分。",
            }

        # --- 灯5: 60日价格区间位置与逆势方向 ---
        if core_net_flow_yi is not None:
            if (hs300_pos60 <= pos_low or market_pct <= -0.35) and core_net_flow_yi >= core_medium_yi:
                l5_state = "buy"
                l5_score = round(_clamp(74 + (pos_low - min(hs300_pos60, pos_low)) * 0.5 + abs(min(0, market_pct)) * 8, 74, 100), 1)
                l5_val = f"低位逆势托盘 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 处于近60日价格区间 {hs300_pos60:.0f}%（当日 {market_pct:+.2f}%），汇金核心池净申购 {core_net_flow_yi:+.1f}亿，护盘特征明显。"
            elif (hs300_pos60 >= pos_high or market_pct >= 0.60) and core_net_flow_yi <= -core_medium_yi:
                l5_state = "sell"
                l5_score = round(_clamp(72 + max(0, hs300_pos60 - pos_high) * 0.7 + max(0, market_pct) * 8, 72, 100), 1)
                l5_val = f"高位逢高降温 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 处于近60日价格区间 {hs300_pos60:.0f}% 偏高位（当日 {market_pct:+.2f}%），汇金核心池净赎回 {core_net_flow_yi:+.1f}亿，符合逆周期减仓降温特征。"
            elif hs300_pos60 >= pos_ext_high and (total_net_flow_yi or 0) >= medium_yi:
                l5_state = "neutral"
                l5_score = 32.0
                l5_val = f"高位申购警惕追涨 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"指数处于近60日价格区间 {hs300_pos60:.0f}% 极高位，此时申购更可能是市场追涨资金而非国家队托底，模型已自动打折归因。"
            elif core_net_flow_yi >= core_strong_yi:
                l5_state = "buy"
                l5_score = 64.0
                l5_val = f"顺势净加仓 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 处于60日价格区间 {hs300_pos60:.0f}% 中位区（{market_pct:+.2f}%），汇金核心池净流入 {core_net_flow_yi:+.1f}亿。"
            elif core_net_flow_yi <= -core_strong_yi:
                l5_state = "sell"
                l5_score = 62.0
                l5_val = f"核心承压流出 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 处于60日价格区间 {hs300_pos60:.0f}%（{market_pct:+.2f}%），汇金核心池净流出 {core_net_flow_yi:+.1f}亿。"
            else:
                l5_state = "neutral"
                l5_score = 24.0
                l5_val = f"位置中性 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 处于近60日价格区间 {hs300_pos60:.0f}%（当日 {market_pct:+.2f}%），未见明显的低位托盘或高位派发背离。"
        else:
            if (hs300_pos60 <= pos_low or market_pct <= -0.60) and market_pct <= 0.50 and effective_vol_ratio >= vol_med:
                l5_state = "buy"
                l5_score = round(_clamp(65 + abs(min(0, market_pct)) * 10, 65, 92), 1)
                l5_val = f"低位放量疑似托盘 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 位于60日价格区间 {hs300_pos60:.0f}%（{market_pct:+.2f}%），核心宽基显著放量。"
            elif hs300_pos60 >= pos_high and market_pct >= 0.0 and effective_vol_ratio >= vol_strong:
                l5_state = "sell"
                l5_score = 65.0
                l5_val = f"高位巨量留意派发 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 位于60日价格区间 {hs300_pos60:.0f}% 高位且宽基巨量成交，关注晚间份额是否净赎回。"
            elif effective_vol_ratio >= vol_med and market_pct > 0.50:
                l5_state = "neutral"
                l5_score = 52.0
                l5_val = f"放量拉升 · 待份额定性 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 位于60日价格区间 {hs300_pos60:.0f}% 并大涨 {market_pct:+.2f}%，核心宽基显著放量，但无份额前无法区分抢筹申购或趁涨派发，需待晚间官方份额核实。"
            else:
                l5_state = "neutral"
                l5_score = 20.0
                l5_val = f"盘面常态 (60日区间 {hs300_pos60:.0f}%)"
                l5_detail = f"沪深300 位于60日价格区间 {hs300_pos60:.0f}%（{market_pct:+.2f}%），量价关系平稳。"

        lamp_dir = {
            "id": "market_direction",
            "name": "60日价格区间位置",
            "state": l5_state,
            "score": l5_score,
            "value_text": l5_val,
            "threshold_text": f"低位≤{pos_low:.0f}%托盘 / 高位≥{pos_high:.0f}%降温",
            "detail": l5_detail,
        }

        lamps = [lamp_shares, lamp_sync, lamp_volume, lamp_tail, lamp_dir]

        # 8. 异常信号强度分（动态权重，自动跳过 na 项）
        base_weights = {
            "shares_flow": 0.36,
            "giant_sync": 0.22,
            "volume_heat": 0.16,
            "tail_pulse": 0.10,
            "market_direction": 0.16,
        }
        active_w_sum = sum(base_weights[l["id"]] for l in lamps if l["state"] != "na")
        nt_confidence = (
            round(sum(l["score"] * (base_weights[l["id"]] / active_w_sum) for l in lamps if l["state"] != "na"), 1)
            if active_w_sum > 0
            else 0.0
        )

        # 9. 定性结论（仅纯调仓覆盖主结论；存在强净方向时保留主方向 + 附带调仓提示）
        buy_lamps = sum(1 for l in lamps if l["state"] == "buy")
        sell_lamps = sum(1 for l in lamps if l["state"] == "sell")
        b_st = streak_info.get("buy_streak", 0)
        s_st = streak_info.get("sell_streak", 0)
        c5d = streak_info.get("core_5d_yi", 0.0)
        core_super_yi = core_strong_yi * 1.45
        core_min_support = core_medium_yi * 0.45

        if total_net_flow_yi is None or core_net_flow_yi is None:
            if buy_lamps >= 2 and effective_vol_ratio >= vol_med:
                verdict_code, verdict_label = "PRELIM_RESCUE", "盘面疑似护盘放量"
            elif sell_lamps >= 2 and effective_vol_ratio >= vol_strong and hs300_pos60 >= pos_high:
                verdict_code, verdict_label = "PRELIM_SELL", "盘面疑似放量派发"
            elif effective_vol_ratio >= vol_med:
                verdict_code, verdict_label = "PRELIM_WATCH", "盘面显著放量 · 待份额核实"
            else:
                verdict_code, verdict_label = "QUIET", "盘面平稳 · 待份额"
        elif is_pure_rotation:
            verdict_code, verdict_label = "ROTATION", "疑似跨宽基调仓"
        elif (
            (core_net_flow_yi >= core_strong_yi and (total_net_flow_yi >= strong_yi or buy_lamps >= 3))
            or (core_net_flow_yi >= core_super_yi)
        ):
            verdict_code, verdict_label = "STRONG_BUY", "疑似大额护盘买入"
        elif (
            core_net_flow_yi >= core_medium_yi
            or (b_st >= 3 and c5d >= streak_5d_thresh)
            or (total_net_flow_yi >= strong_yi and core_net_flow_yi >= core_min_support)
        ):
            verdict_code, verdict_label = "MODERATE_BUY", "疑似温和净买入" if core_net_flow_yi >= core_medium_yi else "宽基局部净申购"
        elif (
            (core_net_flow_yi <= -core_strong_yi and (total_net_flow_yi <= -strong_yi or sell_lamps >= 3))
            or (core_net_flow_yi <= -core_super_yi)
        ):
            verdict_code = "STRONG_SELL"
            verdict_label = "疑似大额逢高减仓" if hs300_pos60 >= 55.0 else "疑似大额净流出"
        elif (
            core_net_flow_yi <= -core_medium_yi
            or (s_st >= 3 and c5d <= -streak_5d_thresh)
            or (total_net_flow_yi <= -strong_yi and core_net_flow_yi <= -core_min_support)
        ):
            verdict_code, verdict_label = "MODERATE_SELL", "疑似温和净卖出" if core_net_flow_yi <= -core_medium_yi else "宽基局部净赎回"
        else:
            verdict_code, verdict_label = "QUIET", "平静观望"

        # 10. Top Movers
        sorted_etfs = sorted(
            rows,
            key=lambda x: abs(x.get("net_flow_yi") or 0.0) if x.get("net_flow_yi") is not None else (x.get("amount_ratio") or 0.0),
            reverse=True,
        )
        top_movers = [
            {
                "code": r["code"],
                "name": r.get("name") or self.etf_map.get(r["code"], {}).get("name", r["code"]),
                "group_id": r.get("group_id") or self.etf_map.get(r["code"], {}).get("group", ""),
                "pct_chg": r.get("pct_chg"),
                "amount_yi": round((r.get("amount") or 0.0) / 1e8, 2),
                "amount_ratio": r.get("amount_ratio"),
                "net_flow_yi": r.get("net_flow_yi"),
                "tail_share": r.get("tail_share"),
            }
            for r in sorted_etfs[:6]
        ]

        # 11. 摘要 & 透明元数据（完整度、锚点年龄、特征掩码、调仓额）
        auto_summary = self._build_concise_summary(
            verdict_code=verdict_code,
            core_net=core_net_flow_yi,
            total_net=total_net_flow_yi,
            vol_desc=vol_desc,
            hs300_pos60=hs300_pos60,
            rotation_desc=rotation_desc,
            streak_info=streak_info,
            missing_core_codes=missing_core_codes,
        )

        meta = {
            "core_pool_size": len(self.huijin_core_codes),
            "core_net_flow_yi": core_net_flow_yi,
            "completeness_pct": completeness_pct,
            "missing_codes": missing_codes,
            "missing_core_codes": missing_core_codes,
            "anchor_report_date": max(anchor_dates_used) if anchor_dates_used else None,
            "anchor_age_days": round(sum(anchor_ages_used) / len(anchor_ages_used)) if anchor_ages_used else None,
            "has_rotation": has_rotation,
            "rotation_matched_yi": rotation_matched_yi,
            "feature_mask": [l["id"] for l in lamps if l["state"] != "na"],
        }

        return {
            "trade_date": trade_date,
            "data_stage": data_stage,
            "sh_shares_ready": sh_ready,
            "sz_shares_ready": sz_ready,
            "sh_index_close": sh_bm.get("close"),
            "sh_index_pct": sh_pct,
            "hs300_close": hs300_bm.get("close"),
            "hs300_pct": hs300_pct,
            "zz1000_pct": zz1000_bm.get("pct_chg"),
            "cyb_pct": cyb_bm.get("pct_chg"),
            "total_net_flow_yi": total_net_flow_yi,
            "gross_inflow_yi": gross_inflow_yi,
            "gross_outflow_yi": gross_outflow_yi,
            "nt_est_low_yi": nt_low,
            "nt_est_mid_yi": nt_mid,
            "nt_est_high_yi": nt_high,
            "nt_confidence": nt_confidence,
            "verdict_code": verdict_code,
            "verdict_label": verdict_label,
            "rotation_desc": rotation_desc,
            "lamps": lamps,
            "group_flows": group_flows,
            "top_movers": top_movers,
            "scale": day_scale,
            "meta": meta,
            "auto_summary": auto_summary,
        }

    @staticmethod
    def _build_concise_summary(
        *,
        verdict_code: str,
        core_net: float | None,
        total_net: float | None,
        vol_desc: str,
        hs300_pos60: float,
        rotation_desc: str,
        streak_info: dict[str, Any],
        missing_core_codes: list[str],
    ) -> str:
        if total_net is None or core_net is None:
            return f"今日成交{vol_desc}，沪深300位于60日价格区间 {hs300_pos60:.0f}%；待交易所盘后披露份额后核实准确买卖量。"
        if verdict_code == "ROTATION":
            return f"资金在宽基间明显切换（{rotation_desc}），汇金12只核心池净额 {core_net:+.1f}亿，整体{vol_desc}。"
        streak_txt = ""
        if streak_info.get("buy_streak", 0) >= 3:
            streak_txt = f"；核心池已连买 {streak_info['buy_streak']} 天（5日累计 {streak_info['core_5d_yi']:+.1f}亿）"
        elif streak_info.get("sell_streak", 0) >= 3:
            streak_txt = f"；核心池已连卖 {streak_info['sell_streak']} 天（5日累计 {streak_info['core_5d_yi']:+.1f}亿）"
        warn_txt = f"（注：核心池尚缺 {len(missing_core_codes)} 只份额，待补全核实）" if missing_core_codes else ""
        return (
            f"汇金12只核心池净申赎 {core_net:+.1f}亿，全宽基池 {total_net:+.1f}亿；"
            f"市场整体{vol_desc}，沪深300处于60日价格区间 {hs300_pos60:.0f}%{streak_txt}。{warn_txt}"
        )
