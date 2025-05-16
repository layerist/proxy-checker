import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict

import requests
from requests.adapters import HTTPAdapter, Retry
from tqdm import tqdm
import urllib3

# Suppress warnings for insecure requests (e.g., self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def read_proxies(file_path: Path) -> List[str]:
    """Reads proxies from file and returns a cleaned list."""
    if not file_path.is_file():
        logging.error(f"Proxy list not found: {file_path}")
        return []

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        proxies = [line.strip() for line in lines if line.strip()]
        logging.info(f"Loaded {len(proxies)} proxies from {file_path}")
        return proxies
    except Exception as e:
        logging.error(f"Failed to read proxies from {file_path}: {e}")
        return []


def parse_proxy_line(line: str) -> Optional[Dict[str, str]]:
    """Parses proxy string to dictionary for requests."""
    parts = line.strip().split(":")
    if len(parts) == 2:
        ip, port = parts
        proxy_url = f"http://{ip}:{port}"
    elif len(parts) == 4:
        ip, port, user, pw = parts
        proxy_url = f"http://{user}:{pw}@{ip}:{port}"
    else:
        logging.debug(f"Invalid proxy format skipped: {line}")
        return None
    return {"http": proxy_url, "https": proxy_url}


def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """Creates a requests session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def check_proxy(proxy_line: str, test_url: str, timeout: int) -> Optional[str]:
    """Returns proxy_line if it works with test_url, else None."""
    proxies = parse_proxy_line(proxy_line)
    if not proxies:
        return None

    session = make_session()
    try:
        response = session.get(test_url, proxies=proxies, timeout=timeout, verify=False)
        if response.status_code == 200:
            logging.debug(f"Valid proxy: {proxy_line} → {response.json().get('origin', '')}")
            return proxy_line
    except requests.RequestException as e:
        logging.debug(f"Failed proxy: {proxy_line} — {e}")
    return None


def write_proxies(file_path: Path, proxies: List[str]) -> None:
    """Saves valid proxies to file."""
    try:
        file_path.write_text("\n".join(proxies), encoding="utf-8")
        logging.info(f"Saved {len(proxies)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Could not write to {file_path}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check and filter working HTTP proxies.")
    parser.add_argument("input_file", type=Path, help="Path to input proxy list")
    parser.add_argument("output_file", type=Path, help="Path to save working proxies")
    parser.add_argument("--max-workers", type=int, default=10, help="Number of threads (>=2)")
    parser.add_argument("--test-url", type=str, default="http://httpbin.org/ip", help="URL to test proxies")
    parser.add_argument("--timeout", type=int, default=5, help="Timeout per request in seconds")
    args = parser.parse_args()

    if args.max_workers < 2:
        logging.error("Minimum value for --max-workers is 2.")
        return

    proxy_list = read_proxies(args.input_file)
    if not proxy_list:
        return

    working_proxies: List[str] = []
    start_time = time.time()

    try:
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = {
                executor.submit(check_proxy, proxy, args.test_url, args.timeout): proxy
                for proxy in proxy_list
            }

            for future in tqdm(as_completed(futures), total=len(futures), desc="Validating proxies", ncols=80):
                result = future.result()
                if result:
                    working_proxies.append(result)

    except KeyboardInterrupt:
        logging.warning("Interrupted by user. Shutting down...")

    finally:
        elapsed = time.time() - start_time
        write_proxies(args.output_file, working_proxies)
        logging.info(f"Finished in {elapsed:.2f}s — {len(working_proxies)}/{len(proxy_list)} proxies are valid.")


if __name__ == "__main__":
    main()
