#!/usr/bin/env python3
"""
High-performance multithreaded proxy validator (v2).

Improvements:
- Fixed write_proxies double-iteration bug
- Clearer type hints and docstrings
- Safer session lifecycle (thread-local, lazy init)
- Reduced global state leakage
- Better error normalization
- Optional jitter toggle for benchmarking
- Minor performance and readability refinements
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
from typing import Dict, Iterable, List, Optional, Tuple

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
DEFAULT_JITTER = (0.02, 0.12)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Mozilla/5.0 (X11; Linux x86_64)",
)

PROXY_RE = re.compile(
    r"""
    ^
    (?P<host>[^:\s]+)
    :
    (?P<port>\d{2,5})
    (?:
        :
        (?P<user>[^:]+)
        :
        (?P<pwd>[^:]+)
    )?
    $
    """,
    re.VERBOSE,
)

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
    """Load, deduplicate, and shuffle proxy list."""
    if not path.is_file():
        logging.error("Input file not found: %s", path)
        return []

    proxies = {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    }

    result = list(proxies)
    random.shuffle(result)

    logging.info("Loaded %d unique proxies", len(result))
    return result


def write_proxies(path: Path, proxies: Iterable[str]) -> None:
    """Write validated proxies to disk."""
    proxies = list(proxies)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info("Saved %d proxies to %s", len(proxies), path)
    except Exception as exc:
        logging.error("Failed to write output file: %s", exc)


# ============================================================
#                       PROXY UTILS
# ============================================================

def parse_proxy(line: str) -> Optional[Dict[str, str]]:
    """Parse proxy in ip:port[:user:pass] format."""
    match = PROXY_RE.match(line)
    if not match:
        return None

    d = match.groupdict()
    auth = f"{d['user']}:{d['pwd']}@" if d.get("user") and d.get("pwd") else ""
    base = f"http://{auth}{d['host']}:{d['port']}"

    return {"http": base, "https": base}


def make_session() -> requests.Session:
    """Create a tuned requests.Session."""
    session = requests.Session()

    retry = Retry(
        total=DEFAULT_RETRIES,
        connect=DEFAULT_RETRIES,
        read=DEFAULT_RETRIES,
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
    """Thread-local session accessor."""
    if not hasattr(_tls, "session"):
        _tls.session = make_session()
    return _tls.session


# ============================================================
#                       ERROR CLASSIFICATION
# ============================================================

def classify_error(exc: Exception) -> str:
    """Normalize request exceptions to short error codes."""
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timeout"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl_error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    return type(exc).__name__


# ============================================================
#                       PROXY CHECK
# ============================================================

def check_proxy(
    proxy_line: str,
    url: str,
    timeout: int,
    https_only: bool,
    jitter: Optional[Tuple[float, float]],
) -> Tuple[Optional[str], str]:
    """Validate a single proxy."""
    proxies = parse_proxy(proxy_line)
    if not proxies:
        return None, "invalid_format"

    if https_only:
        proxies = {"https": proxies["https"]}

    session = get_session()

    try:
        if jitter:
            time.sleep(random.uniform(*jitter))

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
    jitter: Optional[Tuple[float, float]],
) -> Tuple[List[str], Counter]:
    """Validate proxies concurrently."""
    valid: List[str] = []
    errors: Counter = Counter()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(check_proxy, p, url, timeout, https_only, jitter)
            for p in proxies
        ]

        for future in tqdm(
            as_completed(futures),
            total=len(futures),
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
                errors[type(exc).__name__] += 1

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
    parser.add_argument("--http-url", default=DEFAULT_HTTP_URL)
    parser.add_argument("--https-url", default=DEFAULT_HTTPS_URL)
    parser.add_argument("--no-jitter", action="store_true", help="Disable request jitter")

    args = parser.parse_args()

    proxies = read_proxies(args.input_file)
    if not proxies:
        logging.warning("No proxies to validate")
        return

    jitter = None if args.no_jitter else DEFAULT_JITTER
    start = time.time()

    try:
        valid, errors = validate_all(
            proxies,
            args.http_url,
            args.timeout,
            args.max_workers,
            args.https_only,
            jitter,
        )

        if args.also_test_https and valid:
            logging.info("Re-testing %d proxies with HTTPS", len(valid))
            valid, https_errors = validate_all(
                valid,
                args.https_url,
                args.timeout,
                args.max_workers,
                https_only=True,
                jitter=jitter,
            )
            errors.update(https_errors)

    except KeyboardInterrupt:
        logging.warning("Interrupted — saving partial results")
        write_proxies(args.output_file, valid)
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
            ", ".join(f"{k}:{v}" for k, v in errors.most_common()),
        )


if __name__ == "__main__":
    main()
