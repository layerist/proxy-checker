import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict, TypedDict, Set

import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm
import urllib3

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logger configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s"
)

class ProxyDict(TypedDict):
    http: str
    https: str

# Defaults
DEFAULT_TEST_URL = "http://httpbin.org/ip"
DEFAULT_TIMEOUT = 5
DEFAULT_MAX_WORKERS = 20


def read_proxies(file_path: Path) -> List[str]:
    """Reads proxies from file and returns unique, cleaned entries."""
    if not file_path.exists():
        logging.error(f"Input file not found: {file_path}")
        return []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        unique_proxies: Set[str] = {line.strip() for line in lines if line.strip()}
        logging.info(f"Loaded {len(unique_proxies)} unique proxies.")
        return list(unique_proxies)
    except Exception as e:
        logging.error(f"Failed to read proxies: {e}")
        return []


def parse_proxy_line(line: str) -> Optional[ProxyDict]:
    """Parses proxy strings like IP:PORT or IP:PORT:USER:PASS."""
    parts = line.strip().split(":")
    try:
        if len(parts) == 2:
            ip, port = parts
            auth = ""
        elif len(parts) == 4:
            ip, port, user, pwd = parts
            auth = f"{user}:{pwd}@"
        else:
            logging.debug(f"Ignored malformed proxy: {line}")
            return None
        proxy_url = f"http://{auth}{ip}:{port}"
        return {"http": proxy_url, "https": proxy_url}
    except Exception as e:
        logging.debug(f"Error parsing proxy line: {line} — {e}")
        return None


def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """Creates a requests session with retry logic."""
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


def check_proxy(proxy_line: str, url: str, timeout: int, https_only: bool) -> Optional[str]:
    """Returns proxy line if it successfully connects to the test URL."""
    proxies = parse_proxy_line(proxy_line)
    if not proxies:
        return None

    if https_only:
        proxies["http"] = ""

    session = make_session()
    try:
        response = session.get(url, proxies=proxies, timeout=timeout, verify=False)
        if response.ok:
            logging.debug(f"Valid proxy: {proxy_line}")
            return proxy_line
    except requests.RequestException as e:
        logging.debug(f"Failed proxy: {proxy_line} — {e}")
    return None


def write_proxies(path: Path, proxies: List[str]) -> None:
    """Writes list of valid proxies to output file."""
    try:
        path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info(f"Saved {len(proxies)} valid proxies to {path}")
    except Exception as e:
        logging.error(f"Failed to write output file: {e}")


def validate_proxies_concurrently(
    proxies: List[str],
    url: str,
    timeout: int,
    max_workers: int,
    https_only: bool
) -> List[str]:
    """Runs proxy validation in parallel using threads."""
    valid: List[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(check_proxy, proxy, url, timeout, https_only): proxy
            for proxy in proxies
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="Checking proxies", ncols=80):
            result = future.result()
            if result:
                valid.append(result)
    return valid


def main() -> None:
    parser = argparse.ArgumentParser(description="Multithreaded proxy validator")
    parser.add_argument("input_file", type=Path, help="Input file with proxy list")
    parser.add_argument("output_file", type=Path, help="Output file for valid proxies")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Concurrent threads")
    parser.add_argument("--test-url", type=str, default=DEFAULT_TEST_URL, help="URL to test proxies against")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout per proxy request")
    parser.add_argument("--https-only", action="store_true", help="Only validate HTTPS proxies")

    args = parser.parse_args()

    if args.max_workers < 2:
        logging.error("Minimum --max-workers is 2.")
        return

    proxies = read_proxies(args.input_file)
    if not proxies:
        logging.warning("No proxies to validate.")
        return

    logging.info(f"Starting validation using {args.max_workers} threads...")

    start = time.time()
    try:
        valid_proxies = validate_proxies_concurrently(
            proxies, args.test_url, args.timeout, args.max_workers, args.https_only
        )
    except KeyboardInterrupt:
        logging.warning("Validation interrupted.")
        valid_proxies = []

    duration = time.time() - start
    write_proxies(args.output_file, valid_proxies)
    logging.info(f"Completed in {duration:.2f}s — {len(valid_proxies)}/{len(proxies)} proxies valid.")


if __name__ == "__main__":
    main()
