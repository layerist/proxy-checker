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

# Suppress only insecure-request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def read_proxies(file_path: Path) -> List[str]:
    """
    Reads proxies from a file and returns a cleaned list.
    """
    if not file_path.is_file():
        logging.error(f"Proxy list not found: {file_path}")
        return []

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        proxies = [line.strip() for line in lines if line.strip()]
        logging.info(f"Loaded {len(proxies)} proxies from {file_path}")
        return proxies
    except Exception as e:
        logging.error(f"Failed to read {file_path}: {e}")
        return []


def parse_proxy_line(line: str) -> Optional[Dict[str, str]]:
    """
    Converts a line 'ip:port' or 'ip:port:user:pass' into a dict for requests.
    """
    parts = line.split(":")
    if len(parts) not in (2, 4):
        logging.debug(f"Skipping invalid proxy format: {line}")
        return None

    if len(parts) == 2:
        ip, port = parts
        auth = ""
    else:
        ip, port, user, pw = parts
        auth = f"{user}:{pw}@"

    proxy_url = f"http://{auth}{ip}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def make_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """
    Builds a requests.Session with retry/backoff support.
    """
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


def check_proxy(
    proxy_line: str,
    session: requests.Session,
    test_url: str,
    timeout: int
) -> Optional[str]:
    """
    Returns proxy_line if it successfully connects to test_url within timeout.
    """
    proxies = parse_proxy_line(proxy_line)
    if not proxies:
        return None

    try:
        resp = session.get(test_url, proxies=proxies, timeout=timeout, verify=False)
        if resp.status_code == 200:
            origin = resp.json().get("origin", "")
            logging.debug(f"OK: {proxy_line} → {origin}")
            return proxy_line
    except requests.RequestException as e:
        logging.debug(f"Failed {proxy_line}: {e}")
    return None


def write_proxies(file_path: Path, working: List[str]) -> None:
    """
    Writes the vetted proxies to the given file.
    """
    try:
        file_path.write_text("\n".join(working), encoding="utf-8")
        logging.info(f"Wrote {len(working)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Could not write to {file_path}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter working proxies.")
    parser.add_argument("input_file", type=Path, help="Path to raw proxy list")
    parser.add_argument("output_file", type=Path, help="Path for filtered proxies")
    parser.add_argument(
        "--max-workers", type=int, default=10,
        help="Thread count (>=2)"
    )
    parser.add_argument(
        "--test-url", type=str, default="http://httpbin.org/ip",
        help="URL to test proxies against"
    )
    parser.add_argument(
        "--timeout", type=int, default=5,
        help="Seconds to wait per request"
    )
    args = parser.parse_args()

    if args.max_workers < 2:
        logging.error("max-workers must be at least 2.")
        return

    proxies = read_proxies(args.input_file)
    if not proxies:
        return

    session = make_session()
    working: List[str] = []
    start = time.time()

    try:
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {
                pool.submit(check_proxy, p, session, args.test_url, args.timeout): p
                for p in proxies
            }
            for future in tqdm(as_completed(futures), total=len(futures), desc="Checking proxies"):
                result = future.result()
                if result:
                    working.append(result)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user, shutting down threads...")
    finally:
        elapsed = time.time() - start
        write_proxies(args.output_file, working)
        logging.info(f"Done in {elapsed:.2f}s — {len(working)}/{len(proxies)} proxies OK.")


if __name__ == "__main__":
    main()
