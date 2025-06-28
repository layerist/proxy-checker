import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict, TypedDict

import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm
import urllib3

# Suppress warnings for insecure requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s"
)

class ProxyDict(TypedDict):
    http: str
    https: str

DEFAULT_TEST_URL = "http://httpbin.org/ip"
DEFAULT_TIMEOUT = 5
DEFAULT_MAX_WORKERS = 20


def read_proxies(file_path: Path) -> List[str]:
    """
    Reads proxies from a file and returns a deduplicated, stripped list.
    """
    if not file_path.is_file():
        logging.error(f"Proxy list file not found: {file_path}")
        return []

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        proxies = list({line.strip() for line in lines if line.strip()})
        logging.info(f"Loaded {len(proxies)} unique proxies from {file_path}")
        return proxies
    except Exception as exc:
        logging.error(f"Error reading proxies from {file_path}: {exc}")
        return []


def parse_proxy_line(line: str) -> Optional[ProxyDict]:
    """
    Parses a proxy line into a requests-compatible dictionary.
    Supports IP:PORT or IP:PORT:USER:PASS
    """
    parts = line.strip().split(":")
    try:
        if len(parts) == 2:
            ip, port = parts
            proxy_url = f"http://{ip}:{port}"
        elif len(parts) == 4:
            ip, port, user, pw = parts
            proxy_url = f"http://{user}:{pw}@{ip}:{port}"
        else:
            logging.debug(f"Ignored invalid proxy format: {line}")
            return None
        return {"http": proxy_url, "https": proxy_url}
    except Exception as exc:
        logging.debug(f"Failed to parse proxy '{line}': {exc}")
        return None


def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """
    Returns a requests session with retry logic enabled.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def check_proxy(
    proxy_line: str,
    test_url: str,
    timeout: int,
    https_only: bool
) -> Optional[str]:
    """
    Checks if a proxy works with the given test URL.
    Returns the proxy line if working, otherwise None.
    """
    proxies = parse_proxy_line(proxy_line)
    if not proxies:
        return None

    if https_only:
        proxies["http"] = ""

    session = make_session()
    try:
        response = session.get(
            test_url,
            proxies=proxies,
            timeout=timeout,
            verify=False
        )
        if response.status_code == 200:
            logging.debug(f"Proxy working: {proxy_line}")
            return proxy_line
    except requests.RequestException as exc:
        logging.debug(f"Proxy failed: {proxy_line} — {exc}")
    return None


def write_proxies(file_path: Path, proxies: List[str]) -> None:
    """
    Writes working proxies to a file.
    """
    try:
        file_path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info(f"Saved {len(proxies)} working proxies to {file_path}")
    except Exception as exc:
        logging.error(f"Error writing proxies to {file_path}: {exc}")


def validate_proxies_concurrently(
    proxies: List[str],
    test_url: str,
    timeout: int,
    max_workers: int,
    https_only: bool
) -> List[str]:
    """
    Validates proxies in parallel using a thread pool.
    """
    valid_proxies: List[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(check_proxy, proxy, test_url, timeout, https_only)
            for proxy in proxies
        ]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Validating proxies", ncols=80):
            result = future.result()
            if result:
                valid_proxies.append(result)
    return valid_proxies


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and filter working HTTP/HTTPS proxies."
    )
    parser.add_argument("input_file", type=Path, help="Path to input proxy list (IP:PORT or IP:PORT:USER:PASS)")
    parser.add_argument("output_file", type=Path, help="Path to output valid proxies")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Number of concurrent threads")
    parser.add_argument("--test-url", type=str, default=DEFAULT_TEST_URL, help="Test URL to verify proxies")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout in seconds")
    parser.add_argument("--https-only", action="store_true", help="Validate only using HTTPS")

    args = parser.parse_args()

    if args.max_workers < 2:
        logging.error("--max-workers must be at least 2.")
        return

    proxies = read_proxies(args.input_file)
    if not proxies:
        return

    start_time = time.time()
    try:
        valid_proxies = validate_proxies_concurrently(
            proxies=proxies,
            test_url=args.test_url,
            timeout=args.timeout,
            max_workers=args.max_workers,
            https_only=args.https_only
        )
    except KeyboardInterrupt:
        logging.warning("Validation interrupted by user.")
        valid_proxies = []

    elapsed = time.time() - start_time
    write_proxies(args.output_file, valid_proxies)
    logging.info(
        f"Finished in {elapsed:.2f} seconds — {len(valid_proxies)}/{len(proxies)} proxies are valid."
    )


if __name__ == "__main__":
    main()
