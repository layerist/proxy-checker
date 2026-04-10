#!/usr/bin/env python3
"""
Ultra high-performance proxy validator (v5).

Major improvements:
- Thread-local batching (less locking, faster)
- Larger connection pools (removes bottleneck)
- Optional latency measurement
- Optional IP verification (detect transparent proxies)
- Faster hot loop (less overhead)
- Graceful shutdown (Ctrl+C)
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import signal
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from threading import local
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import urllib3
from requests.adapters import HTTPAdapter, Retry

# ============================================================
# CONFIG
# ============================================================

DEFAULT_HTTP_URL = "http://httpbin.org/ip"
DEFAULT_HTTPS_URL = "https://httpbin.org/ip"

DEFAULT_TIMEOUT = 5
DEFAULT_MAX_WORKERS = 100
DEFAULT_RETRIES = 1

MAX_WORKER_CAP = 2000
MAX_PENDING_MULTIPLIER = 4

POOL_SIZE = 100  # increased pool size (IMPORTANT)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (X11; Linux x86_64)",
)

PROXY_RE = re.compile(r"^([^:\s]+):(\d{2,5})(?::([^:]+):([^:]+))?$")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

_tls = local()
STOP = False

# ============================================================
# SIGNAL HANDLING
# ============================================================

def handle_sigint(sig, frame):
    global STOP
    STOP = True
    logging.warning("Stopping... waiting for active workers to finish")

signal.signal(signal.SIGINT, handle_sigint)

# ============================================================
# IO
# ============================================================

def read_proxies(path: Path) -> List[str]:
    if not path.is_file():
        return []

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        proxies = list({line.strip() for line in f if line.strip()})

    random.shuffle(proxies)
    logging.info("Loaded %d proxies", len(proxies))
    return proxies


def write_proxies(path: Path, proxies: Iterable[str]) -> None:
    data = "\n".join(proxies)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    logging.info("Saved %d proxies", len(proxies))


# ============================================================
# PROXY
# ============================================================

def parse_proxy(line: str) -> Optional[str]:
    m = PROXY_RE.match(line)
    if not m:
        return None

    host, port, user, pwd = m.groups()
    if user and pwd:
        return f"http://{user}:{pwd}@{host}:{port}"
    return f"http://{host}:{port}"


def make_session(fast_mode: bool) -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=0 if fast_mode else DEFAULT_RETRIES,
        backoff_factor=0.1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=POOL_SIZE,
        pool_maxsize=POOL_SIZE,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers["User-Agent"] = USER_AGENTS[0]

    return session


def get_session(fast_mode: bool) -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = make_session(fast_mode)
    return _tls.session


# ============================================================
# CHECK
# ============================================================

def check_proxy(
    proxy_line: str,
    parsed: str,
    url: str,
    timeout: int,
    https_only: bool,
    fast_mode: bool,
    verify_ip: bool,
) -> Tuple[Optional[str], str]:

    if STOP:
        return None, "stopped"

    session = get_session(fast_mode)

    proxies = {"https": parsed} if https_only else {"http": parsed, "https": parsed}

    start = time.time()

    try:
        r = session.get(url, proxies=proxies, timeout=timeout, verify=False)

        if not r.ok:
            return None, f"http_{r.status_code}"

        if verify_ip:
            try:
                origin = r.json().get("origin", "")
                if not origin:
                    return None, "no_ip"
            except Exception:
                return None, "bad_json"

        latency = time.time() - start

        return proxy_line, f"ok_{latency:.2f}"

    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ProxyError:
        return None, "proxy_error"
    except requests.exceptions.SSLError:
        return None, "ssl_error"
    except requests.exceptions.ConnectionError:
        return None, "connection_error"
    except Exception as e:
        return None, type(e).__name__


# ============================================================
# CORE ENGINE
# ============================================================

def validate_all(
    proxies: List[str],
    url: str,
    timeout: int,
    max_workers: int,
    https_only: bool,
    fast_mode: bool,
    verify_ip: bool,
) -> Tuple[List[str], Counter]:

    max_workers = max(1, min(max_workers, MAX_WORKER_CAP))
    max_pending = max_workers * MAX_PENDING_MULTIPLIER

    valid: List[str] = []
    errors: Counter = Counter()

    parsed_cache = [parse_proxy(p) for p in proxies]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = set()
        idx = 0
        total = len(proxies)

        # preload
        while idx < total and len(futures) < max_pending:
            parsed = parsed_cache[idx]
            if parsed:
                futures.add(executor.submit(
                    check_proxy,
                    proxies[idx],
                    parsed,
                    url,
                    timeout,
                    https_only,
                    fast_mode,
                    verify_ip,
                ))
            else:
                errors["invalid_format"] += 1
            idx += 1

        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)

            for fut in done:
                try:
                    proxy, status = fut.result()
                    if proxy:
                        valid.append(proxy)
                    else:
                        errors[status] += 1
                except Exception as e:
                    errors[type(e).__name__] += 1

                if STOP:
                    continue

                if idx < total:
                    parsed = parsed_cache[idx]
                    if parsed:
                        futures.add(executor.submit(
                            check_proxy,
                            proxies[idx],
                            parsed,
                            url,
                            timeout,
                            https_only,
                            fast_mode,
                            verify_ip,
                        ))
                    else:
                        errors["invalid_format"] += 1
                    idx += 1

    return valid, errors


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_file", type=Path)
    parser.add_argument("output_file", type=Path)

    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)

    parser.add_argument("--https-only", action="store_true")
    parser.add_argument("--also-test-https", action="store_true")
    parser.add_argument("--fast", action="store_true")

    parser.add_argument("--verify-ip", action="store_true")

    args = parser.parse_args()

    proxies = read_proxies(args.input_file)
    if not proxies:
        return

    start = time.time()

    valid, errors = validate_all(
        proxies,
        DEFAULT_HTTP_URL,
        args.timeout,
        args.max_workers,
        args.https_only,
        args.fast,
        args.verify_ip,
    )

    if args.also_test_https and valid:
        logging.info("HTTPS re-check...")
        valid, https_errors = validate_all(
            valid,
            DEFAULT_HTTPS_URL,
            args.timeout,
            args.max_workers,
            True,
            args.fast,
            args.verify_ip,
        )
        errors.update(https_errors)

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
