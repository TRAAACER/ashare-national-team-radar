"""官方交易所 ETF 每日总份额采集（上交所直连 + 深交所官方日频接口，严格日期校验）."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable

import akshare as ak
import pandas as pd

from src.fetchers.http_client import request_with_fallback

logger = logging.getLogger(__name__)


def fetch_sse_shares_for_date(trade_date: str, target_codes: set[str]) -> list[dict[str, Any]]:
    """
    抓取上交所指定日期 ETF 份额。
    SSE 官方接口返回 TOT_VOL 单位为【万份】，这里统一换算为【份】。
    """
    resp = request_with_fallback(
        "GET",
        "https://query.sse.com.cn/commonQuery.do",
        params={
            "isPagination": "true",
            "pageHelp.pageSize": "3000",
            "pageHelp.pageNo": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": trade_date,
        },
        headers={"Referer": "https://www.sse.com.cn/"},
        timeout=12.0,
    )
    data = resp.json()
    items = data.get("result") or []
    rows: list[dict[str, Any]] = []
    for it in items:
        code = str(it.get("SEC_CODE", "")).strip()
        if code not in target_codes:
            continue
        raw_vol = it.get("TOT_VOL")
        if raw_vol in (None, "", "-"):
            continue
        try:
            shares = float(str(raw_vol).replace(",", "")) * 10000.0
        except ValueError:
            continue
        if shares <= 0:
            continue
        stat_date = str(it.get("STAT_DATE") or "").strip()[:10]
        if len(stat_date) != 10:
            continue
        rows.append(
            {
                "trade_date": stat_date,
                "code": code,
                "total_shares": shares,
                "share_source": "sse",
            }
        )
    return rows


def fetch_sse_shares_for_dates(
    trade_dates: list[str],
    target_codes: set[str],
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """并发抓取多个交易日的上交所 ETF 份额."""
    if not trade_dates or not target_codes:
        return []
    all_rows: list[dict[str, Any]] = []
    total = len(trade_dates)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_sse_shares_for_date, d, target_codes): d
            for d in trade_dates
        }
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                all_rows.extend(fut.result())
            except Exception as e:
                logger.warning("SSE shares failed for %s: %s", d, e)
            done += 1
            if progress_cb and (done % 5 == 0 or done == total):
                progress_cb(done, total, f"上交所历史份额 {d} ({done}/{total})")
    return all_rows


def fetch_szse_shares_range(
    start_date: str,
    end_date: str,
    target_codes: set[str],
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """
    通过深交所官方日频接口抓取区间内全部深市 ETF 份额。
    自动按 <= 45 天分段拉取（规避深交所导出行数截断上限），单位为【份】。
    """
    if not target_codes:
        return []
    dt_start = datetime.strptime(start_date, "%Y-%m-%d")
    dt_end = datetime.strptime(end_date, "%Y-%m-%d")
    if dt_start > dt_end:
        return []

    chunks: list[tuple[str, str]] = []
    cur = dt_start
    while cur <= dt_end:
        nxt = min(dt_end, cur + timedelta(days=45))
        chunks.append((cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")))
        cur = nxt + timedelta(days=1)

    all_rows: list[dict[str, Any]] = []
    for idx, (s_str, e_str) in enumerate(chunks, start=1):
        try:
            df = ak.fund_scale_daily_szse(start_date=s_str, end_date=e_str, symbol="ETF")
            if df is not None and not df.empty:
                df["基金代码"] = df["基金代码"].astype(str).str.strip()
                sub = df[df["基金代码"].isin(target_codes)]
                for _, r in sub.iterrows():
                    raw_val = r.get("基金份额")
                    if pd.isna(raw_val):
                        continue
                    shares = float(str(raw_val).replace(",", ""))
                    if shares <= 0:
                        continue
                    d_norm = pd.to_datetime(r["日期"]).strftime("%Y-%m-%d")
                    all_rows.append(
                        {
                            "trade_date": d_norm,
                            "code": r["基金代码"],
                            "total_shares": shares,
                            "share_source": "szse",
                        }
                    )
        except Exception as e:
            logger.warning("SZSE range %s~%s failed: %s", s_str, e_str, e)
        if progress_cb:
            progress_cb(idx, len(chunks), f"深交所历史份额 {s_str}~{e_str} ({idx}/{len(chunks)})")
    return all_rows


def fetch_szse_latest_snapshot(target_codes: set[str], trade_date: str) -> list[dict[str, Any]]:
    """
    深交所快照安全守卫：仅当返回表中自带明确有效日期列且与目标 trade_date 一致时才采纳。
    绝不把没有日期证明的旧快照伪造成目标日写入正式份额表，彻底杜绝日期污染。
    """
    try:
        df = ak.fund_etf_scale_szse()
        if df is None or df.empty:
            return []
        date_col = next((c for c in ("日期", "统计日期", "更新日期", "交易日期") if c in df.columns), None)
        if date_col is None:
            logger.info("SZSE snapshot has no effective date column; skipping to prevent date pollution.")
            return []
        df["基金代码"] = df["基金代码"].astype(str).str.strip()
        sub = df[df["基金代码"].isin(target_codes)]
        rows: list[dict[str, Any]] = []
        for _, r in sub.iterrows():
            raw_d = r.get(date_col)
            if pd.isna(raw_d):
                continue
            eff_date = pd.to_datetime(raw_d).strftime("%Y-%m-%d")
            if eff_date != trade_date:
                continue
            raw = r.get("基金份额")
            if pd.isna(raw):
                continue
            shares = float(str(raw).replace(",", ""))
            if shares <= 0:
                continue
            rows.append(
                {
                    "trade_date": eff_date,
                    "code": r["基金代码"],
                    "total_shares": shares,
                    "share_source": "szse",
                }
            )
        return rows
    except Exception as e:
        logger.warning("SZSE snapshot fallback failed: %s", e)
        return []
