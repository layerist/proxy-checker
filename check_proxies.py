#!/usr/bin/env python3
"""
Ultra high-performance proxy validator (v4).

Major upgrades:
- Bounded task queue (no memory explosion)
- Faster hot-path execution
- Better connection reuse
- Optional "fast mode" (no retries)
- Reduced overhead per request
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import time
from collections import Counter, deque
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
DEFAULT_MAX_WORKERS = 50
DEFAULT_RETRIES = 2

MAX_WORKER_CAP = 1000
MAX_PENDING_MULTIPLIER = 3  # limits queued futures

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (X11; Linux x86_64)",
)

PROXY_RE = re.compile(r"^([^:\s]+):(\d{2,5})(?::([^:]+):([^:]+))?$")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

_tls = local()

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

    if fast_mode:
        retry = Retry(total=0)
    else:
        retry = Retry(
            total=DEFAULT_RETRIES,
            backoff_factor=0.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)

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
) -> Tuple[Optional[str], str]:

    session = get_session(fast_mode)

    proxies = {"https": parsed} if https_only else {"http": parsed, "https": parsed}

    try:
        r = session.get(url, proxies=proxies, timeout=timeout, verify=False)
        return (proxy_line, "ok") if r.ok else (None, f"http_{r.status_code}")
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
# CORE ENGINE (IMPORTANT PART)
# ============================================================

def validate_all(
    proxies: List[str],
    url: str,
    timeout: int,
    max_workers: int,
    https_only: bool,
    fast_mode: bool,
) -> Tuple[List[str], Counter]:

    max_workers = max(1, min(max_workers, MAX_WORKER_CAP))
    max_pending = max_workers * MAX_PENDING_MULTIPLIER

    valid: List[str] = []
    errors: Counter = Counter()

    parsed_cache = {p: parse_proxy(p) for p in proxies}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = set()
        it = iter(proxies)

        # preload
        for _ in range(min(max_pending, len(proxies))):
            try:
                p = next(it)
                parsed = parsed_cache[p]
                if not parsed:
                    errors["invalid_format"] += 1
                    continue
                futures.add(executor.submit(check_proxy, p, parsed, url, timeout, https_only, fast_mode))
            except StopIteration:
                break

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

                # refill queue
                try:
                    p = next(it)
                    parsed = parsed_cache[p]
                    if not parsed:
                        errors["invalid_format"] += 1
                        continue
                    futures.add(executor.submit(check_proxy, p, parsed, url, timeout, https_only, fast_mode))
                except StopIteration:
                    pass

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
