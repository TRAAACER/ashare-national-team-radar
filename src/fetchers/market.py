"""行情与尾盘数据采集：新浪日线 + 腾讯日线/5分钟线 + 基准指数."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from src.fetchers.http_client import request_with_fallback

logger = logging.getLogger(__name__)


def _tx_symbol(code: str, exchange: str) -> str:
    prefix = "sh" if exchange.upper() == "SH" else "sz"
    return f"{prefix}{code}"


def fetch_benchmark_daily(
    code: str, name: str, market: str, days: int = 320
) -> list[dict[str, Any]]:
    """抓取基准指数日线（优先新浪纯 JSON，失败自动降级腾讯纯 JSON；窗口首日无前收时 pct_chg 置 None 防止覆写库内真值）."""
    sym = _tx_symbol(code, market)
    try:
        resp = request_with_fallback(
            "GET",
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": sym, "scale": "240", "ma": "no", "datalen": str(days)},
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=12.0,
        )
        items = resp.json()
        rows: list[dict[str, Any]] = []
        prev_close: float | None = None
        if isinstance(items, list):
            for it in items:
                trade_date = str(it.get("day", ""))[:10]
                if len(trade_date) != 10:
                    continue
                close = float(it["close"])
                vol = float(it.get("volume", 0))
                pct_chg = round((close - prev_close) / prev_close * 100.0, 3) if (prev_close and prev_close > 0) else None
                prev_close = close
                rows.append(
                    {
                        "trade_date": trade_date,
                        "code": code,
                        "name": name,
                        "close": round(close, 4),
                        "pct_chg": pct_chg,
                        "amount": vol,
                    }
                )
        if rows:
            return rows
    except Exception as e:
        logger.warning("Sina JSON benchmark %s failed: %s; falling back to Tencent", code, e)

    resp = request_with_fallback(
        "GET",
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq",
        timeout=12.0,
    )
    payload = resp.json()
    node = (payload.get("data") or {}).get(sym) or {}
    klines = node.get("qfqday") or node.get("day") or []
    rows = []
    prev_close = None
    for item in klines:
        if len(item) < 6:
            continue
        trade_date = str(item[0])[:10]
        close = float(item[2])
        vol = float(item[5])
        pct_chg = round((close - prev_close) / prev_close * 100.0, 3) if (prev_close and prev_close > 0) else None
        prev_close = close
        rows.append(
            {
                "trade_date": trade_date,
                "code": code,
                "name": name,
                "close": round(close, 4),
                "pct_chg": pct_chg,
                "amount": vol,
            }
        )
    return rows


def _fetch_etf_daily_sina_json(etf: dict[str, Any], days: int = 320) -> list[dict[str, Any]]:
    """新浪纯 JSON 日线接口（不依赖 py_mini_racer/V8，多线程绝对安全；明确标记估算成交额与首日未知涨跌幅）."""
    sym = _tx_symbol(etf["code"], etf["exchange"])
    resp = request_with_fallback(
        "GET",
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": sym, "scale": "240", "ma": "no", "datalen": str(days)},
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=12.0,
    )
    items = resp.json()
    if not isinstance(items, list) or not items:
        return []
    rows: list[dict[str, Any]] = []
    prev_close: float | None = None
    for it in items:
        trade_date = str(it.get("day", ""))[:10]
        if len(trade_date) != 10:
            continue
        o = float(it["open"])
        h = float(it["high"])
        l = float(it["low"])
        c = float(it["close"])
        vol = float(it.get("volume", 0))
        raw_amt = it.get("amount")
        if raw_amt not in (None, "") and float(raw_amt) > 0:
            amt = float(raw_amt)
            is_est = 0
        else:
            amt = vol * ((o + h + l + c) / 4.0)
            is_est = 1
        pct_chg = round((c - prev_close) / prev_close * 100.0, 3) if (prev_close and prev_close > 0) else None
        prev_close = c
        rows.append(
            {
                "trade_date": trade_date,
                "code": etf["code"],
                "name": etf["name"],
                "exchange": etf["exchange"],
                "group_id": etf["group"],
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "pct_chg": pct_chg,
                "volume": vol,
                "amount": round(amt, 2),
                "amount_is_estimated": is_est,
            }
        )
    return rows


def _fetch_etf_daily_tencent(etf: dict[str, Any], days: int = 320) -> list[dict[str, Any]]:
    sym = _tx_symbol(etf["code"], etf["exchange"])
    resp = request_with_fallback(
        "GET",
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},",
        timeout=12.0,
    )
    payload = resp.json()
    node = (payload.get("data") or {}).get(sym) or {}
    klines = node.get("day") or node.get("qfqday") or []
    rows: list[dict[str, Any]] = []
    prev_close: float | None = None
    for item in klines:
        if len(item) < 6:
            continue
        trade_date = str(item[0])[:10]
        o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
        vol_lots = float(item[5])  # 腾讯 ETF 成交量单位为手(100份)
        vol_shares = vol_lots * 100.0
        avg_p = (o + c + h + l) / 4.0
        amount = vol_shares * avg_p
        pct_chg = round((c - prev_close) / prev_close * 100.0, 3) if (prev_close and prev_close > 0) else None
        prev_close = c
        rows.append(
            {
                "trade_date": trade_date,
                "code": etf["code"],
                "name": etf["name"],
                "exchange": etf["exchange"],
                "group_id": etf["group"],
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "pct_chg": pct_chg,
                "volume": vol_shares,
                "amount": round(amount, 2),
                "amount_is_estimated": 1,
            }
        )
    return rows


def fetch_etf_daily_one(etf: dict[str, Any], days: int = 320) -> list[dict[str, Any]]:
    """单只 ETF 日线抓取：优先新浪纯 JSON（线程安全），失败降级腾讯纯 JSON."""
    try:
        rows = _fetch_etf_daily_sina_json(etf, days=days)
        if rows:
            return rows
    except Exception as e:
        logger.warning("Sina JSON daily failed for %s: %s; falling back to Tencent", etf["code"], e)
    try:
        return _fetch_etf_daily_tencent(etf, days=days)
    except Exception as e:
        logger.error("Tencent daily also failed for %s: %s", etf["code"], e)
        return []


def fetch_all_etf_daily(
    etfs: list[dict[str, Any]],
    days: int = 320,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """并发抓取全部监控池 ETF 的历史日线."""
    all_rows: list[dict[str, Any]] = []
    total = len(etfs)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_etf_daily_one, etf, days): etf for etf in etfs}
        for fut in as_completed(futures):
            etf = futures[fut]
            try:
                rows = fut.result()
                all_rows.extend(rows)
            except Exception as e:
                logger.warning("ETF daily error %s: %s", etf["code"], e)
            done += 1
            if progress_cb:
                progress_cb(done, total, f"日线行情 {etf['code']} {etf['name']} ({done}/{total})")
    return all_rows


def _parse_tail_metrics_from_klines(code: str, klines: list[Any]) -> list[dict[str, Any]]:
    """
    从 5 分钟 K 线严格提取完整交易日的尾盘 30 分钟（14:30~15:00）特征。
    强约束：
      - bars >= 44（标准全天 48 根）
      - min_hhmm <= '0945'（排除 320 根窗口最早被截断的半日）
      - max_hhmm >= '1455'（排除仅开上午盘 09:35~11:30 的半日）
    """
    by_day: dict[str, dict[str, Any]] = {}
    for bar in klines:
        if len(bar) < 6:
            continue
        ts = str(bar[0])  # YYYYMMDDHHMM
        if len(ts) < 12:
            continue
        d_str = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        hhmm = ts[8:12]
        close = float(bar[2])
        vol_lots = float(bar[5])
        amt = vol_lots * 100.0 * close
        rec = by_day.setdefault(
            d_str,
            {"total": 0.0, "tail": 0.0, "bars": 0, "min_hhmm": hhmm, "max_hhmm": hhmm},
        )
        rec["total"] += amt
        rec["bars"] += 1
        if hhmm < rec["min_hhmm"]:
            rec["min_hhmm"] = hhmm
        if hhmm > rec["max_hhmm"]:
            rec["max_hhmm"] = hhmm
        # 14:35, 14:40, 14:45, 14:50, 14:55, 15:00 -> 最后 30 分钟
        if hhmm >= "1435":
            rec["tail"] += amt

    sorted_days = sorted(by_day.keys())
    results: list[dict[str, Any]] = []
    tail_history: list[float] = []
    for d in sorted_days:
        rec = by_day[d]
        if (
            rec["bars"] < 44
            or rec["min_hhmm"] > "0945"
            or rec["max_hhmm"] < "1455"
            or rec["total"] <= 0
        ):
            continue
        tail_amt = rec["tail"]
        tail_share = tail_amt / rec["total"]
        if tail_history:
            base = sum(tail_history[-5:]) / len(tail_history[-5:])
            tail_ratio = round(tail_amt / base, 3) if base > 0 else 1.0
        else:
            tail_ratio = None
        tail_history.append(tail_amt)
        results.append(
            {
                "trade_date": d,
                "code": code,
                "tail_amount": round(tail_amt, 2),
                "tail_share": round(tail_share, 4),
                "tail_ratio": tail_ratio,
            }
        )
    return results


def fetch_etf_tail_metrics_one(etf: dict[str, Any], bars: int = 320) -> list[dict[str, Any]]:
    """
    通过腾讯 5 分钟 K 线计算最近完整交易日的尾盘 30 分钟（14:30~15:00）成交特征。
    """
    sym = _tx_symbol(etf["code"], etf["exchange"])
    try:
        resp = request_with_fallback(
            "GET",
            f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m5,,{bars}",
            timeout=10.0,
        )
        payload = resp.json()
        klines = ((payload.get("data") or {}).get(sym) or {}).get("m5") or []
    except Exception as e:
        logger.warning("Tencent 5m failed for %s: %s", etf["code"], e)
        return []
    return _parse_tail_metrics_from_klines(etf["code"], klines)


def fetch_all_tail_metrics(
    etfs: list[dict[str, Any]],
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    total = len(etfs)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_etf_tail_metrics_one, etf): etf for etf in etfs}
        for fut in as_completed(futures):
            etf = futures[fut]
            try:
                all_rows.extend(fut.result())
            except Exception as e:
                logger.warning("Tail metrics error %s: %s", etf["code"], e)
            done += 1
            if progress_cb:
                progress_cb(done, total, f"尾盘5分钟脉冲 {etf['code']} ({done}/{total})")
    return all_rows
