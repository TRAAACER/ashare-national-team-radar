"""HTTP 会话辅助：针对国内金融数据源提供直连优先 + 系统代理降级 + 指数退避重试."""

from __future__ import annotations

import time
from typing import Any
import requests

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def make_session(trust_env: bool = False) -> requests.Session:
    s = requests.Session()
    s.trust_env = trust_env
    s.headers.update(DEFAULT_HEADERS)
    return s


def request_with_fallback(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
    retries: int = 2,
) -> requests.Response:
    """
    先直连（绕过可能不稳定的本地代理）尝试；若全部失败，再降级走系统代理。
    """
    last_err: Exception | None = None
    for trust_env in (False, True):
        session = make_session(trust_env=trust_env)
        for attempt in range(retries):
            try:
                resp = session.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                )
                resp.raise_for_status()
                return resp
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(0.4 * (attempt + 1))
    assert last_err is not None
    raise last_err
