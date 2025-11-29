#!/usr/bin/env python3
"""
Enhanced multithreaded proxy validator — improved version.
- Session pool (1 session per thread, reused)
- More efficient error handling
- Cleaner parsing + structure
- Lower overhead in check_proxy
"""

import argparse
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from collections import Counter

import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s"
)

DEFAULT_TEST_URL_HTTP = "http://httpbin.org/ip"
DEFAULT_TEST_URL_HTTPS = "https://httpbin.org/ip"
DEFAULT_TIMEOUT = 5
DEFAULT_MAX_WORKERS = 20
DEFAULT_RETRIES = 2

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Mozilla/5.0 (X11; Linux x86_64)",
]

PROXY_RE = re.compile(r"^(\S+):(\d+)(?::([^:]+):([^:]+))?$")


# ============================================================
#                           UTILITIES
# ============================================================

def read_proxies(path: Path) -> List[str]:
    """Load and deduplicate proxies."""
    if not path.exists():
        logging.error(f"Input file not found: {path}")
        return []

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        proxies = list({l.strip() for l in lines if l.strip()})
        logging.info(f"Loaded {len(proxies)} proxies")
        random.shuffle(proxies)
        return proxies
    except Exception as e:
        logging.error(f"Failed to read proxies: {e}")
        return []


def parse_proxy(line: str) -> Optional[Dict[str, str]]:
    """Parse proxy formats into requests dict."""
    match = PROXY_RE.match(line)
    if not match:
        return None

    ip, port, user, pwd = match.groups()
    auth = f"{user}:{pwd}@" if user and pwd else ""
    url = f"http://{auth}{ip}:{port}"
    return {"http": url, "https": url}


def make_session() -> requests.Session:
    """Create reusable session for each worker."""
    session = requests.Session()
    retry = Retry(
        total=DEFAULT_RETRIES,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=DEFAULT_MAX_WORKERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)
    return session


# Thread-local session: one per worker
from threading import local
_tls = local()


def get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = make_session()
    return _tls.session


# ============================================================
#                       PROXY CHECKER
# ============================================================

def classify_error(err: Exception) -> str:
    if isinstance(err, requests.exceptions.ConnectTimeout):
        return "timeout"
    if isinstance(err, requests.exceptions.ReadTimeout):
        return "timeout"
    if isinstance(err, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(err, requests.exceptions.ConnectionError):
        return "conn_error"
    return type(err).__name__


def check_proxy(
        proxy_line: str,
        url: str,
        timeout: int,
        https_only: bool
) -> Tuple[Optional[str], str]:
    """Check a single proxy; returns (proxy_line or None, status)."""

    proxies = parse_proxy(proxy_line)
    if not proxies:
        return None, "invalid_format"

    if https_only:
        proxies["http"] = None

    session = get_session()

    try:
        time.sleep(random.uniform(0.02, 0.12))
        resp = session.get(url, proxies=proxies, timeout=timeout, verify=False)
        return (proxy_line, "ok") if resp.ok else (None, f"http_{resp.status_code}")

    except Exception as e:
        return None, classify_error(e)


# ============================================================
#                       CONCURRENT VALIDATION
# ============================================================

def validate_all(
        proxies: List[str],
        url: str,
        timeout: int,
        max_workers: int,
        https_only: bool,
) -> Tuple[List[str], Counter]:

    results = []
    errors = Counter()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(check_proxy, p, url, timeout, https_only): p for p in proxies}

        for fut in tqdm(as_completed(futures), total=len(futures), ncols=90, desc="Checking"):
            try:
                valid, status = fut.result()
                if valid:
                    results.append(valid)
                else:
                    errors[status] += 1
            except Exception as e:
                errors[type(e).__name__] += 1

    return results, errors


# ============================================================
#                             MAIN
# ============================================================

def write_proxies(path: Path, proxies: List[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info(f"Saved {len(proxies)} proxies to {path}")
    except Exception as e:
        logging.error(f"Failed to write proxies: {e}")


def main():
    parser = argparse.ArgumentParser(description="Proxy validator")
    parser.add_argument("input_file", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--https-only", action="store_true")
    parser.add_argument("--also-test-https", action="store_true")
    args = parser.parse_args()

    proxies = read_proxies(args.input_file)
    if not proxies:
        logging.warning("No proxies to validate.")
        return

    start = time.time()

    try:
        logging.info(f"Testing {len(proxies)} proxies...")

        valid, errors = validate_all(
            proxies, DEFAULT_TEST_URL_HTTP, args.timeout, args.max_workers, args.https_only
        )

        if args.also_test_https and valid:
            logging.info("Re-testing valid proxies with HTTPS...")
            valid, _ = validate_all(
                valid, DEFAULT_TEST_URL_HTTPS, args.timeout, args.max_workers, True
            )

    except KeyboardInterrupt:
        logging.warning("Interrupted, saving partial results...")
        write_proxies(args.output_file, valid if "valid" in locals() else [])
        return

    duration = time.time() - start
    write_proxies(args.output_file, valid)

    logging.info(
        f"Done in {duration:.2f}s — {len(valid)}/{len(proxies)} valid "
        f"({len(valid) / len(proxies) * 100:.1f}%)."
    )

    if errors:
        logging.info("Errors: " + ", ".join(f"{k}:{v}" for k, v in errors.items()))


if __name__ == "__main__":
    main()
