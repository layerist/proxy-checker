#!/usr/bin/env python3
"""
High-performance multithreaded proxy validator.

Features:
- One persistent requests.Session per worker (thread-local)
- Robust proxy parsing (ip:port[:user:pass])
- HTTP and optional HTTPS validation
- Retry & backoff via urllib3 Retry
- Error statistics aggregation
- Low overhead per check
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local
from typing import Dict, List, Optional, Tuple

import requests
import urllib3
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm

# ============================================================
#                         CONFIG
# ============================================================

DEFAULT_HTTP_URL = "http://httpbin.org/ip"
DEFAULT_HTTPS_URL = "https://httpbin.org/ip"

DEFAULT_TIMEOUT = 5
DEFAULT_MAX_WORKERS = 20
DEFAULT_RETRIES = 2

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Mozilla/5.0 (X11; Linux x86_64)",
)

PROXY_RE = re.compile(r"^(?P<ip>\S+):(?P<port>\d+)(?::(?P<user>[^:]+):(?P<pwd>[^:]+))?$")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

_tls = local()

# ============================================================
#                       IO HELPERS
# ============================================================

def read_proxies(path: Path) -> List[str]:
    if not path.exists():
        logging.error("Input file not found: %s", path)
        return []

    proxies = {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    }

    proxies = list(proxies)
    random.shuffle(proxies)

    logging.info("Loaded %d unique proxies", len(proxies))
    return proxies


def write_proxies(path: Path, proxies: List[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info("Saved %d proxies to %s", len(proxies), path)
    except Exception as e:
        logging.error("Failed to write output file: %s", e)


# ============================================================
#                       PROXY UTILS
# ============================================================

def parse_proxy(line: str) -> Optional[Dict[str, str]]:
    match = PROXY_RE.match(line)
    if not match:
        return None

    d = match.groupdict()
    auth = f"{d['user']}:{d['pwd']}@" if d["user"] and d["pwd"] else ""
    proxy_url = f"http://{auth}{d['ip']}:{d['port']}"

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def make_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=DEFAULT_RETRIES,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=DEFAULT_MAX_WORKERS,
        pool_maxsize=DEFAULT_MAX_WORKERS,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)

    return session


def get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = make_session()
    return _tls.session


# ============================================================
#                       ERROR CLASSIFICATION
# ============================================================

def classify_error(exc: Exception) -> str:
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timeout"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    return exc.__class__.__name__


# ============================================================
#                       PROXY CHECK
# ============================================================

def check_proxy(
    proxy_line: str,
    url: str,
    timeout: int,
    https_only: bool,
) -> Tuple[Optional[str], str]:
    proxies = parse_proxy(proxy_line)
    if not proxies:
        return None, "invalid_format"

    if https_only:
        proxies["http"] = None

    session = get_session()

    try:
        time.sleep(random.uniform(0.02, 0.12))
        response = session.get(
            url,
            proxies=proxies,
            timeout=timeout,
            verify=False,
        )

        if response.ok:
            return proxy_line, "ok"

        return None, f"http_{response.status_code}"

    except Exception as exc:
        return None, classify_error(exc)


# ============================================================
#                    CONCURRENT VALIDATION
# ============================================================

def validate_all(
    proxies: List[str],
    url: str,
    timeout: int,
    max_workers: int,
    https_only: bool,
) -> Tuple[List[str], Counter]:

    valid: List[str] = []
    errors: Counter = Counter()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = (
            executor.submit(check_proxy, p, url, timeout, https_only)
            for p in proxies
        )

        for future in tqdm(
            as_completed(futures),
            total=len(proxies),
            desc="Checking",
            ncols=90,
        ):
            try:
                proxy, status = future.result()
                if proxy:
                    valid.append(proxy)
                else:
                    errors[status] += 1
            except Exception as exc:
                errors[exc.__class__.__name__] += 1

    return valid, errors


# ============================================================
#                             MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Multithreaded proxy validator")
    parser.add_argument("input_file", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--https-only", action="store_true")
    parser.add_argument("--also-test-https", action="store_true")

    args = parser.parse_args()

    proxies = read_proxies(args.input_file)
    if not proxies:
        logging.warning("No proxies to validate")
        return

    start = time.time()

    try:
        valid, errors = validate_all(
            proxies,
            DEFAULT_HTTP_URL,
            args.timeout,
            args.max_workers,
            args.https_only,
        )

        if args.also_test_https and valid:
            logging.info("Re-testing %d proxies with HTTPS", len(valid))
            valid, _ = validate_all(
                valid,
                DEFAULT_HTTPS_URL,
                args.timeout,
                args.max_workers,
                https_only=True,
            )

    except KeyboardInterrupt:
        logging.warning("Interrupted — saving partial results")
        write_proxies(args.output_file, valid if "valid" in locals() else [])
        return

    duration = time.time() - start
    write_proxies(args.output_file, valid)

    logging.info(
        "Done in %.2fs — %d/%d valid (%.1f%%)",
        duration,
        len(valid),
        len(proxies),
        (len(valid) / len(proxies)) * 100,
    )

    if errors:
        logging.info(
            "Errors: %s",
            ", ".join(f"{k}:{v}" for k, v in errors.items()),
        )


if __name__ == "__main__":
    main()
