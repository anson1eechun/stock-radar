import threading
import time
from urllib.parse import urlparse

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) stock-radar/0.1"
# 證交所網站對短時間大量請求會封鎖 IP
MIN_INTERVAL = {"www.twse.com.tw": 3.0, "www.tpex.org.tw": 2.0, "api.bls.gov": 1.0}

_session = requests.Session()
_session.headers["User-Agent"] = UA
_last_call: dict[str, float] = {}
_lock = threading.Lock()


def _throttle(host: str) -> None:
    gap = MIN_INTERVAL.get(host, 0.3)
    with _lock:
        wait = _last_call.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


RETRY_STATUS = {429, 500, 502, 503, 504}


def request(method: str, url: str, retries: int = 3, timeout: int = 30, **kw) -> requests.Response:
    host = urlparse(url).netloc
    for attempt in range(retries):
        last = attempt == retries - 1
        _throttle(host)
        try:
            r = _session.request(method, url, timeout=timeout, **kw)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in RETRY_STATUS and not last:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def get_json(url: str, **kw):
    return request("GET", url, **kw).json()


def get_text(url: str, **kw) -> str:
    r = request("GET", url, **kw)
    r.encoding = r.encoding or "utf-8"
    return r.text
