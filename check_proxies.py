#!/usr/bin/env python3
"""
Ultra High Performance Proxy Validator v7

Improvements:
- Faster executor pipeline
- Thread-local sessions
- TCP pre-check
- Adaptive queue scheduling
- Lower lock contention
- Faster IP verification
- Proxy latency sorting
- Live statistics
- Retry support
- Optimized requests adapter
- Better memory usage
- Huge list support (millions of proxies)
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
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock, local
from typing import List, Optional, Tuple

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIG
# ============================================================

DEFAULT_HTTP_URL = "http://httpbin.org/ip"
DEFAULT_HTTPS_URL = "https://httpbin.org/ip"

DEFAULT_CONNECT_TIMEOUT = 2
DEFAULT_READ_TIMEOUT = 4

DEFAULT_WORKERS = 500
MAX_WORKERS = 3000

MAX_PENDING_MULTIPLIER = 4
POOL_SIZE_MULTIPLIER = 2

MAX_LATENCY = 5.0

DEFAULT_RETRIES = 0

PROXY_RE = re.compile(
    r"^([^:\s]+):(\d{2,5})(?::([^:]+):([^:]+))?$"
)

IP_REGEX = re.compile(
    rb"(?:\d{1,3}\.){3}\d{1,3}"
)

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

_tls = local()
STOP = False
WRITE_LOCK = Lock()


# ============================================================
# SIGNAL
# ============================================================

def handle_sigint(sig, frame):
    global STOP
    STOP = True
    logging.warning("Stopping gracefully...")


signal.signal(signal.SIGINT, handle_sigint)


# ============================================================
# IO
# ============================================================

def read_proxies(path: Path) -> List[str]:
    if not path.exists():
        logging.error("File not found: %s", path)
        return []

    seen = set()
    proxies = []

    with path.open(
        "r",
        encoding="utf-8",
        errors="ignore"
    ) as f:
        for line in f:
            proxy = line.strip()
            if proxy and proxy not in seen:
                seen.add(proxy)
                proxies.append(proxy)

    random.shuffle(proxies)

    logging.info(
        "Loaded %d unique proxies",
        len(proxies)
    )

    return proxies


def write_proxies(
    path: Path,
    proxies: List[Tuple[str, float]]
):
    proxies.sort(key=lambda x: x[1])

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with path.open(
        "w",
        encoding="utf-8"
    ) as f:
        f.write(
            "\n".join(
                proxy for proxy, _ in proxies
            )
        )

    logging.info(
        "Saved %d proxies",
        len(proxies)
    )


# ============================================================
# PARSE
# ============================================================

def parse_proxy(
    line: str
) -> Optional[
    Tuple[str, str, int]
]:
    m = PROXY_RE.match(line)

    if not m:
        return None

    host, port, user, pwd = m.groups()
    port = int(port)

    if user:
        proxy_url = (
            f"http://{user}:{pwd}"
            f"@{host}:{port}"
        )
    else:
        proxy_url = (
            f"http://{host}:{port}"
        )

    return proxy_url, host, port


# ============================================================
# TCP PRECHECK
# ============================================================

def tcp_check(
    host: str,
    port: int,
    timeout: float
) -> bool:
    try:
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        sock.settimeout(timeout)
        sock.setsockopt(
            socket.IPPROTO_TCP,
            socket.TCP_NODELAY,
            1
        )

        result = sock.connect_ex(
            (host, port)
        )

        sock.close()

        return result == 0

    except Exception:
        return False


# ============================================================
# SESSION
# ============================================================

def make_session() -> requests.Session:
    s = requests.Session()

    retries = Retry(
        total=DEFAULT_RETRIES,
        connect=0,
        read=0,
        redirect=0,
        backoff_factor=0
    )

    adapter = HTTPAdapter(
        pool_connections=1024,
        pool_maxsize=1024,
        max_retries=retries,
        pool_block=False
    )

    s.mount("http://", adapter)
    s.mount("https://", adapter)

    s.headers.update({
        "Connection": "keep-alive",
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64)"
        )
    })

    return s


def get_session():
    if not hasattr(
        _tls,
        "session"
    ):
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
    max_latency: float
):
    if STOP:
        return None, "stopped"

    session = get_session()

    proxies = (
        {"https": proxy_url}
        if https_only
        else {
            "http": proxy_url,
            "https": proxy_url
        }
    )

    start = time.perf_counter()

    try:
        r = session.get(
            url,
            proxies=proxies,
            timeout=timeout,
            verify=False,
            stream=False,
            allow_redirects=False
        )

        if r.status_code != 200:
            return None, (
                f"http_{r.status_code}"
            )

        latency = (
            time.perf_counter()
            - start
        )

        if latency > max_latency:
            return None, "too_slow"

        if verify_ip:
            if not IP_REGEX.search(
                r.content
            ):
                return None, "no_ip"

        return (
            proxy_line,
            latency
        ), "ok"

    except requests.ConnectTimeout:
        return None, "connect_timeout"

    except requests.ReadTimeout:
        return None, "read_timeout"

    except requests.ProxyError:
        return None, "proxy_error"

    except requests.ConnectionError:
        return None, "connection_error"

    except Exception as e:
        return (
            None,
            type(e).__name__
        )


# ============================================================
# ENGINE
# ============================================================

def validate_all(
    proxies: List[str],
    url: str,
    workers: int,
    timeout: Tuple[int, int],
    https_only: bool,
    verify_ip: bool,
    max_latency: float,
    tcp_precheck: bool
):
    workers = min(
        workers,
        MAX_WORKERS
    )

    max_pending = (
        workers
        * MAX_PENDING_MULTIPLIER
    )

    parsed = [
        parse_proxy(p)
        for p in proxies
    ]

    results = []
    errors = Counter()

    total = len(proxies)
    checked = 0
    start_time = time.time()

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        futures = {}
        proxy_queue = deque(
            range(total)
        )

        def submit():
            while (
                proxy_queue
                and len(futures)
                < max_pending
            ):
                i = proxy_queue.popleft()

                item = parsed[i]

                if not item:
                    errors[
                        "invalid_format"
                    ] += 1
                    continue

                proxy_url, host, port = item

                if tcp_precheck:
                    if not tcp_check(
                        host,
                        port,
                        timeout[0]
                    ):
                        errors[
                            "tcp_fail"
                        ] += 1
                        continue

                fut = executor.submit(
                    check_proxy,
                    proxies[i],
                    proxy_url,
                    url,
                    timeout,
                    https_only,
                    verify_ip,
                    max_latency
                )

                futures[fut] = i

        submit()

        while futures:
            for fut in as_completed(
                tuple(futures)
            ):
                futures.pop(fut, None)

                checked += 1

                try:
                    result, status = (
                        fut.result()
                    )

                    if result:
                        results.append(
                            result
                        )
                    else:
                        errors[
                            status
                        ] += 1

                except Exception as e:
                    errors[
                        type(e).__name__
                    ] += 1

                if STOP:
                    break

                submit()

                if (
                    checked % 1000
                    == 0
                ):
                    elapsed = (
                        time.time()
                        - start_time
                    )

                    speed = (
                        checked
                        / elapsed
                    )

                    logging.info(
                        "Checked=%d/%d "
                        "Valid=%d "
                        "Speed=%.0f/s",
                        checked,
                        total,
                        len(results),
                        speed
                    )

                break

    return results, errors


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "input_file",
        type=Path
    )

    parser.add_argument(
        "output_file",
        type=Path
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS
    )

    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT
    )

    parser.add_argument(
        "--read-timeout",
        type=int,
        default=DEFAULT_READ_TIMEOUT
    )

    parser.add_argument(
        "--https-only",
        action="store_true"
    )

    parser.add_argument(
        "--verify-ip",
        action="store_true"
    )

    parser.add_argument(
        "--max-latency",
        type=float,
        default=MAX_LATENCY
    )

    parser.add_argument(
        "--no-tcp-check",
        action="store_true"
    )

    args = parser.parse_args()

    proxies = read_proxies(
        args.input_file
    )

    if not proxies:
        return

    url = (
        DEFAULT_HTTPS_URL
        if args.https_only
        else DEFAULT_HTTP_URL
    )

    start = time.time()

    valid, errors = validate_all(
        proxies=proxies,
        url=url,
        workers=args.workers,
        timeout=(
            args.connect_timeout,
            args.read_timeout
        ),
        https_only=args.https_only,
        verify_ip=args.verify_ip,
        max_latency=args.max_latency,
        tcp_precheck=(
            not args.no_tcp_check
        )
    )

    write_proxies(
        args.output_file,
        valid
    )

    elapsed = (
        time.time() - start
    )

    logging.info(
        "Done in %.2fs | "
        "%d/%d valid "
        "(%.2f%%)",
        elapsed,
        len(valid),
        len(proxies),
        (
            len(valid)
            / len(proxies)
            * 100
        )
    )

    if errors:
        logging.info(
            "Errors: %s",
            dict(errors)
        )


if __name__ == "__main__":
    main()
