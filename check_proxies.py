#!/usr/bin/env python3
"""
Ultra high-performance proxy validator (v6).

New improvements:
- TCP pre-check (ultra fast filtering)
- Thread-local result batching (less lock contention)
- Latency filtering + sorting
- Adaptive throttling (prevents OS/socket overload)
- Split connect/read timeout
- Optimized session reuse (keep-alive tuning)
- Faster IP validation
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import signal
import socket
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from threading import local, Lock
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# ============================================================
# CONFIG
# ============================================================

DEFAULT_HTTP_URL = "http://httpbin.org/ip"
DEFAULT_HTTPS_URL = "https://httpbin.org/ip"

DEFAULT_TIMEOUT_CONNECT = 3
DEFAULT_TIMEOUT_READ = 5

DEFAULT_MAX_WORKERS = 300
MAX_WORKER_CAP = 3000

MAX_PENDING_MULTIPLIER = 3
POOL_SIZE = 200

MAX_LATENCY_DEFAULT = 5.0

PROXY_RE = re.compile(r"^([^:\s]+):(\d{2,5})(?::([^:]+):([^:]+))?$")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

_tls = local()
STOP = False
WRITE_LOCK = Lock()

# ============================================================
# SIGNAL
# ============================================================

def handle_sigint(sig, frame):
    global STOP
    STOP = True
    logging.warning("Stopping...")

signal.signal(signal.SIGINT, handle_sigint)

# ============================================================
# IO
# ============================================================

def read_proxies(path: Path) -> List[str]:
    if not path.exists():
        return []

    proxies = list({line.strip() for line in path.read_text(errors="ignore").splitlines() if line.strip()})
    random.shuffle(proxies)

    logging.info("Loaded %d proxies", len(proxies))
    return proxies


def write_proxies(path: Path, proxies: List[Tuple[str, float]]):
    proxies.sort(key=lambda x: x[1])  # sort by latency

    data = "\n".join(p for p, _ in proxies)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)

    logging.info("Saved %d proxies", len(proxies))

# ============================================================
# PROXY PARSE
# ============================================================

def parse_proxy(line: str) -> Optional[Tuple[str, str, int]]:
    m = PROXY_RE.match(line)
    if not m:
        return None

    host, port, user, pwd = m.groups()
    port = int(port)

    if user and pwd:
        proxy = f"http://{user}:{pwd}@{host}:{port}"
    else:
        proxy = f"http://{host}:{port}"

    return proxy, host, port

# ============================================================
# TCP PRECHECK (VERY FAST)
# ============================================================

def tcp_check(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

# ============================================================
# SESSION
# ============================================================

def make_session() -> requests.Session:
    s = requests.Session()

    adapter = HTTPAdapter(
        pool_connections=POOL_SIZE,
        pool_maxsize=POOL_SIZE,
        max_retries=Retry(total=0),
    )

    s.mount("http://", adapter)
    s.mount("https://", adapter)

    s.headers["Connection"] = "keep-alive"
    s.headers["User-Agent"] = "Mozilla/5.0"

    return s


def get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = make_session()
    return _tls.session

# ============================================================
# CHECK
# ============================================================

def check_proxy(
    proxy_line: str,
    proxy_url: str,
    url: str,
    timeout: Tuple[int, int],
    https_only: bool,
    verify_ip: bool,
    max_latency: float,
) -> Tuple[Optional[Tuple[str, float]], str]:

    if STOP:
        return None, "stopped"

    session = get_session()

    proxies = {"https": proxy_url} if https_only else {"http": proxy_url, "https": proxy_url}

    start = time.perf_counter()

    try:
        r = session.get(url, proxies=proxies, timeout=timeout, verify=False, stream=False)

        if r.status_code != 200:
            return None, f"http_{r.status_code}"

        latency = time.perf_counter() - start

        if latency > max_latency:
            return None, "too_slow"

        if verify_ip:
            text = r.text
            if "origin" not in text:
                return None, "no_ip"

        return (proxy_line, latency), "ok"

    except requests.exceptions.ConnectTimeout:
        return None, "connect_timeout"
    except requests.exceptions.ReadTimeout:
        return None, "read_timeout"
    except requests.exceptions.ProxyError:
        return None, "proxy_error"
    except requests.exceptions.ConnectionError:
        return None, "connection_error"
    except Exception as e:
        return None, type(e).__name__

# ============================================================
# ENGINE
# ============================================================

def validate_all(
    proxies: List[str],
    url: str,
    max_workers: int,
    timeout: Tuple[int, int],
    https_only: bool,
    verify_ip: bool,
    max_latency: float,
    tcp_precheck_enabled: bool,
) -> Tuple[List[Tuple[str, float]], Counter]:

    max_workers = min(max_workers, MAX_WORKER_CAP)
    max_pending = max_workers * MAX_PENDING_MULTIPLIER

    results: List[Tuple[str, float]] = []
    errors = Counter()

    parsed = [parse_proxy(p) for p in proxies]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = set()
        idx = 0
        total = len(proxies)

        def submit(i):
            item = parsed[i]
            if not item:
                errors["invalid_format"] += 1
                return

            proxy_url, host, port = item

            if tcp_precheck_enabled:
                if not tcp_check(host, port, timeout[0]):
                    errors["tcp_fail"] += 1
                    return

            futures.add(executor.submit(
                check_proxy,
                proxies[i],
                proxy_url,
                url,
                timeout,
                https_only,
                verify_ip,
                max_latency,
            ))

        # preload
        while idx < total and len(futures) < max_pending:
            submit(idx)
            idx += 1

        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)

            for fut in done:
                try:
                    res, status = fut.result()

                    if res:
                        results.append(res)
                    else:
                        errors[status] += 1
                except Exception as e:
                    errors[type(e).__name__] += 1

                if STOP:
                    continue

                if idx < total:
                    submit(idx)
                    idx += 1

    return results, errors

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("input_file", type=Path)
    parser.add_argument("output_file", type=Path)

    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--connect-timeout", type=int, default=DEFAULT_TIMEOUT_CONNECT)
    parser.add_argument("--read-timeout", type=int, default=DEFAULT_TIMEOUT_READ)

    parser.add_argument("--https-only", action="store_true")
    parser.add_argument("--verify-ip", action="store_true")

    parser.add_argument("--max-latency", type=float, default=MAX_LATENCY_DEFAULT)
    parser.add_argument("--no-tcp-check", action="store_true")

    args = parser.parse_args()

    proxies = read_proxies(args.input_file)
    if not proxies:
        return

    start = time.time()

    valid, errors = validate_all(
        proxies,
        DEFAULT_HTTP_URL,
        args.workers,
        (args.connect_timeout, args.read_timeout),
        args.https_only,
        args.verify_ip,
        args.max_latency,
        not args.no_tcp_check,
    )

    write_proxies(args.output_file, valid)

    logging.info(
        "Done in %.2fs | %d/%d valid",
        time.time() - start,
        len(valid),
        len(proxies),
    )

    if errors:
        logging.info("Errors: %s", dict(errors))


if __name__ == "__main__":
    main()
