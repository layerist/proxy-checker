import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path
import time
import urllib3
from typing import List, Optional, Dict

# Suppress warnings from urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging with timestamps and levels
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def read_proxies(file_path: str) -> List[str]:
    """
    Reads proxies from a file and returns a list of valid proxy strings.
    """
    path = Path(file_path)
    if not path.is_file():
        logging.error(f"File not found: {file_path}")
        return []

    try:
        with path.open("r", encoding="utf-8") as file:
            proxies = [line.strip() for line in file if line.strip()]
        logging.info(f"Loaded {len(proxies)} proxies from {file_path}")
        return proxies
    except Exception as e:
        logging.error(f"Error reading proxies from {file_path}: {e}")
        return []

def parse_proxy(proxy: str) -> Optional[Dict[str, str]]:
    """
    Parses a proxy string into a requests-compatible proxy dictionary.
    """
    parts = proxy.split(":")
    if len(parts) == 2:
        ip, port = parts
        proxy_url = f"http://{ip}:{port}"
    elif len(parts) == 4:
        ip, port, username, password = parts
        proxy_url = f"http://{username}:{password}@{ip}:{port}"
    else:
        logging.warning(f"Invalid proxy format: {proxy}")
        return None

    return {"http": proxy_url, "https": proxy_url}

def check_proxy(proxy: str, retries: int = 3, timeout: int = 5) -> Optional[str]:
    """
    Checks if a proxy is working by sending a request through it.
    """
    proxies = parse_proxy(proxy)
    if not proxies:
        return None

    test_url = "http://httpbin.org/ip"
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(test_url, proxies=proxies, timeout=timeout, verify=False)
            if response.status_code == 200:
                logging.info(f"Working proxy: {proxy} (IP: {response.json().get('origin')})")
                return proxy
        except requests.RequestException as e:
            logging.debug(f"Proxy {proxy} failed (attempt {attempt}/{retries}): {e}")

    logging.info(f"Non-working proxy: {proxy}")
    return None

def write_proxies(file_path: str, proxies: List[str]) -> None:
    """
    Writes working proxies to the specified output file.
    """
    path = Path(file_path)
    try:
        with path.open("w", encoding="utf-8") as file:
            file.writelines(f"{proxy}\n" for proxy in proxies)
        logging.info(f"Successfully wrote {len(proxies)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Error writing proxies to file {file_path}: {e}")

def main(input_file: str, output_file: str, max_workers: int = 10) -> None:
    """
    Main function to load, check, and save working proxies.
    """
    start_time = time.time()

    proxies = read_proxies(input_file)
    if not proxies:
        logging.error("No proxies to process. Exiting.")
        return

    working_proxies = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_proxy = {executor.submit(check_proxy, proxy): proxy for proxy in proxies}

        for future in as_completed(future_to_proxy):
            try:
                result = future.result()
                if result:
                    working_proxies.append(result)
            except Exception as e:
                logging.error(f"Unexpected error: {e}")

    write_proxies(output_file, working_proxies)
    elapsed_time = time.time() - start_time
    logging.info(f"Finished processing in {elapsed_time:.2f} seconds. Total working proxies: {len(working_proxies)}")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Check and filter working proxies.")
    parser.add_argument("input_file", type=str, help="File with list of proxies in ip:port[:username:password] format")
    parser.add_argument("output_file", type=str, help="Output file for working proxies")
    parser.add_argument("--max_workers", type=int, default=10, help="Number of threads to use for proxy checking")
    args = parser.parse_args()

    if args.max_workers < 2:
        logging.error("Max workers must be a positive integer. Exiting.")
    else:
        main(args.input_file, args.output_file, args.max_workers)
