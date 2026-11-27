"""核心算法、防折算误判、个人笔记保留、P0/P1 回归与 FastAPI 接口测试."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from src import notifier
from src.analyzer import NationalTeamAnalyzer, _compute_adaptive_thresholds
from src.config import WEB_DIR
from src.db import Database
from src.fetchers.holdings_ann import classify_announcement_category
from src.fetchers.market import _parse_tail_metrics_from_klines
from src.fetchers.shares import fetch_szse_latest_snapshot
from src.server import create_app
from src.sync_service import SyncService


def _seed_synthetic_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "test.db")
    # 基准指数 4 天
    db.upsert_benchmark_rows(
        [
            {"trade_date": "2026-11-18", "code": "000300", "name": "沪深300", "close": 4000, "pct_chg": 0.1},
            {"trade_date": "2026-11-19", "code": "000300", "name": "沪深300", "close": 3940, "pct_chg": -1.5},
            {"trade_date": "2026-11-20", "code": "000300", "name": "沪深300", "close": 3960, "pct_chg": 0.5},
            {"trade_date": "2026-11-23", "code": "000300", "name": "沪深300", "close": 3970, "pct_chg": 0.25},
        ]
    )

    etf_defs = [
        ("510300", "SH", "HS300", 4.0, 500e8),
        ("510310", "SH", "HS300", 4.0, 400e8),
        ("510330", "SH", "HS300", 4.0, 300e8),
        ("159919", "SZ", "HS300", 4.0, 250e8),
        ("510050", "SH", "SZ50", 3.0, 300e8),
        ("512100", "SH", "ZZ1000", 2.5, 200e8),
        ("159845", "SZ", "ZZ1000", 2.5, 150e8),
        ("563360", "SH", "A500", 1.0, 200e8),
        ("159352", "SZ", "A500", 1.0, 200e8),
    ]

    # 先铺 7 天基线让 MA20 有值
    base_dates = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"]
    rows = []
    for d in base_dates:
        for code, ex, gid, px, sh in etf_defs:
            rows.append(
                {
                    "trade_date": d,
                    "code": code,
                    "name": code,
                    "exchange": ex,
                    "group_id": gid,
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "pct_chg": 0.0,
                    "volume": 1e8,
                    "amount": 20e8,
                    "total_shares": sh,
                }
            )

    # 2026-11-19: 典型逆势护盘日（大盘 -1.5%，300四巨头齐大额净申购合计 > +220亿，成交量 3x，尾盘脉冲）
    for code, ex, gid, px, sh in etf_defs:
        add_sh = 13e8 if gid == "HS300" else (6e8 if gid == "SZ50" else 3e8)
        rows.append(
            {
                "trade_date": "2026-11-19",
                "code": code,
                "name": code,
                "exchange": ex,
                "group_id": gid,
                "open": px,
                "high": px,
                "low": px,
                "close": px * 0.985,
                "pct_chg": -1.5,
                "volume": 3e8,
                "amount": 60e8,
                "total_shares": sh + add_sh,
                "tail_amount": 18e8,
                "tail_share": 0.30,
                "tail_ratio": 2.4,
            }
        )

    # 2026-11-20: 跨宽基调仓日（HS300大幅赎回 -72亿，A500/ZZ1000大幅申购 +71.5亿，净额近中性）
    for code, ex, gid, px, sh in etf_defs:
        prev_sh = sh + (13e8 if gid == "HS300" else (6e8 if gid == "SZ50" else 3e8))
        if gid == "HS300":
            delta = -4.5e8  # 每只约 -18亿，4只约 -72亿
        elif gid == "A500":
            delta = 22.0e8  # 每只约 +22亿，2只约 +44亿
        elif gid == "ZZ1000":
            delta = 5.5e8   # 每只约 +13.75亿，2只约 +27.5亿
        else:
            delta = 0.0
        rows.append(
            {
                "trade_date": "2026-11-20",
                "code": code,
                "name": code,
                "exchange": ex,
                "group_id": gid,
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "pct_chg": 0.5,
                "volume": 1.5e8,
                "amount": 25e8,
                "total_shares": prev_sh + delta,
            }
        )

    # 2026-11-23: 模拟 510300 发生 1拆2（份额翻倍、价格减半，总规模不变）-> 必须防误判！
    for code, ex, gid, px, sh in etf_defs:
        prev_sh = sh + (8.5e8 if gid == "HS300" else (6e8 if gid == "SZ50" else (25e8 if gid == "A500" else 8.5e8)))
        if code == "510300":
            new_sh = prev_sh * 2.0
            new_px = px / 2.0
        else:
            new_sh = prev_sh
            new_px = px
        rows.append(
            {
                "trade_date": "2026-11-23",
                "code": code,
                "name": code,
                "exchange": ex,
                "group_id": gid,
                "open": new_px,
                "high": new_px,
                "low": new_px,
                "close": new_px,
                "pct_chg": 0.0,
                "volume": 1e8,
                "amount": 20e8,
                "total_shares": new_sh,
            }
        )

    db.upsert_etf_daily_rows(rows)
    return db


def test_analyzer_buy_rotation_split_and_note_preservation(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    analyzer = NationalTeamAnalyzer(db)
    n = analyzer.recompute_all()
    assert n >= 4

    # 1. 验证逆势强护盘买入 + 持久化每日标尺
    log_buy = db.get_daily_log("2026-11-19")
    assert log_buy is not None
    assert log_buy["data_stage"] == "VERIFIED"
    assert log_buy["verdict_code"] == "STRONG_BUY"
    assert log_buy["total_net_flow_yi"] > 100
    assert log_buy["nt_est_low_yi"] > 0
    assert log_buy["nt_est_high_yi"] >= log_buy["nt_est_low_yi"]
    assert log_buy["nt_confidence"] >= 75
    assert isinstance(log_buy.get("scale"), dict)
    assert log_buy["scale"]["alert_flow_yi"] > 0

    # 2. 验证近中性双向跨宽基调仓识别
    log_rot = db.get_daily_log("2026-11-20")
    assert log_rot is not None
    assert log_rot["verdict_code"] == "ROTATION"
    assert "→" in log_rot["rotation_desc"]

    # 3. 验证 1拆2 折算防误判：2026-11-23 保持平静，且 510300 显式标记 split & NULL
    log_split = db.get_daily_log("2026-11-23")
    assert log_split is not None
    assert log_split["data_stage"] == "VERIFIED"
    assert log_split["verdict_code"] == "QUIET"
    assert abs(log_split["total_net_flow_yi"] or 0.0) < 5.0
    etfs_23 = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-23")}
    assert etfs_23["510300"]["flow_status"] == "split"
    assert etfs_23["510300"]["share_delta"] is None
    assert etfs_23["510300"]["net_flow_yi"] is None

    # 4. 验证个人笔记与标签在重算后完整保留
    db.update_user_note("2026-11-19", "尾盘跟着加了半仓300", "跟买,护盘有效")
    analyzer.recompute_all()
    after = db.get_daily_log("2026-11-19")
    assert after["user_note"] == "尾盘跟着加了半仓300"
    assert after["user_tags_list"] == ["跟买", "护盘有效"]


def test_api_endpoints_and_exports(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    NationalTeamAnalyzer(db).recompute_all()
    svc = SyncService(db)
    app = create_app(db, svc)
    client = TestClient(app)

    r_dash = client.get("/api/dashboard?days=30&benchmark=000300&flow_scope=ALL")
    assert r_dash.status_code == 200
    body = r_dash.json()
    assert body["latest"]["trade_date"] == "2026-11-23"
    assert body["etf_total_count"] == 31
    assert body["core_pool_count"] == 12
    assert len(body["trend"]) >= 4
    assert len(body["benchmarks_meta"]) == 8
    assert all("nt_est_low_yi" in g and "volume_desc" in g for g in body["latest"]["group_flows"])

    r_dash_group = client.get("/api/dashboard?days=30&benchmark=000852&flow_scope=GROUP")
    assert r_dash_group.status_code == 200
    assert r_dash_group.json()["benchmark_selected"]["code"] == "000852"
    assert r_dash_group.json()["flow_scope"] == "GROUP"

    r_note = client.put("/api/logs/2026-11-19/note", json={"user_note": "实盘验证有效", "user_tags": "关键底部"})
    assert r_note.status_code == 200
    assert r_note.json()["log"]["user_note"] == "实盘验证有效"

    r_logs = client.get("/api/logs?only_with_note=true")
    assert r_logs.status_code == 200
    assert r_logs.json()["total"] == 1

    r_md = client.get("/api/logs/export?fmt=markdown")
    assert r_md.status_code == 200
    assert "实盘验证有效" in r_md.text

    r_csv = client.get("/api/logs/export?fmt=csv")
    assert r_csv.status_code == 200
    assert "2026-11-19" in r_csv.text


def test_p0_1_missing_core_blocks_verified_and_undated_szse_rejected(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    # 删除 510300 在 2026-11-19 的份额，模拟核心巨无霸缺数
    with db.connect() as conn:
        conn.execute(
            "UPDATE etf_daily SET total_shares = NULL WHERE trade_date = '2026-11-19' AND code = '510300'"
        )
    NationalTeamAnalyzer(db).recompute_all()
    log = db.get_daily_log("2026-11-19")
    assert log is not None
    assert log["data_stage"] == "SHARES_PARTIAL"
    assert "510300" in (log.get("meta") or {}).get("missing_core_codes", [])

    # 无有效日期列的深交所快照必须拒绝，防止日期污染
    df_no_date = pd.DataFrame([{"基金代码": "159919", "基金份额": "30000000000"}])
    with patch("src.fetchers.shares.ak.fund_etf_scale_szse", return_value=df_no_date):
        assert fetch_szse_latest_snapshot({"159919"}, "2026-11-19") == []

    # 日期不匹配的快照也必须拒绝
    df_old_date = pd.DataFrame([{"日期": "2026-11-18", "基金代码": "159919", "基金份额": "30000000000"}])
    with patch("src.fetchers.shares.ak.fund_etf_scale_szse", return_value=df_old_date):
        assert fetch_szse_latest_snapshot({"159919"}, "2026-11-19") == []

    # 明确匹配日期列时才允许采纳
    df_match_date = pd.DataFrame([{"日期": "2026-11-19", "基金代码": "159919", "基金份额": "30000000000"}])
    with patch("src.fetchers.shares.ak.fund_etf_scale_szse", return_value=df_match_date):
        rows = fetch_szse_latest_snapshot({"159919"}, "2026-11-19")
        assert len(rows) == 1
        assert rows[0]["trade_date"] == "2026-11-19"
        assert rows[0]["share_source"] == "szse"


def test_p0_2_strict_core_pool_point_in_time_holdings_and_no_15pct_floor(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "pit.db")
    db.upsert_holdings(
        [
            {
                "report_date": "2024-12-31",
                "published_at": "2025-03-31",
                "code": "510300",
                "etf_name": "华泰柏瑞沪深300ETF",
                "holder_name": "中央汇金投资有限责任公司",
                "entity_label": "中央汇金投资",
                "entity_category": "核心主力",
                "hold_shares": 300e8,
                "hold_ratio": 60.0,
                "est_value_yi": 1200.0,
            },
            {
                "report_date": "2025-12-31",
                "published_at": "2026-03-31",
                "code": "510300",
                "etf_name": "华泰柏瑞沪深300ETF",
                "holder_name": "中央汇金资产管理有限责任公司",
                "entity_label": "中央汇金资产管理",
                "entity_category": "核心主力",
                "hold_shares": 450e8,
                "hold_ratio": 75.0,
                "est_value_yi": 1800.0,
            },
            # 非汇金核心池高持仓（创业板 & 国新央企）绝不能混入 huijin_core
            {
                "report_date": "2025-12-31",
                "published_at": "2026-03-31",
                "code": "159915",
                "etf_name": "易方达创业板ETF",
                "holder_name": "中央汇金资产管理有限责任公司",
                "entity_label": "中央汇金资产管理",
                "entity_category": "核心主力",
                "hold_shares": 100e8,
                "hold_ratio": 54.03,
                "est_value_yi": 250.0,
            },
            {
                "report_date": "2025-12-31",
                "published_at": "2026-03-31",
                "code": "560170",
                "etf_name": "国新央企科技引领ETF",
                "holder_name": "国新投资有限公司",
                "entity_label": "国新投资",
                "entity_category": "国新/诚通",
                "hold_shares": 10e8,
                "hold_ratio": 66.30,
                "est_value_yi": 10.0,
            },
            # 低实名比例 ETF（5.83%）绝不能被硬抬高到 15%（P1-1）
            {
                "report_date": "2025-12-31",
                "published_at": "2026-03-31",
                "code": "510100",
                "etf_name": "易方达上证50ETF",
                "holder_name": "中央汇金资产管理有限责任公司",
                "entity_label": "中央汇金资产管理",
                "entity_category": "核心主力",
                "hold_shares": 5e8,
                "hold_ratio": 5.83,
                "est_value_yi": 10.0,
            },
        ]
    )

    analyzer = NationalTeamAnalyzer(db)
    assert len(analyzer.huijin_core_codes) == 12
    assert analyzer._is_huijin_core("510300") is True
    assert analyzer._is_huijin_core("159915") is False
    assert analyzer._is_huijin_core("560170") is False

    # 时点一致查询：2025-11-20 只能看到 2024-12-31 报告，不能穿越读取 2025-12-31（2026-03-31 发布）
    timeline = db.get_holdings_timeline()
    snap_2025 = analyzer._resolve_point_in_time_anchor("510300", "2025-11-20", timeline)
    assert snap_2025 is not None
    assert snap_2025["report_date"] == "2024-12-31"
    assert abs(snap_2025["nt_ratio"] - 0.60) < 1e-4

    snap_2026 = analyzer._resolve_point_in_time_anchor("510300", "2026-04-01", timeline)
    assert snap_2026 is not None
    assert snap_2026["report_date"] == "2025-12-31"
    assert abs(snap_2026["nt_ratio"] - 0.75) < 1e-4

    # 取消 15% 硬底线
    overrides = db.get_nt_weight_overrides()
    assert abs(overrides["510100"] - 0.0583) < 1e-4


def test_p0_3_stale_derived_cleared_on_split_and_pct_chg_preserved(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    analyzer = NationalTeamAnalyzer(db)

    # 先把 2026-11-23 的 510300 改成未拆价（close=4.0 但份额翻倍），制造一笔旧派生大额净买入
    db.upsert_etf_daily_rows(
        [{"trade_date": "2026-11-23", "code": "510300", "close": 4.0, "pct_chg": 0.0}]
    )
    analyzer.recompute_all()
    before = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-23")}["510300"]
    assert before["net_flow_yi"] is not None and before["net_flow_yi"] > 500
    assert before["flow_status"] == "ok"

    # 修正价格为拆份后的 2.0，再次重算必须显式清零旧派生值为 NULL（绝不被 COALESCE 残留）
    db.upsert_etf_daily_rows([{"trade_date": "2026-11-23", "code": "510300", "close": 2.0}])
    analyzer.recompute_all()
    after = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-23")}["510300"]
    assert after["flow_status"] == "split"
    assert after["share_delta"] is None
    assert after["net_flow_yi"] is None

    # 增量拉取首日 pct_chg=None 绝不覆盖库中已有真实涨跌幅 -1.5
    db.upsert_etf_daily_rows([{"trade_date": "2026-11-19", "code": "510300", "pct_chg": None}])
    row_19 = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-19")}["510300"]
    assert row_19["pct_chg"] == -1.5

    # 库内 pct_chg 缺失时，recompute_all 按库内前收补算精确涨跌幅
    with db.connect() as conn:
        conn.execute(
            "UPDATE etf_daily SET pct_chg = NULL WHERE trade_date = '2026-11-19' AND code = '510310'"
        )
    analyzer.recompute_all()
    row_310 = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-19")}["510310"]
    assert row_310["pct_chg"] is not None and abs(row_310["pct_chg"] - (-1.5)) < 0.05


def test_p0_4_long_offline_sync_window_and_accurate_counts(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "offline.db")
    # 预置 25 天已核实日志，进入增量模式（is_initial_backfill == False）
    dates_all = [f"2026-{(i // 20) + 1:02d}-{(i % 20) + 1:02d}" for i in range(95)]
    old_logs = [
        {
            "trade_date": d,
            "data_stage": "VERIFIED",
            "verdict_code": "QUIET",
            "verdict_label": "平静无异动",
            "total_net_flow_yi": 0.0,
        }
        for d in dates_all[:25]
    ]
    db.publish_analysis_results([], old_logs)

    recorded_bm_days: list[int] = []
    recorded_etf_days: list[int] = []

    def fake_bm(code: str, name: str, market: str, days: int = 300) -> list[dict[str, Any]]:
        recorded_bm_days.append(days)
        return [
            {"trade_date": d, "code": code, "name": name, "close": 4000.0, "pct_chg": 0.1}
            for d in dates_all
        ]

    def fake_etf_daily(etfs: list[dict[str, Any]], days: int = 320, progress_cb: Any = None) -> list[dict[str, Any]]:
        recorded_etf_days.append(days)
        rows = []
        for d in dates_all[-20:]:
            for e in etfs[:2]:
                rows.append(
                    {
                        "trade_date": d,
                        "code": e["code"],
                        "name": e["name"],
                        "exchange": e["exchange"],
                        "group_id": e["group"],
                        "close": 4.0,
                        "pct_chg": 0.0,
                        "amount": 10e8,
                    }
                )
        return rows

    svc = SyncService(db)
    with (
        patch("src.sync_service.fetch_benchmark_daily", side_effect=fake_bm),
        patch("src.sync_service.fetch_all_etf_daily", side_effect=fake_etf_daily),
        patch("src.sync_service.fetch_all_tail_metrics", return_value=[]),
        patch("src.sync_service.fetch_sse_shares_for_dates", return_value=[]),
        patch("src.sync_service.fetch_szse_shares_range", return_value=[]),
        patch("src.sync_service.fetch_szse_latest_snapshot", return_value=[]),
        patch("src.sync_service.fetch_all_etf_top_holders", return_value=[]),
        patch("src.sync_service.fetch_cninfo_announcements", return_value=[]),
    ):
        res = svc.run_smart_sync(trigger="boot", force=False)

    assert res["status"] == "ok"
    # Step 1 必须拉满完整窗口（>= 300天）以获取完整交易日历
    assert recorded_bm_days and min(recorded_bm_days) >= 300
    # Step 2 因为缺失起点距离最新日有 70 个交易日，ETF 日线回补跨度必须自动扩展超过 45 天
    assert recorded_etf_days and recorded_etf_days[0] >= 100
    # 份额抓取返回为空时，不得把未核实天数误报为 verified_count
    assert res["verified_count"] < len(dates_all) - 1


def test_p0_5_outbox_retry_on_failure_and_cross_process_lease(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    NationalTeamAnalyzer(db).recompute_all()
    svc = SyncService(db)
    settings = db.get_settings()
    settings["notify_win_toast"] = True
    settings["feishu_enabled"] = True
    settings["feishu_webhook"] = "https://open.feishu.cn/open-apis/bot/v2/hook/test"

    calls: list[list[str]] = []

    def fake_dispatch_first(
        st: dict[str, Any],
        title: str,
        body: str,
        launch_url: str = "",
        extra: dict[str, Any] | None = None,
        channels: list[str] | None = None,
    ) -> dict[str, tuple[bool, str]]:
        calls.append(list(channels or []))
        return {"win_toast": (True, "ok"), "feishu": (False, "network timeout")}

    # 第 1 次：win_toast 成功，feishu 失败 -> feishu 绝不能被永久去重
    with patch("src.sync_service.dispatch_notification", side_effect=fake_dispatch_first):
        sent1 = svc._dispatch_daily_alerts(["2026-11-19"], settings)
    assert sent1 == ["2026-11-19"]
    assert set(calls[0]) == {"win_toast", "feishu"}
    assert db.is_channel_sent("2026-11-19:VERIFIED", "win_toast") is True
    assert db.is_channel_sent("2026-11-19:VERIFIED", "feishu") is False

    # 第 2 次：只应重试失败的 feishu，不能重复弹窗 win_toast
    def fake_dispatch_second(
        st: dict[str, Any],
        title: str,
        body: str,
        launch_url: str = "",
        extra: dict[str, Any] | None = None,
        channels: list[str] | None = None,
    ) -> dict[str, tuple[bool, str]]:
        calls.append(list(channels or []))
        return {"feishu": (True, "ok")}

    with patch("src.sync_service.dispatch_notification", side_effect=fake_dispatch_second):
        sent2 = svc._dispatch_daily_alerts(["2026-11-19"], settings)
    assert sent2 == ["2026-11-19"]
    assert calls[1] == ["feishu"]
    assert db.is_channel_sent("2026-11-19:VERIFIED", "feishu") is True

    # 第 3 次：全通道均已送达，不再触发发送
    with patch("src.sync_service.dispatch_notification") as mock_disp3:
        sent3 = svc._dispatch_daily_alerts(["2026-11-19"], settings)
        assert sent3 == []
        mock_disp3.assert_not_called()

    # 跨实例 SQLite 租约互斥验证
    svc2 = SyncService(db)
    assert db.acquire_sync_lease(svc._owner_id, ttl_seconds=600) is True
    try:
        busy_res = svc2.recompute_with_lock()
        assert busy_res["status"] == "busy"
    finally:
        db.release_sync_lease(svc._owner_id)
    ok_res = svc2.recompute_with_lock()
    assert ok_res["status"] == "ok"


def test_p0_6_strict_settings_validation_and_lock_release_on_error(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    NationalTeamAnalyzer(db).recompute_all()
    svc = SyncService(db)
    client = TestClient(create_app(db, svc))

    # 非法类型与非法 URL scheme 必须返回 422
    assert client.put("/api/settings", json={"backfill_days": "not_an_int"}).status_code == 422
    assert client.put("/api/settings", json={"backfill_days": 9999}).status_code == 422
    assert client.put("/api/settings", json={"custom_webhook_url": "file:///etc/passwd"}).status_code == 422
    assert client.put("/api/settings", json={"feishu_webhook": "javascript:alert(1)"}).status_code == 422
    # 合法局域网 HTTP webhook 允许通过
    assert client.put("/api/settings", json={"custom_webhook_url": "http://127.0.0.1:9999/hook"}).status_code == 200

    # 即使数据库被手动写入脏值，run_smart_sync 异常后也必须完整释放进程锁和租约（不再永久死锁）
    db.update_settings({"backfill_days": "corrupted_value"})
    err_res = svc.run_smart_sync(trigger="manual")
    assert err_res["status"] == "error"
    assert svc.state["is_running"] is False
    # 立刻再调用 recompute_with_lock 必须能拿到锁并成功，证明无死锁
    rec_res = svc.recompute_with_lock()
    assert rec_res["status"] == "ok"


def test_p0_7_and_p0_8_dashboard_strict_benchmark_and_vendor_asset(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    NationalTeamAnalyzer(db).recompute_all()
    client = TestClient(create_app(db, SyncService(db)))

    # 选中未入库的 000688（科创50）时，绝不能偷换为 hs300_close
    r_kc = client.get("/api/dashboard?days=30&benchmark=000688&flow_scope=ALL")
    assert r_kc.status_code == 200
    trend_kc = r_kc.json()["trend"]
    assert len(trend_kc) >= 4
    assert all(pt["benchmark_close"] is None and pt["benchmark_pct"] is None for pt in trend_kc)
    assert any(pt["hs300_close"] is not None for pt in trend_kc)

    # 上证指数（000001）请求 GROUP 必须自动回落到 ALL
    r_sh = client.get("/api/dashboard?days=30&benchmark=000001&flow_scope=GROUP")
    assert r_sh.status_code == 200
    assert r_sh.json()["flow_scope"] == "ALL"

    # 本地离线 ECharts 资源必须存在且非空
    echarts_path = WEB_DIR / "vendor" / "echarts.min.js"
    assert echarts_path.exists()
    assert echarts_path.stat().st_size > 500_000


def test_p1_2_adaptive_scale_smooth_ramp_no_cliff() -> None:
    ratios = [0.0020 + (i % 5) * 0.0005 for i in range(80)]
    prev_str = None
    weights = {}
    thresholds_by_n = {}
    for n in (14, 15, 24, 25, 26, 60):
        sc = _compute_adaptive_thresholds(
            auto_enabled=True,
            static_cfg={},
            pool_aum_yi=12000.0,
            core_aum_yi=8000.0,
            hist_pool_ratios=ratios[:n],
            hist_core_ratios=ratios[:n],
        )
        weights[n] = sc["hist_weight"]
        thresholds_by_n[n] = sc["strong_flow_yi"]
        prev_str = sc["strong_flow_yi"]

    assert prev_str is not None
    assert weights[14] == 0.0 and weights[15] == 0.0
    assert 0.15 < weights[24] < weights[25] < weights[26] < weights[60] == 0.50
    # 第 24 天到第 25 天必须平滑过渡，不允许出现旧版 20% 断崖跳变
    jump_pct = abs(thresholds_by_n[25] - thresholds_by_n[24]) / thresholds_by_n[24]
    assert jump_pct < 0.03


def test_p1_3_tail_parser_rejects_half_day_and_truncated_day() -> None:
    def _make_bars(date_yyyymmdd: str, hhmm_list: list[str], vol: float = 1000.0) -> list[list[Any]]:
        return [[f"{date_yyyymmdd}{hh}", "4.0", "4.0", "4.0", "4.0", str(vol)] for hh in hhmm_list]

    morning_24 = [
        f"{h:02d}{m:02d}"
        for h, m in (
            [(9, m) for m in range(35, 60, 5)]
            + [(10, m) for m in range(0, 60, 5)]
            + [(11, m) for m in range(0, 35, 5)]
        )
    ]
    afternoon_24 = [
        f"{h:02d}{m:02d}"
        for h, m in (
            [(13, m) for m in range(5, 60, 5)]
            + [(14, m) for m in range(0, 60, 5)]
            + [(15, 0)]
        )
    ]
    assert len(morning_24) == 24 and len(afternoon_24) == 24

    klines = (
        _make_bars("20261118", afternoon_24)                # 截断首日（缺上午）-> 拒绝
        + _make_bars("20261119", morning_24 + afternoon_24) # 完整 48 根 -> 接受
        + _make_bars("20261120", morning_24)                # 仅上午半日 -> 拒绝
    )
    res = _parse_tail_metrics_from_klines("510300", klines)
    assert [r["trade_date"] for r in res] == ["2026-11-19"]
    assert abs(res[0]["tail_share"] - (6 / 48)) < 1e-4


def test_p1_4_and_p1_6_decoupled_rotation_tags_retention_csv_and_https_pushplus(tmp_path: Path) -> None:
    db = _seed_synthetic_db(tmp_path)
    # 新增 2026-11-24：大额净买入（HS300 +180亿）同时伴随 A500 大额赎回（-65亿），净额 +115亿
    # 必须保持 BUY 主结论，绝不能被误判为中性 ROTATION，同时保留 rotation_desc 标注调仓！
    db.upsert_benchmark_rows(
        [{"trade_date": "2026-11-24", "code": "000300", "name": "沪深300", "close": 3950, "pct_chg": -0.5}]
    )
    prev_rows = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-23")}
    day24_rows = []
    for code, r in prev_rows.items():
        sh = r["total_shares"]
        px = r["close"]
        if r["group_id"] == "HS300":
            # 每只买 +45亿（4只合计 +180亿）
            delta_sh = (45.0 * 1e8) / px
        elif r["group_id"] == "A500":
            # 每只卖 -32.5亿（2只合计 -65亿）
            delta_sh = (-32.5 * 1e8) / px
        else:
            delta_sh = 0.0
        day24_rows.append(
            {
                "trade_date": "2026-11-24",
                "code": code,
                "name": code,
                "exchange": r["exchange"],
                "group_id": r["group_id"],
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "pct_chg": -0.5,
                "volume": 2e8,
                "amount": 40e8,
                "total_shares": sh + delta_sh,
            }
        )
    db.upsert_etf_daily_rows(day24_rows)

    analyzer = NationalTeamAnalyzer(db)
    analyzer.recompute_all()
    log24 = db.get_daily_log("2026-11-24")
    assert log24 is not None
    assert log24["verdict_code"] in ("STRONG_BUY", "MODERATE_BUY")
    assert "→" in (log24.get("rotation_desc") or "")

    # 仅填标签不填正文时：在 min_log_date 裁剪后仍保留，且 only_with_note=true 能筛出
    db.update_user_note("2026-11-19", "", "重要样本")
    db.update_user_note("2026-11-20", "=SUM(1,2)", "测试公式注入")
    analyzer.recompute_all(min_log_date="2026-11-23")
    assert db.get_daily_log("2026-11-19") is not None
    assert db.get_daily_log("2026-11-19")["user_tags"] == "重要样本"

    client = TestClient(create_app(db, SyncService(db)))
    r_noted = client.get("/api/logs?only_with_note=true")
    assert r_noted.status_code == 200
    noted_dates = {it["trade_date"] for it in r_noted.json()["items"]}
    assert {"2026-11-19", "2026-11-20"} <= noted_dates

    # CSV 导出：文本列中和公式注入（'=SUM(1,2)），数值列保持原生负数不加单引号
    r_csv = client.get("/api/logs/export?fmt=csv")
    assert r_csv.status_code == 200
    text = r_csv.content.decode("utf-8-sig")
    parsed = list(csv.DictReader(io.StringIO(text)))
    row20 = next(r for r in parsed if r["交易日期"] == "2026-11-20")
    assert row20["个人复盘备注"] == "'=SUM(1,2)"
    assert not row20["全池净申赎(亿元)"].startswith("'")

    # 公告事件分类测试
    assert classify_announcement_category("中央汇金免于发出要约的法律意见书") == "主体股权/要约事件"
    assert classify_announcement_category("中央汇金公司增持交易型开放式指数基金（ETF）公告") == "ETF/基金增持"

    # PushPlus 强制使用 HTTPS
    called_urls: list[str] = []

    class _DummyResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"code": 200, "msg": "ok"}

    def fake_post(url: str, **kwargs: Any) -> _DummyResp:
        called_urls.append(url)
        return _DummyResp()

    with patch("src.notifier.requests.post", side_effect=fake_post):
        ok, _ = notifier._send_personal_wechat("pushplus", "tok123", "标题", "正文")
        assert ok is True
        assert called_urls and called_urls[0].startswith("https://www.pushplus.plus/")


def test_prelim_low_surge_not_mislabeled_as_sell_and_verified_only_alerts(tmp_path: Path) -> None:
    """低位暴涨无份额日不得误判为 PRELIM_SELL，且自动提醒仅对 VERIFIED 结论触发."""
    db = _seed_synthetic_db(tmp_path)
    # 铺一段前期更高点让 2026-11-24 位于 60 日低位 (~20%)，当日大涨 +4.17% 且显著放量但尚无份额
    db.upsert_benchmark_rows(
        [
            {"trade_date": "2026-11-10", "code": "000300", "name": "沪深300", "close": 4600, "pct_chg": 0.0},
            {"trade_date": "2026-11-24", "code": "000300", "name": "沪深300", "close": 4100, "pct_chg": 4.17},
        ]
    )
    prev_rows = {r["code"]: r for r in db.get_etf_daily_by_date("2026-11-23")}
    day24_rows = []
    for code, r in prev_rows.items():
        px = r["close"] * 1.042
        day24_rows.append(
            {
                "trade_date": "2026-11-24",
                "code": code,
                "name": code,
                "exchange": r["exchange"],
                "group_id": r["group_id"],
                "open": r["close"],
                "high": px,
                "low": r["close"],
                "close": px,
                "pct_chg": 4.2,
                "volume": 3e8,
                "amount": 50e8,  # 显著放量 (+150%)
                "total_shares": None,  # 傍晚尚未披露官方份额
            }
        )
    db.upsert_etf_daily_rows(day24_rows)

    NationalTeamAnalyzer(db).recompute_all()
    log24 = db.get_daily_log("2026-11-24")
    assert log24 is not None
    assert log24["data_stage"] == "MARKET_PRELIM"
    assert log24["verdict_code"] == "PRELIM_WATCH"
    assert "待份额" in log24["verdict_label"]
    assert log24["verdict_code"] != "PRELIM_SELL"

    # 自动提醒仅针对 VERIFIED 核实结论触发，绝不在 MARKET_PRELIM 提前弹窗
    svc = SyncService(db)
    settings = db.get_settings()
    settings["notify_win_toast"] = True
    with patch("src.sync_service.dispatch_notification") as mock_disp:
        sent = svc._dispatch_daily_alerts(["2026-11-24"], settings)
        assert sent == []
        mock_disp.assert_not_called()


def test_pigeonhole_inventory_floor_only_on_downward_breach(tmp_path: Path) -> None:
    """鸽笼库存底线：砸穿实名持仓新低时托起下限，反弹后区间内普通卖出不误触发."""
    helper = {
        "510300": {
            "dates": ["2025-12-31", "2026-01-15", "2026-01-20", "2026-06-30", "2026-10-15", "2026-10-20"],
            "shares": [880e8, 760e8, 600e8, 200e8, 420e8, 390e8],
            "cum_abs": [0, 120e8, 280e8, 680e8, 900e8, 930e8],
        }
    }
    fn = NationalTeamAnalyzer._pigeonhole_forced_sell_fraction
    # 1. 仍在 H_0=735亿 之上（880 -> 760）：非国家队筹码可解释，不触发
    assert fn("510300", "2025-12-31", 735e8, "2026-01-15", 760e8, -120e8, helper) == 0.0
    # 2. 从 760 砸穿 735 至 600（净赎回 160亿份，其中 135亿份跌破 H_0）：forced_frac = 135/160 = 0.84375
    f_breach = fn("510300", "2025-12-31", 735e8, "2026-01-20", 600e8, -160e8, helper)
    assert abs(f_breach - (135.0 / 160.0)) < 1e-4
    # 3. 此前已跌到过 200亿，下半年反弹到 420 后小卖到 390：eff_cap = min(735, 200) = 200 < 390，绝不误触发！
    assert fn("510300", "2025-12-31", 735e8, "2026-10-20", 390e8, -30e8, helper) == 0.0


def test_auto_holdings_cadence_health_monitor_and_schedule_validation(tmp_path: Path) -> None:
    """验证 4月初/9月初底牌自动刷新期判定、数据源异常自检弹窗与定时时间格式校验."""
    from datetime import datetime
    from src.sync_service import expected_latest_report_date

    assert expected_latest_report_date(datetime(2026, 4, 5)) == "2025-12-31"
    assert expected_latest_report_date(datetime(2026, 8, 31)) == "2025-12-31"
    assert expected_latest_report_date(datetime(2026, 9, 1)) == "2026-06-30"
    assert expected_latest_report_date(datetime(2027, 2, 15)) == "2026-06-30"

    db = _seed_synthetic_db(tmp_path)
    NationalTeamAnalyzer(db).recompute_all()
    svc = SyncService(db)

    # 模拟连续 2 个已完成交易日处于 SHARES_PARTIAL（交易所接口故障）
    with db.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE daily_logs SET data_stage = 'SHARES_PARTIAL', sz_shares_ready = 0 WHERE trade_date IN ('2026-11-20', '2026-11-23')"
        )
    health = svc.evaluate_system_health(now=datetime(2026, 11, 24, 21, 35))
    assert health["status"] == "error"
    assert any(i["code"] == "CONSECUTIVE_INCOMPLETE_SHARES" for i in health["issues"])

    settings = db.get_settings()
    settings["notify_win_toast"] = True
    settings["notify_health_alerts"] = True
    with patch("src.sync_service.dispatch_notification", return_value={"win_toast": (True, "ok")}) as mock_h:
        assert svc._dispatch_health_alert(settings, datetime(2026, 11, 24, 21, 35)) is True
        assert mock_h.call_count == 1
        # 当天内重复检查自动去重，不刷屏
        assert svc._dispatch_health_alert(settings, datetime(2026, 11, 24, 22, 0)) is False
        assert mock_h.call_count == 1

    # 非法定时时间格式必须返回 HTTP 422
    client = TestClient(create_app(db, svc))
    assert client.put("/api/settings", json={"auto_schedule_time": "25:99"}).status_code == 422
