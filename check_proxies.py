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

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

class ProxyDict(TypedDict):
    http: str
    https: str


def read_proxies(file_path: Path) -> List[str]:
    """Reads proxies from file and returns a deduplicated, cleaned list."""
    if not file_path.is_file():
        logging.error(f"Proxy list file not found: {file_path}")
        return []

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        proxies = list({line.strip() for line in lines if line.strip()})
        logging.info(f"Loaded {len(proxies)} unique proxies from {file_path}")
        return proxies
    except Exception as e:
        logging.error(f"Error reading proxies: {e}")
        return []


def parse_proxy_line(line: str) -> Optional[ProxyDict]:
    """Converts a proxy string to requests-compatible format."""
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
    except Exception as e:
        logging.debug(f"Failed to parse proxy: {line} — {e}")
        return None


def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """Creates a session with retry logic."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def check_proxy(proxy_line: str, test_url: str, timeout: int, https_only: bool) -> Optional[str]:
    """Tests a proxy and returns it if valid."""
    proxies = parse_proxy_line(proxy_line)
    if not proxies:
        return None

    if https_only:
        proxies["http"] = ""

    session = make_session()
    try:
        response = session.get(test_url, proxies=proxies, timeout=timeout, verify=False)
        if response.status_code == 200:
            logging.debug(f"Working proxy: {proxy_line}")
            return proxy_line
    except requests.RequestException as e:
        logging.debug(f"Proxy failed: {proxy_line} — {e}")
    return None


def write_proxies(file_path: Path, proxies: List[str]) -> None:
    """Writes a list of proxies to a file."""
    try:
        file_path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info(f"Saved {len(proxies)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Failed to write proxies to {file_path}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and filter working HTTP/HTTPS proxies.")
    parser.add_argument("input_file", type=Path, help="Input file with proxies (IP:PORT or IP:PORT:USER:PASS)")
    parser.add_argument("output_file", type=Path, help="Output file for valid proxies")
    parser.add_argument("--max-workers", type=int, default=20, help="Number of concurrent threads")
    parser.add_argument("--test-url", type=str, default="http://httpbin.org/ip", help="Test URL to check proxies")
    parser.add_argument("--timeout", type=int, default=5, help="Request timeout in seconds")
    parser.add_argument("--https-only", action="store_true", help="Force testing only over HTTPS")
    args = parser.parse_args()

    if args.max_workers < 2:
        logging.error("Please set --max-workers to at least 2.")
        return

    proxy_list = read_proxies(args.input_file)
    if not proxy_list:
        return

    valid_proxies: List[str] = []
    start_time = time.time()

    try:
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            tasks = {
                executor.submit(check_proxy, proxy, args.test_url, args.timeout, args.https_only): proxy
                for proxy in proxy_list
            }

            for future in tqdm(as_completed(tasks), total=len(tasks), desc="Validating proxies", ncols=80):
                result = future.result()
                if result:
                    valid_proxies.append(result)

    except KeyboardInterrupt:
        logging.warning("Validation interrupted by user.")

    finally:
        write_proxies(args.output_file, valid_proxies)
        elapsed = time.time() - start_time
        logging.info(
            f"Done in {elapsed:.2f} seconds — {len(valid_proxies)}/{len(proxy_list)} proxies are valid."
        )


if __name__ == "__main__":
    main()
