"""SQLite 本地数据库层（WAL 模式，线程与跨进程安全，支持原子发布、通知 Outbox 与独立批注表）."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from src.config import DB_PATH, DEFAULT_SETTINGS


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == 259  # STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _statutory_published_at(report_date: str) -> str:
    """若数据源未提供实际披露日，按基金信息披露法定最晚日期推算有效可用日，避免历史前视偏差."""
    if not report_date or len(report_date) < 10:
        return report_date or ""
    year = int(report_date[:4])
    mmdd = report_date[5:10]
    if mmdd == "12-31":
        # 年报法定披露截止次年 3 月 31 日
        return f"{year + 1}-03-31"
    if mmdd == "06-30":
        # 半年报法定披露截止当年 8 月 31 日
        return f"{year}-08-31"
    return report_date


class Database:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None if immediate else "",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, col: str, col_def: str) -> None:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS etf_daily (
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    exchange TEXT,
                    group_id TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    pct_chg REAL,
                    volume REAL,
                    amount REAL,
                    amount_is_estimated INTEGER DEFAULT 0,
                    nav REAL,
                    total_shares REAL,
                    share_delta REAL,
                    net_flow_yi REAL,
                    flow_status TEXT DEFAULT '',
                    amount_ma20 REAL,
                    amount_ratio REAL,
                    tail_amount REAL,
                    tail_share REAL,
                    tail_ma5 REAL,
                    tail_ratio REAL,
                    share_source TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (trade_date, code)
                );

                CREATE INDEX IF NOT EXISTS idx_etf_daily_date ON etf_daily(trade_date);
                CREATE INDEX IF NOT EXISTS idx_etf_daily_code_date ON etf_daily(code, trade_date);

                CREATE TABLE IF NOT EXISTS benchmark_daily (
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    close REAL,
                    pct_chg REAL,
                    amount REAL,
                    updated_at TEXT,
                    PRIMARY KEY (trade_date, code)
                );

                CREATE INDEX IF NOT EXISTS idx_benchmark_date ON benchmark_daily(trade_date);

                CREATE TABLE IF NOT EXISTS daily_logs (
                    trade_date TEXT PRIMARY KEY,
                    data_stage TEXT NOT NULL,
                    sh_shares_ready INTEGER DEFAULT 0,
                    sz_shares_ready INTEGER DEFAULT 0,
                    sh_index_close REAL,
                    sh_index_pct REAL,
                    hs300_close REAL,
                    hs300_pct REAL,
                    zz1000_pct REAL,
                    cyb_pct REAL,
                    total_net_flow_yi REAL,
                    gross_inflow_yi REAL,
                    gross_outflow_yi REAL,
                    nt_est_low_yi REAL,
                    nt_est_mid_yi REAL,
                    nt_est_high_yi REAL,
                    nt_confidence REAL,
                    verdict_code TEXT,
                    verdict_label TEXT,
                    rotation_desc TEXT,
                    lamps_json TEXT,
                    group_flows_json TEXT,
                    top_movers_json TEXT,
                    scale_json TEXT DEFAULT '{}',
                    meta_json TEXT DEFAULT '{}',
                    auto_summary TEXT,
                    user_note TEXT DEFAULT '',
                    user_tags TEXT DEFAULT '',
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS user_notes (
                    trade_date TEXT PRIMARY KEY,
                    user_note TEXT DEFAULT '',
                    user_tags TEXT DEFAULT '',
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS announcements (
                    ann_id TEXT PRIMARY KEY,
                    publish_date TEXT,
                    title TEXT,
                    org_name TEXT,
                    matched_keyword TEXT,
                    event_category TEXT DEFAULT '主体股权事件',
                    url TEXT,
                    notified INTEGER DEFAULT 0,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_ann_date ON announcements(publish_date DESC);

                CREATE TABLE IF NOT EXISTS etf_holdings (
                    code TEXT NOT NULL,
                    etf_name TEXT,
                    report_date TEXT NOT NULL,
                    published_at TEXT DEFAULT '',
                    holder_name TEXT NOT NULL,
                    entity_label TEXT,
                    entity_category TEXT,
                    hold_shares REAL,
                    hold_ratio REAL,
                    est_value_yi REAL,
                    updated_at TEXT,
                    PRIMARY KEY (code, report_date, holder_name)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS notifications_sent (
                    dedup_key TEXT PRIMARY KEY,
                    trade_date TEXT,
                    stage TEXT,
                    title TEXT,
                    body TEXT,
                    channels_json TEXT,
                    sent_at TEXT
                );

                CREATE TABLE IF NOT EXISTS notification_outbox (
                    dedup_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    trade_date TEXT,
                    stage TEXT,
                    title TEXT,
                    body TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    claimed_at TEXT,
                    sent_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (dedup_key, channel)
                );

                CREATE TABLE IF NOT EXISTS sync_leases (
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT,
                    message TEXT
                );
                """
            )
            # 幂等迁移：平滑升级旧库字段，零数据丢失
            self._ensure_column(conn, "etf_daily", "amount_is_estimated", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "etf_daily", "flow_status", "TEXT DEFAULT ''")
            self._ensure_column(conn, "etf_daily", "tail_ma5", "REAL")
            self._ensure_column(conn, "daily_logs", "scale_json", "TEXT DEFAULT '{}'")
            self._ensure_column(conn, "daily_logs", "meta_json", "TEXT DEFAULT '{}'")
            self._ensure_column(conn, "announcements", "event_category", "TEXT DEFAULT '主体股权事件'")
            self._ensure_column(conn, "etf_holdings", "published_at", "TEXT DEFAULT ''")

            # 把旧库 daily_logs 中已有的个人批注/标签同步迁移到独立 user_notes 表
            conn.execute(
                """
                INSERT OR IGNORE INTO user_notes (trade_date, user_note, user_tags, updated_at)
                SELECT trade_date, COALESCE(user_note, ''), COALESCE(user_tags, ''), COALESCE(updated_at, ?)
                FROM daily_logs
                WHERE TRIM(COALESCE(user_note, '')) != '' OR TRIM(COALESCE(user_tags, '')) != ''
                """,
                (_now_str(),),
            )

            # 把旧库 notifications_sent 中已成功送达的记录幂等迁移至 notification_outbox，防止升级后重复弹窗
            sent_rows = conn.execute(
                "SELECT dedup_key, trade_date, stage, title, body, channels_json, sent_at FROM notifications_sent"
            ).fetchall()
            valid_channels = {"win_toast", "feishu", "wecom", "personal_wechat", "custom_webhook"}
            for sr in sent_rows:
                try:
                    chs = json.loads(sr["channels_json"] or "[]")
                except Exception:
                    chs = []
                if not isinstance(chs, list):
                    continue
                for ch in chs:
                    if ch in valid_channels:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO notification_outbox (
                                dedup_key, channel, trade_date, stage, title, body,
                                status, attempts, last_error, claimed_at, sent_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'sent', 1, '', ?, ?, ?)
                            """,
                            (
                                sr["dedup_key"],
                                ch,
                                sr["trade_date"] or "",
                                sr["stage"] or "",
                                sr["title"] or "",
                                sr["body"] or "",
                                sr["sent_at"] or _now_str(),
                                sr["sent_at"] or _now_str(),
                                sr["sent_at"] or _now_str(),
                            ),
                        )

    # ---------- Cross-process Sync Leases ----------
    def acquire_sync_lease(
        self, owner_id: str, ttl_seconds: int = 900, lease_name: str = "main_sync"
    ) -> bool:
        """基于 SQLite BEGIN IMMEDIATE 的跨进程+多实例同步互斥租约."""
        now_dt = datetime.now()
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        exp_s = (now_dt + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        pid = os.getpid()
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT owner_id, owner_pid, expires_at FROM sync_leases WHERE lease_name = ?",
                (lease_name,),
            ).fetchone()
            if row is not None:
                existing_owner = row["owner_id"]
                existing_pid = int(row["owner_pid"] or 0)
                exp_dt = _parse_dt(row["expires_at"])
                not_expired = exp_dt is not None and exp_dt > now_dt
                if existing_owner != owner_id and not_expired and _is_pid_alive(existing_pid):
                    return False
            conn.execute(
                """
                INSERT INTO sync_leases (lease_name, owner_id, owner_pid, acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    owner_pid = excluded.owner_pid,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (lease_name, owner_id, pid, now_s, exp_s),
            )
            return True

    def release_sync_lease(self, owner_id: str, lease_name: str = "main_sync") -> None:
        with self.connect(immediate=True) as conn:
            conn.execute(
                "DELETE FROM sync_leases WHERE lease_name = ? AND owner_id = ?",
                (lease_name, owner_id),
            )

    def mark_stale_sync_runs_interrupted(self) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE sync_runs
                SET status = 'interrupted', finished_at = ?, message = COALESCE(message, '') || ' (上次未正常完成)'
                WHERE status = 'running'
                """,
                (_now_str(),),
            )
            return int(cur.rowcount)

    # ---------- Settings ----------
    def get_settings(self) -> dict[str, Any]:
        settings = dict(DEFAULT_SETTINGS)
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
            for r in rows:
                try:
                    settings[r["key"]] = json.loads(r["value_json"])
                except Exception:
                    pass
        return settings

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        now = _now_str()
        with self.connect() as conn:
            for k, v in updates.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (k, json.dumps(v, ensure_ascii=False), now),
                )
        return self.get_settings()

    # ---------- Benchmark & ETF Daily ----------
    def upsert_benchmark_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        now = _now_str()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO benchmark_daily (trade_date, code, name, close, pct_chg, amount, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    name = COALESCE(excluded.name, benchmark_daily.name),
                    close = COALESCE(excluded.close, benchmark_daily.close),
                    pct_chg = COALESCE(excluded.pct_chg, benchmark_daily.pct_chg),
                    amount = COALESCE(excluded.amount, benchmark_daily.amount),
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        r["trade_date"],
                        r["code"],
                        r.get("name"),
                        r.get("close"),
                        r.get("pct_chg"),
                        r.get("amount"),
                        now,
                    )
                    for r in rows
                ],
            )
        self.fill_missing_benchmark_pct_chg()

    def fill_missing_benchmark_pct_chg(self) -> None:
        """为 pct_chg 为 NULL 的基准指数首日行按库内前一日收盘价自动补算涨跌幅."""
        with self.connect() as conn:
            codes = [r["code"] for r in conn.execute("SELECT DISTINCT code FROM benchmark_daily").fetchall()]
            updates: list[tuple[float, str, str]] = []
            for code in codes:
                series = conn.execute(
                    "SELECT trade_date, close, pct_chg FROM benchmark_daily WHERE code = ? ORDER BY trade_date ASC",
                    (code,),
                ).fetchall()
                prev_close: float | None = None
                for row in series:
                    c = row["close"]
                    if row["pct_chg"] is None and prev_close and prev_close > 0 and c is not None:
                        pct = round((float(c) - prev_close) / prev_close * 100.0, 3)
                        updates.append((pct, row["trade_date"], code))
                    if c is not None and float(c) > 0:
                        prev_close = float(c)
            if updates:
                conn.executemany(
                    "UPDATE benchmark_daily SET pct_chg = ? WHERE trade_date = ? AND code = ?",
                    updates,
                )

    def upsert_etf_daily_rows(self, rows: list[dict[str, Any]]) -> None:
        """
        原始行情/份额/尾盘字段补丁写入：仅覆盖非 None 的原始字段，避免行情写入抹掉已拉到的份额或尾盘数据。
        派生字段（share_delta/net_flow_yi/amount_ma20/amount_ratio/flow_status/tail_ma5）请统一走
        update_etf_derived_rows 或 publish_analysis_results，确保可显式写回 NULL。
        """
        if not rows:
            return
        now = _now_str()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO etf_daily (
                    trade_date, code, name, exchange, group_id,
                    open, high, low, close, pct_chg, volume, amount, amount_is_estimated,
                    nav, total_shares, share_delta, net_flow_yi, flow_status,
                    amount_ma20, amount_ratio, tail_amount, tail_share, tail_ma5, tail_ratio,
                    share_source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code) DO UPDATE SET
                    name = COALESCE(excluded.name, etf_daily.name),
                    exchange = COALESCE(excluded.exchange, etf_daily.exchange),
                    group_id = COALESCE(excluded.group_id, etf_daily.group_id),
                    open = COALESCE(excluded.open, etf_daily.open),
                    high = COALESCE(excluded.high, etf_daily.high),
                    low = COALESCE(excluded.low, etf_daily.low),
                    close = COALESCE(excluded.close, etf_daily.close),
                    pct_chg = COALESCE(excluded.pct_chg, etf_daily.pct_chg),
                    volume = COALESCE(excluded.volume, etf_daily.volume),
                    amount = COALESCE(excluded.amount, etf_daily.amount),
                    amount_is_estimated = COALESCE(excluded.amount_is_estimated, etf_daily.amount_is_estimated),
                    nav = COALESCE(excluded.nav, etf_daily.nav),
                    total_shares = COALESCE(excluded.total_shares, etf_daily.total_shares),
                    tail_amount = COALESCE(excluded.tail_amount, etf_daily.tail_amount),
                    tail_share = COALESCE(excluded.tail_share, etf_daily.tail_share),
                    tail_ratio = COALESCE(excluded.tail_ratio, etf_daily.tail_ratio),
                    share_source = COALESCE(excluded.share_source, etf_daily.share_source),
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        r["trade_date"],
                        r["code"],
                        r.get("name"),
                        r.get("exchange"),
                        r.get("group_id"),
                        r.get("open"),
                        r.get("high"),
                        r.get("low"),
                        r.get("close"),
                        r.get("pct_chg"),
                        r.get("volume"),
                        r.get("amount"),
                        (int(bool(r["amount_is_estimated"])) if "amount_is_estimated" in r and r["amount_is_estimated"] is not None else None),
                        r.get("nav"),
                        r.get("total_shares"),
                        r.get("share_delta"),
                        r.get("net_flow_yi"),
                        r.get("flow_status", ""),
                        r.get("amount_ma20"),
                        r.get("amount_ratio"),
                        r.get("tail_amount"),
                        r.get("tail_share"),
                        r.get("tail_ma5"),
                        r.get("tail_ratio"),
                        r.get("share_source"),
                        now,
                    )
                    for r in rows
                ],
            )

    def _apply_derived_updates(self, conn: sqlite3.Connection, rows: list[dict[str, Any]], now: str) -> None:
        """显式写回派生字段（允许 None 覆写为 NULL），绝不用 COALESCE 保留过期脏值."""
        if not rows:
            return
        conn.executemany(
            """
            UPDATE etf_daily SET
                pct_chg = COALESCE(?, pct_chg),
                amount_ma20 = ?,
                amount_ratio = ?,
                share_delta = ?,
                net_flow_yi = ?,
                flow_status = ?,
                tail_ma5 = ?,
                tail_ratio = ?,
                updated_at = ?
            WHERE trade_date = ? AND code = ?
            """,
            [
                (
                    r.get("pct_chg"),
                    r.get("amount_ma20"),
                    r.get("amount_ratio"),
                    r.get("share_delta"),
                    r.get("net_flow_yi"),
                    r.get("flow_status") or "",
                    r.get("tail_ma5"),
                    r.get("tail_ratio"),
                    now,
                    r["trade_date"],
                    r["code"],
                )
                for r in rows
            ],
        )

    def update_etf_derived_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        now = _now_str()
        with self.connect(immediate=True) as conn:
            self._apply_derived_updates(conn, rows, now)

    def _upsert_daily_log_in_conn(self, conn: sqlite3.Connection, log: dict[str, Any], now: str) -> None:
        # 优先从独立 user_notes 表读取个人批注与标签，确保永不因重算或清理丢失
        note_row = conn.execute(
            "SELECT user_note, user_tags FROM user_notes WHERE trade_date = ?",
            (log["trade_date"],),
        ).fetchone()
        preserved_note = note_row["user_note"] if note_row else ""
        preserved_tags = note_row["user_tags"] if note_row else ""

        conn.execute(
            """
            INSERT INTO daily_logs (
                trade_date, data_stage, sh_shares_ready, sz_shares_ready,
                sh_index_close, sh_index_pct, hs300_close, hs300_pct, zz1000_pct, cyb_pct,
                total_net_flow_yi, gross_inflow_yi, gross_outflow_yi,
                nt_est_low_yi, nt_est_mid_yi, nt_est_high_yi, nt_confidence,
                verdict_code, verdict_label, rotation_desc,
                lamps_json, group_flows_json, top_movers_json, scale_json, meta_json,
                auto_summary, user_note, user_tags, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                data_stage = excluded.data_stage,
                sh_shares_ready = excluded.sh_shares_ready,
                sz_shares_ready = excluded.sz_shares_ready,
                sh_index_close = excluded.sh_index_close,
                sh_index_pct = excluded.sh_index_pct,
                hs300_close = excluded.hs300_close,
                hs300_pct = excluded.hs300_pct,
                zz1000_pct = excluded.zz1000_pct,
                cyb_pct = excluded.cyb_pct,
                total_net_flow_yi = excluded.total_net_flow_yi,
                gross_inflow_yi = excluded.gross_inflow_yi,
                gross_outflow_yi = excluded.gross_outflow_yi,
                nt_est_low_yi = excluded.nt_est_low_yi,
                nt_est_mid_yi = excluded.nt_est_mid_yi,
                nt_est_high_yi = excluded.nt_est_high_yi,
                nt_confidence = excluded.nt_confidence,
                verdict_code = excluded.verdict_code,
                verdict_label = excluded.verdict_label,
                rotation_desc = excluded.rotation_desc,
                lamps_json = excluded.lamps_json,
                group_flows_json = excluded.group_flows_json,
                top_movers_json = excluded.top_movers_json,
                scale_json = excluded.scale_json,
                meta_json = excluded.meta_json,
                auto_summary = excluded.auto_summary,
                user_note = CASE
                    WHEN TRIM(COALESCE(daily_logs.user_note, '')) != '' THEN daily_logs.user_note
                    ELSE excluded.user_note
                END,
                user_tags = CASE
                    WHEN TRIM(COALESCE(daily_logs.user_tags, '')) != '' THEN daily_logs.user_tags
                    ELSE excluded.user_tags
                END,
                updated_at = excluded.updated_at
            """,
            (
                log["trade_date"],
                log["data_stage"],
                int(bool(log.get("sh_shares_ready"))),
                int(bool(log.get("sz_shares_ready"))),
                log.get("sh_index_close"),
                log.get("sh_index_pct"),
                log.get("hs300_close"),
                log.get("hs300_pct"),
                log.get("zz1000_pct"),
                log.get("cyb_pct"),
                log.get("total_net_flow_yi"),
                log.get("gross_inflow_yi"),
                log.get("gross_outflow_yi"),
                log.get("nt_est_low_yi"),
                log.get("nt_est_mid_yi"),
                log.get("nt_est_high_yi"),
                log.get("nt_confidence"),
                log.get("verdict_code"),
                log.get("verdict_label"),
                log.get("rotation_desc"),
                json.dumps(log.get("lamps", []), ensure_ascii=False),
                json.dumps(log.get("group_flows", []), ensure_ascii=False),
                json.dumps(log.get("top_movers", []), ensure_ascii=False),
                json.dumps(log.get("scale", {}), ensure_ascii=False),
                json.dumps(log.get("meta", {}), ensure_ascii=False),
                log.get("auto_summary", ""),
                preserved_note,
                preserved_tags,
                now,
            ),
        )

    def publish_analysis_results(
        self,
        derived_rows: list[dict[str, Any]],
        daily_logs: list[dict[str, Any]],
        min_log_date: str | None = None,
    ) -> int:
        """
        在单次原子短事务中统一发布派生资金流与每日日志，杜绝读到“新明细+旧日志”撕裂状态；
        清理窗口外旧日志时，同时保护含个人备注或含个人标签的记录（并保留独立 user_notes 表）。
        """
        now = _now_str()
        with self.connect(immediate=True) as conn:
            self._apply_derived_updates(conn, derived_rows, now)
            if min_log_date:
                # 先备份任何尚未进入 user_notes 的记录
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_notes (trade_date, user_note, user_tags, updated_at)
                    SELECT trade_date, COALESCE(user_note, ''), COALESCE(user_tags, ''), ?
                    FROM daily_logs
                    WHERE TRIM(COALESCE(user_note, '')) != '' OR TRIM(COALESCE(user_tags, '')) != ''
                    """,
                    (now,),
                )
                conn.execute(
                    """
                    DELETE FROM daily_logs
                    WHERE trade_date < ?
                      AND TRIM(COALESCE(user_note, '')) = ''
                      AND TRIM(COALESCE(user_tags, '')) = ''
                      AND trade_date NOT IN (
                          SELECT trade_date FROM user_notes
                          WHERE TRIM(COALESCE(user_note, '')) != '' OR TRIM(COALESCE(user_tags, '')) != ''
                      )
                    """,
                    (min_log_date,),
                )
            for log_obj in daily_logs:
                self._upsert_daily_log_in_conn(conn, log_obj, now)
        return len(daily_logs)

    def get_all_etf_daily(self, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM etf_daily WHERE 1=1"
        params: list[Any] = []
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        query += " ORDER BY trade_date ASC, code ASC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def get_etf_daily_by_date(self, trade_date: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM etf_daily WHERE trade_date = ? ORDER BY COALESCE(ABS(net_flow_yi), 0) DESC, COALESCE(amount, 0) DESC",
                (trade_date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_benchmarks_by_date(self, trade_date: str) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM benchmark_daily WHERE trade_date = ?", (trade_date,)
            ).fetchall()
            return {r["code"]: dict(r) for r in rows}

    def get_all_benchmarks(self, code: str = "000300", limit: int | None = 300) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT * FROM benchmark_daily WHERE code = ? ORDER BY trade_date ASC",
                    (code,),
                ).fetchall()
                return [dict(r) for r in rows]
            rows = conn.execute(
                "SELECT * FROM benchmark_daily WHERE code = ? ORDER BY trade_date DESC LIMIT ?",
                (code, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_distinct_trade_dates(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM etf_daily ORDER BY trade_date ASC"
            ).fetchall()
            return [r["trade_date"] for r in rows]

    # ---------- Daily Logs ----------
    def upsert_daily_log(self, log: dict[str, Any]) -> None:
        now = _now_str()
        with self.connect(immediate=True) as conn:
            self._upsert_daily_log_in_conn(conn, log, now)

    @staticmethod
    def _inflate_log(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for field, target, default in [
            ("lamps_json", "lamps", []),
            ("group_flows_json", "group_flows", []),
            ("top_movers_json", "top_movers", []),
            ("scale_json", "scale", {}),
            ("meta_json", "meta", {}),
        ]:
            raw = d.pop(field, None)
            try:
                d[target] = json.loads(raw) if raw else default
            except Exception:
                d[target] = default
        d["sh_shares_ready"] = bool(d.get("sh_shares_ready"))
        d["sz_shares_ready"] = bool(d.get("sz_shares_ready"))
        raw_tags = d.get("user_tags") or ""
        d["user_tags_list"] = [t.strip() for t in raw_tags.split(",") if t.strip()]
        return d

    def get_daily_logs(
        self,
        limit: int = 300,
        offset: int = 0,
        verdict_filter: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        keyword: str | None = None,
        only_with_note: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM daily_logs WHERE 1=1"
        params: list[Any] = []
        if verdict_filter and verdict_filter != "ALL":
            if verdict_filter == "BUY":
                query += " AND verdict_code IN ('STRONG_BUY', 'MODERATE_BUY', 'PRELIM_RESCUE')"
            elif verdict_filter == "SELL":
                query += " AND verdict_code IN ('STRONG_SELL', 'MODERATE_SELL', 'PRELIM_SELL')"
            elif verdict_filter == "ANOMALY":
                query += " AND verdict_code != 'QUIET'"
            elif verdict_filter == "ROTATION":
                query += " AND (verdict_code = 'ROTATION' OR TRIM(COALESCE(rotation_desc, '')) != '')"
            else:
                query += " AND verdict_code = ?"
                params.append(verdict_filter)
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        if keyword:
            like = f"%{keyword}%"
            query += " AND (auto_summary LIKE ? OR user_note LIKE ? OR user_tags LIKE ? OR verdict_label LIKE ? OR rotation_desc LIKE ? OR trade_date LIKE ?)"
            params.extend([like, like, like, like, like, like])
        if only_with_note:
            query += " AND (TRIM(COALESCE(user_note, '')) != '' OR TRIM(COALESCE(user_tags, '')) != '')"
        query += " ORDER BY trade_date DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._inflate_log(r) for r in rows]

    def get_daily_log(self, trade_date: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_logs WHERE trade_date = ?", (trade_date,)
            ).fetchone()
            return self._inflate_log(row) if row else None

    def get_latest_daily_log(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_logs ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            return self._inflate_log(row) if row else None

    def update_user_note(self, trade_date: str, user_note: str, user_tags: str) -> dict[str, Any] | None:
        now = _now_str()
        with self.connect(immediate=True) as conn:
            if user_note.strip() or user_tags.strip():
                conn.execute(
                    """
                    INSERT INTO user_notes (trade_date, user_note, user_tags, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(trade_date) DO UPDATE SET
                        user_note = excluded.user_note,
                        user_tags = excluded.user_tags,
                        updated_at = excluded.updated_at
                    """,
                    (trade_date, user_note, user_tags, now),
                )
            else:
                conn.execute("DELETE FROM user_notes WHERE trade_date = ?", (trade_date,))
            conn.execute(
                "UPDATE daily_logs SET user_note = ?, user_tags = ?, updated_at = ? WHERE trade_date = ?",
                (user_note, user_tags, now, trade_date),
            )
        return self.get_daily_log(trade_date)

    def get_log_stages(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT trade_date, data_stage FROM daily_logs").fetchall()
            return {r["trade_date"]: r["data_stage"] for r in rows}

    # ---------- Announcements ----------
    def upsert_announcements(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """返回本次新入库且尚未通知的公告列表；已存在记录可补全 event_category."""
        if not items:
            return []
        now = _now_str()
        newly_inserted: list[dict[str, Any]] = []
        with self.connect() as conn:
            for item in items:
                cat = item.get("event_category") or "主体股权事件"
                exists = conn.execute(
                    "SELECT 1 FROM announcements WHERE ann_id = ?", (item["ann_id"],)
                ).fetchone()
                if not exists:
                    conn.execute(
                        """
                        INSERT INTO announcements (
                            ann_id, publish_date, title, org_name, matched_keyword, event_category, url, notified, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                        """,
                        (
                            item["ann_id"],
                            item["publish_date"],
                            item["title"],
                            item.get("org_name", ""),
                            item.get("matched_keyword", ""),
                            cat,
                            item.get("url", ""),
                            now,
                        ),
                    )
                    newly_inserted.append({**item, "event_category": cat})
                else:
                    conn.execute(
                        "UPDATE announcements SET event_category = ? WHERE ann_id = ?",
                        (cat, item["ann_id"]),
                    )
        return newly_inserted

    def mark_announcements_notified(self, ann_ids: list[str]) -> None:
        if not ann_ids:
            return
        with self.connect() as conn:
            conn.executemany(
                "UPDATE announcements SET notified = 1 WHERE ann_id = ?",
                [(aid,) for aid in ann_ids],
            )

    def get_announcements(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM announcements ORDER BY publish_date DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Holdings ----------
    def upsert_holdings(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        now = _now_str()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO etf_holdings (
                    code, etf_name, report_date, published_at, holder_name, entity_label,
                    entity_category, hold_shares, hold_ratio, est_value_yi, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, report_date, holder_name) DO UPDATE SET
                    etf_name = excluded.etf_name,
                    published_at = COALESCE(NULLIF(excluded.published_at, ''), etf_holdings.published_at),
                    entity_label = excluded.entity_label,
                    entity_category = excluded.entity_category,
                    hold_shares = excluded.hold_shares,
                    hold_ratio = excluded.hold_ratio,
                    est_value_yi = excluded.est_value_yi,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        it["code"],
                        it.get("etf_name", ""),
                        it["report_date"],
                        it.get("published_at") or _statutory_published_at(it["report_date"]),
                        it["holder_name"],
                        it.get("entity_label", ""),
                        it.get("entity_category", ""),
                        it.get("hold_shares"),
                        it.get("hold_ratio"),
                        it.get("est_value_yi"),
                        now,
                    )
                    for it in items
                ],
            )

    def get_latest_holdings(self) -> list[dict[str, Any]]:
        """每个 ETF 取最新一个报告期的国家队/机构持仓明细."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.*
                FROM etf_holdings h
                INNER JOIN (
                    SELECT code, MAX(report_date) AS max_rd
                    FROM etf_holdings
                    GROUP BY code
                ) m ON h.code = m.code AND h.report_date = m.max_rd
                ORDER BY COALESCE(h.est_value_yi, 0) DESC, COALESCE(h.hold_ratio, 0) DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_holdings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM etf_holdings ORDER BY report_date ASC, code ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_holdings_timeline(self) -> dict[str, list[dict[str, Any]]]:
        """
        按 ETF 汇总每一份定期报告的实名国家队合计占比与中央汇金单独占比（不做人为 15% 地板修饰），
        并附带法定可用日 published_at 供历史时点一致性查询。
        """
        rows = self.get_all_holdings()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            key = (r["code"], r["report_date"])
            rec = grouped.get(key)
            if rec is None:
                pub = r.get("published_at") or _statutory_published_at(r["report_date"])
                rec = {
                    "code": r["code"],
                    "report_date": r["report_date"],
                    "published_at": pub,
                    "nt_ratio": 0.0,
                    "huijin_ratio": 0.0,
                    "nt_shares": 0.0,
                    "has_nt_match": False,
                    "updated_at": r.get("updated_at") or "",
                }
                grouped[key] = rec
            if r.get("entity_label") and r.get("hold_ratio") is not None:
                ratio = float(r["hold_ratio"]) / 100.0
                rec["nt_ratio"] += ratio
                rec["has_nt_match"] = True
                if r.get("hold_shares"):
                    rec["nt_shares"] += float(r["hold_shares"])
                cat = r.get("entity_category") or ""
                lbl = r.get("entity_label") or ""
                if cat in ("核心主力", "汇金通道") or "汇金" in lbl:
                    rec["huijin_ratio"] += ratio

        by_code: dict[str, list[dict[str, Any]]] = {}
        for rec in grouped.values():
            rec["nt_ratio"] = round(min(0.98, rec["nt_ratio"]), 4)
            rec["huijin_ratio"] = round(min(0.98, rec["huijin_ratio"]), 4)
            by_code.setdefault(rec["code"], []).append(rec)
        for lst in by_code.values():
            lst.sort(key=lambda x: x["report_date"])
        return by_code

    def get_nt_weight_overrides(self) -> dict[str, float]:
        """返回最新一期定期报告的原始实名国家队合计持股比例（0~0.98，不做 15% 地板失真修饰）."""
        rows = self.get_latest_holdings()
        sums: dict[str, float] = {}
        for r in rows:
            if r.get("entity_label") and r.get("hold_ratio") is not None:
                code = r["code"]
                sums[code] = sums.get(code, 0.0) + float(r["hold_ratio"]) / 100.0
        return {k: round(min(0.98, v), 4) for k, v in sums.items() if v > 0}

    # ---------- Notifications Outbox & History ----------
    def claim_notification_channel(
        self,
        dedup_key: str,
        channel: str,
        trade_date: str,
        stage: str,
        title: str,
        body: str,
        stale_seconds: int = 300,
    ) -> bool:
        """
        原子认领单通道发送权（跨进程+并发安全）：
        - 若该 (dedup_key, channel) 已 status='sent'，返回 False（绝不重复发）；
        - 若正在发送且未超过 stale_seconds，返回 False（防并发双发）；
        - 若尚未发送或上次 failed，认领并标记 status='sending'，允许重试！
        """
        now_dt = datetime.now()
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT status, attempts, claimed_at FROM notification_outbox WHERE dedup_key = ? AND channel = ?",
                (dedup_key, channel),
            ).fetchone()
            if row is not None:
                st = row["status"]
                if st == "sent":
                    return False
                if st == "sending":
                    c_dt = _parse_dt(row["claimed_at"])
                    if c_dt is not None and (now_dt - c_dt).total_seconds() < stale_seconds:
                        return False
                attempts = int(row["attempts"] or 0) + 1
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'sending', attempts = ?, claimed_at = ?, updated_at = ?,
                        trade_date = ?, stage = ?, title = ?, body = ?
                    WHERE dedup_key = ? AND channel = ?
                    """,
                    (attempts, now_s, now_s, trade_date, stage, title, body, dedup_key, channel),
                )
                return True

            conn.execute(
                """
                INSERT INTO notification_outbox (
                    dedup_key, channel, trade_date, stage, title, body,
                    status, attempts, last_error, claimed_at, sent_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'sending', 1, '', ?, NULL, ?)
                """,
                (dedup_key, channel, trade_date, stage, title, body, now_s, now_s),
            )
            return True

    def complete_notification_channel(
        self,
        dedup_key: str,
        channel: str,
        ok: bool,
        error: str = "",
        trade_date: str = "",
        stage: str = "",
        title: str = "",
        body: str = "",
    ) -> None:
        now_s = _now_str()
        with self.connect(immediate=True) as conn:
            if ok:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'sent', last_error = '', sent_at = ?, updated_at = ?
                    WHERE dedup_key = ? AND channel = ?
                    """,
                    (now_s, now_s, dedup_key, channel),
                )
                # 同步维护 notifications_sent 汇总（仅记录真正送达成功的通道）
                sent_rows = conn.execute(
                    "SELECT channel FROM notification_outbox WHERE dedup_key = ? AND status = 'sent' ORDER BY channel",
                    (dedup_key,),
                ).fetchall()
                ok_channels = [r["channel"] for r in sent_rows]
                conn.execute(
                    """
                    INSERT INTO notifications_sent (dedup_key, trade_date, stage, title, body, channels_json, sent_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedup_key) DO UPDATE SET
                        title = COALESCE(NULLIF(excluded.title, ''), notifications_sent.title),
                        body = COALESCE(NULLIF(excluded.body, ''), notifications_sent.body),
                        channels_json = excluded.channels_json,
                        sent_at = excluded.sent_at
                    """,
                    (
                        dedup_key,
                        trade_date,
                        stage,
                        title,
                        body,
                        json.dumps(ok_channels, ensure_ascii=False),
                        now_s,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'failed', last_error = ?, updated_at = ?
                    WHERE dedup_key = ? AND channel = ?
                    """,
                    ((error or "unknown error")[:300], now_s, dedup_key, channel),
                )

    def is_channel_sent(self, dedup_key: str, channel: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notification_outbox WHERE dedup_key = ? AND channel = ? AND status = 'sent'",
                (dedup_key, channel),
            ).fetchone()
            return row is not None

    def is_notified(self, dedup_key: str) -> bool:
        """兼容接口：仅当至少有一个通道真正成功送达时才视为已通知."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT channels_json FROM notifications_sent WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if not row:
                return False
            try:
                chs = json.loads(row["channels_json"] or "[]")
                return len(chs) > 0
            except Exception:
                return False

    def record_notification(
        self, dedup_key: str, trade_date: str, stage: str, title: str, body: str, channels: list[str]
    ) -> None:
        if not channels:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO notifications_sent (
                    dedup_key, trade_date, stage, title, body, channels_json, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (dedup_key, trade_date, stage, title, body, json.dumps(channels, ensure_ascii=False), _now_str()),
            )

    def get_recent_notifications(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications_sent ORDER BY sent_at DESC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["channels"] = json.loads(d.pop("channels_json") or "[]")
                except Exception:
                    d["channels"] = []
                # 附带失败/待重试通道状态
                failed_rows = conn.execute(
                    "SELECT channel, last_error, attempts FROM notification_outbox WHERE dedup_key = ? AND status = 'failed'",
                    (d["dedup_key"],),
                ).fetchall()
                d["failed_channels"] = [dict(fr) for fr in failed_rows]
                result.append(d)
            return result

    def start_sync_run(self, run_type: str, message: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_runs (run_type, started_at, status, message) VALUES (?, ?, 'running', ?)",
                (run_type, _now_str(), message),
            )
            return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, status: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = ?, status = ?, message = ? WHERE id = ?",
                (_now_str(), status, message, run_id),
            )

    def get_recent_sync_runs(self, limit: int = 15) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
