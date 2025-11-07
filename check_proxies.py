#!/usr/bin/env python3
"""
Enhanced multithreaded proxy validator.
- Reuses connections per thread (persistent session pool)
- Adaptive retries, timeout, and random user agents
- Better error classification and summary reporting
"""

import argparse
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict, Set, TypedDict, Tuple
from collections import Counter

import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm
import urllib3

# Disable SSL warnings (proxy testing context only)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s"
)

class ProxyDict(TypedDict, total=False):
    http: str
    https: str


# Defaults
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


def read_proxies(file_path: Path) -> List[str]:
    """Load unique proxy lines from file."""
    if not file_path.exists():
        logging.error(f"Input file not found: {file_path}")
        return []

    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        proxies = {line.strip() for line in lines if line.strip()}
        proxies_list = list(proxies)
        random.shuffle(proxies_list)
        logging.info(f"Loaded {len(proxies_list)} unique proxies from {file_path}")
        return proxies_list
    except Exception as e:
        logging.error(f"Failed to read proxies: {e}")
        return []


def parse_proxy_line(line: str) -> Optional[ProxyDict]:
    """Parse 'IP:PORT' or 'IP:PORT:USER:PASS' formats."""
    try:
        match = re.match(r"^(\S+):(\d+)(?::([^:]+):([^:]+))?$", line.strip())
        if not match:
            logging.debug(f"Invalid proxy format: {line}")
            return None
        ip, port, user, pwd = match.groups()
        auth = f"{user}:{pwd}@" if user and pwd else ""
        proxy_url = f"http://{auth}{ip}:{port}"
        return {"http": proxy_url, "https": proxy_url}
    except Exception as e:
        logging.debug(f"Error parsing proxy '{line}': {e}")
        return None


def make_session(retries: int = DEFAULT_RETRIES) -> requests.Session:
    """Create a persistent session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=random.uniform(0.2, 0.6),
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=DEFAULT_MAX_WORKERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return session


def check_proxy(
    proxy_line: str,
    test_url: str,
    timeout: int,
    https_only: bool
) -> Tuple[Optional[str], str]:
    """Check single proxy and return (proxy_line, status)."""
    proxies = parse_proxy_line(proxy_line)
    if not proxies:
        return None, "invalid_format"
    if https_only:
        proxies["http"] = ""

    session = make_session()
    try:
        time.sleep(random.uniform(0.05, 0.25))  # jitter
        resp = session.get(test_url, proxies=proxies, timeout=timeout, verify=False)
        if resp.ok:
            return proxy_line, "ok"
        else:
            return None, f"http_{resp.status_code}"
    except requests.exceptions.ConnectTimeout:
        return None, "timeout"
    except requests.exceptions.ReadTimeout:
        return None, "timeout"
    except requests.exceptions.ProxyError:
        return None, "proxy_error"
    except requests.exceptions.ConnectionError:
        return None, "conn_error"
    except Exception as e:
        return None, type(e).__name__
    finally:
        session.close()


def write_proxies(path: Path, proxies: List[str]) -> None:
    """Write valid proxies to file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info(f"Saved {len(proxies)} valid proxies to {path}")
    except Exception as e:
        logging.error(f"Failed to write proxies: {e}")


def validate_proxies_concurrently(
    proxies: List[str],
    url: str,
    timeout: int,
    max_workers: int,
    https_only: bool,
) -> Tuple[List[str], Counter]:
    """Validate all proxies concurrently."""
    results = []
    errors = Counter()
    total = len(proxies)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_proxy, p, url, timeout, https_only): p for p in proxies}
        for future in tqdm(as_completed(futures), total=total, desc="Checking proxies", ncols=90):
            try:
                valid, status = future.result()
                if valid:
                    results.append(valid)
                else:
                    errors[status] += 1
            except Exception as e:
                errors[str(type(e))] += 1

    return results, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Enhanced multithreaded proxy validator")
    parser.add_argument("input_file", type=Path, help="Input file with proxies")
    parser.add_argument("output_file", type=Path, help="Output file for valid proxies")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--https-only", action="store_true", help="Test only HTTPS connectivity")
    parser.add_argument("--also-test-https", action="store_true", help="Re-test valid proxies with HTTPS URL")

    args = parser.parse_args()
    proxies = read_proxies(args.input_file)
    if not proxies:
        logging.warning("No proxies to validate.")
        return

    start = time.time()
    try:
        logging.info(f"Testing {len(proxies)} proxies with {args.max_workers} threads...")
        valid, errors = validate_proxies_concurrently(
            proxies, DEFAULT_TEST_URL_HTTP, args.timeout, args.max_workers, args.https_only
        )

        if args.also_test_https and valid:
            logging.info("Re-testing valid proxies with HTTPS...")
            valid, _ = validate_proxies_concurrently(
                valid, DEFAULT_TEST_URL_HTTPS, args.timeout, args.max_workers, True
            )

    except KeyboardInterrupt:
        logging.warning("Interrupted by user. Writing partial results...")
        write_proxies(args.output_file, valid if 'valid' in locals() else [])
        return

    duration = time.time() - start
    write_proxies(args.output_file, valid)
    success_rate = (len(valid) / len(proxies) * 100) if proxies else 0

    logging.info(
        f"Done in {duration:.2f}s — {len(valid)}/{len(proxies)} valid ({success_rate:.1f}%)."
    )
    if errors:
        err_str = ", ".join(f"{k}:{v}" for k, v in errors.most_common())
        logging.info(f"Error summary: {err_str}")


if __name__ == "__main__":
    main()
