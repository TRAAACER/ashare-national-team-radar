"""基金前十大实名持有人底牌（东财数据中心，多报告期时点一致）与国家队官宣公告（巨潮资讯网分类）采集."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable

from src.db import _statutory_published_at
from src.fetchers.http_client import request_with_fallback

logger = logging.getLogger(__name__)

# 严格过滤：标题必须包含核心主体词，且最好具备动作/投资相关语境，避免“中央商场”等无关误报
STRICT_ENTITIES = [
    ("中央汇金", ["中央汇金"]),
    ("国新投资", ["国新投资", "中国国新"]),
    ("中国诚通", ["中国诚通", "诚通金控"]),
    ("证金公司", ["中国证券金融股份有限公司", "证金公司"]),
]
RELEVANT_ACTION_HINTS = [
    "增持", "减持", "ETF", "交易型开放式指数", "权益变动", "持股", "要约",
    "平准", "市值管理", "回购", "入股", "股份变动", "无偿划转", "股东",
]
EXCLUDE_NOISE = ["债券", "付息", "兑付", "跟踪评级", "募集说明书", "售后回租", "信托", "租赁", "中票", "超短期融资券"]


def classify_announcement_category(title: str) -> str:
    """区分 ETF/公开增持动作 与 上市公司豁免要约/股权划转等主体事件，避免语义混淆."""
    t = title or ""
    if any(k in t for k in ("ETF", "交易型开放式指数", "基金份额", "宽基")):
        return "ETF/基金增持"
    if any(k in t for k in ("豁免", "要约", "法律意见书", "无偿划转", "详式权益变动", "简式权益变动")):
        return "主体股权/要约事件"
    if "增持" in t or "平准" in t:
        return "公开增持/表态"
    return "主体股权/权益动态"


def match_national_team_entity(
    holder_name: str, entities_cfg: list[dict[str, Any]]
) -> tuple[str, str]:
    """返回 (entity_label, entity_category)，未匹配返回 ('', '')."""
    if not holder_name:
        return "", ""
    for ent in entities_cfg:
        if ent["pattern"] in holder_name:
            return ent["label"], ent["category"]
    return "", ""


def fetch_etf_top_holders_one(
    etf: dict[str, Any],
    entities_cfg: list[dict[str, Any]],
    latest_close: float | None = None,
    max_periods: int = 3,
) -> list[dict[str, Any]]:
    """
    通过东财数据中心 RPT_F10_FUND_HOLDERTOP10 抓取单只 ETF 最近多期（默认近 3 个报告期）前十大实名持有人，
    同时写入法定可用披露日 published_at，以支持历史分析零前视偏差（Point-in-Time）。
    """
    code = etf["code"]
    try:
        resp = request_with_fallback(
            "GET",
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_F10_FUND_HOLDERTOP10",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")',
                "sortColumns": "REPORT_DATE,HOLDER_RANK",
                "sortTypes": "-1,1",
                "pageSize": "35",
            },
            timeout=10.0,
        )
        payload = resp.json()
        data = (payload.get("result") or {}).get("data") or []
    except Exception as e:
        logger.warning("Top10 holders fetch failed for %s: %s", code, e)
        return []

    if not data:
        return []

    distinct_rds: list[str] = []
    for item in data:
        rd = str(item.get("REPORT_DATE", ""))[:10]
        if len(rd) == 10 and rd not in distinct_rds:
            distinct_rds.append(rd)
            if len(distinct_rds) >= max_periods:
                break
    allowed_rds = set(distinct_rds)
    latest_rd = distinct_rds[0] if distinct_rds else ""

    rows: list[dict[str, Any]] = []
    for item in data:
        rd = str(item.get("REPORT_DATE", ""))[:10]
        if rd not in allowed_rds:
            continue
        holder_name = str(item.get("ORG_NAME") or "").strip()
        if not holder_name:
            continue
        entity_label, entity_category = match_national_team_entity(holder_name, entities_cfg)
        hold_share = item.get("HOLD_SHARE")
        hold_ratio = item.get("HOLD_SHARE_RATIO")
        shares_val = float(hold_share) if hold_share is not None else None
        ratio_val = float(hold_ratio) if hold_ratio is not None else None
        est_value_yi = (
            round(shares_val * latest_close / 1e8, 2)
            if (rd == latest_rd and shares_val and latest_close and latest_close > 0)
            else None
        )
        rows.append(
            {
                "code": code,
                "etf_name": etf["name"],
                "report_date": rd,
                "published_at": _statutory_published_at(rd),
                "holder_name": holder_name,
                "entity_label": entity_label,
                "entity_category": entity_category,
                "hold_shares": shares_val,
                "hold_ratio": round(ratio_val, 2) if ratio_val is not None else None,
                "est_value_yi": est_value_yi,
            }
        )
    return rows


def fetch_all_etf_top_holders(
    etfs: list[dict[str, Any]],
    entities_cfg: list[dict[str, Any]],
    latest_closes: dict[str, float],
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    total = len(etfs)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(
                fetch_etf_top_holders_one, etf, entities_cfg, latest_closes.get(etf["code"])
            ): etf
            for etf in etfs
        }
        for fut in as_completed(futures):
            etf = futures[fut]
            try:
                all_rows.extend(fut.result())
            except Exception as e:
                logger.warning("Holders error %s: %s", etf["code"], e)
            done += 1
            if progress_cb:
                progress_cb(done, total, f"定期报告前十大持有人 {etf['code']} ({done}/{total})")
    return all_rows


def _clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_cninfo_announcements(lookback_days: int = 365) -> list[dict[str, Any]]:
    """
    从巨潮资讯网检索中央汇金、国新投资、中国诚通、证金等与增持/股权/ETF相关的正式公告，
    严格关键词清洗并区分 ETF 增持与主体股权/要约事件。
    """
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=lookback_days)
    sdate = start_dt.strftime("%Y-%m-%d")
    edate = end_dt.strftime("%Y-%m-%d")

    seen: dict[str, dict[str, Any]] = {}
    queries = [
        ("中央汇金", "中央汇金 增持"),
        ("中央汇金", "中央汇金"),
        ("国新投资", "国新投资 增持"),
        ("中国诚通", "中国诚通 增持"),
        ("诚通金控", "诚通金控"),
    ]
    for tag, q in queries:
        try:
            resp = request_with_fallback(
                "GET",
                "http://www.cninfo.com.cn/new/fulltextSearch/full",
                params={
                    "searchkey": q,
                    "sdate": sdate,
                    "edate": edate,
                    "isfulltext": "false",
                    "sortName": "pubdate",
                    "sortType": "desc",
                    "pageNum": 1,
                    "pageSize": 25,
                },
                headers={"Referer": "http://www.cninfo.com.cn/"},
                timeout=12.0,
            )
            payload = resp.json()
            anns = payload.get("announcements") or []
        except Exception as e:
            logger.warning("CNINFO search failed for %s: %s", q, e)
            continue

        for a in anns:
            ann_id = str(a.get("announcementId") or "").strip()
            if not ann_id or ann_id in seen:
                continue
            title = _clean_html(a.get("announcementTitle", ""))
            sec_name = _clean_html(a.get("secName", ""))
            if not title:
                continue
            if any(noise in title for noise in EXCLUDE_NOISE):
                continue
            matched_ent = ""
            for ent_label, patterns in STRICT_ENTITIES:
                if any(p in title for p in patterns):
                    matched_ent = ent_label
                    break
            if not matched_ent:
                continue
            if matched_ent != "中央汇金" and not any(h in title for h in RELEVANT_ACTION_HINTS):
                continue

            ts_ms = a.get("announcementTime")
            if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                pub_date = datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d")
            else:
                pub_date = edate

            adj_url = str(a.get("adjunctUrl") or "").lstrip("/")
            full_url = f"http://static.cninfo.com.cn/{adj_url}" if adj_url else ""
            seen[ann_id] = {
                "ann_id": ann_id,
                "publish_date": pub_date,
                "title": title,
                "org_name": sec_name or matched_ent,
                "matched_keyword": matched_ent or tag,
                "event_category": classify_announcement_category(title),
                "url": full_url,
            }

    results = sorted(seen.values(), key=lambda x: x["publish_date"], reverse=True)
    return results
