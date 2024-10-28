import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path
import time
import urllib3

# Suppress warnings from urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging with timestamps and levels
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def read_proxies(file_path):
    """
    Reads proxies from a file and returns a list of valid proxy strings.
    Args:
        file_path (str): Path to the file containing proxy addresses.
    Returns:
        list: A list of proxies read from the file.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        logging.error(f"File not found: {file_path}")
        return []

    try:
        with file_path.open('r') as file:
            proxies = [line.strip() for line in file if line.strip()]
        logging.info(f"Loaded {len(proxies)} proxies from {file_path}")
        return proxies
    except Exception as e:
        logging.error(f"Error reading proxies from file {file_path}: {e}")
        return []

def parse_proxy(proxy):
    """
    Parses a proxy string into a requests-compatible proxy dictionary.
    Args:
        proxy (str): Proxy string in format ip:port or ip:port:username:password.
    Returns:
        dict: A dictionary for use with the requests module or None if invalid format.
    """
    parts = proxy.split(':')
    if len(parts) == 2:
        ip, port = parts
        proxy_url = f"http://{ip}:{port}"
    elif len(parts) == 4:
        ip, port, username, password = parts
        proxy_url = f"http://{username}:{password}@{ip}:{port}"
    else:
        logging.warning(f"Invalid proxy format: {proxy}")
        return None

    return {'http': proxy_url, 'https': proxy_url}

def check_proxy(proxy, retries=3):
    """
    Checks if a proxy is working by sending a request through it.
    Args:
        proxy (str): Proxy string in format ip:port or ip:port:username:password.
        retries (int): Number of retry attempts for a proxy.
    Returns:
        str: The proxy string if it is working, otherwise None.
    """
    proxies = parse_proxy(proxy)
    if not proxies:
        return None

    url = 'http://httpbin.org/ip'
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, proxies=proxies, timeout=5, verify=False)
            if response.status_code == 200:
                logging.info(f"Working proxy: {proxy} (IP: {response.json()['origin']})")
                return proxy
        except requests.RequestException as e:
            logging.debug(f"Proxy {proxy} failed on attempt {attempt}/{retries}: {e}")

    logging.info(f"Non-working proxy: {proxy}")
    return None

def write_proxies(file_path, proxies):
    """
    Writes working proxies to the specified output file.
    Args:
        file_path (str): Path to the file where working proxies will be written.
        proxies (list): List of working proxies.
    """
    file_path = Path(file_path)
    try:
        with file_path.open('w') as file:
            file.writelines(f"{proxy}\n" for proxy in proxies)
        logging.info(f"Successfully wrote {len(proxies)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Error writing proxies to file {file_path}: {e}")

def main(input_file, output_file, max_workers=10):
    """
    Main function to load, check, and save working proxies.
    Args:
        input_file (str): File path to input proxy list.
        output_file (str): File path to save working proxies.
        max_workers (int): Number of threads to use for parallel proxy checking.
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
            proxy = future_to_proxy[future]
            try:
                result = future.result()
                if result:
                    working_proxies.append(result)
            except Exception as e:
                logging.error(f"Error processing proxy {proxy}: {e}")

    if working_proxies:
        write_proxies(output_file, working_proxies)
        logging.info(f"Total working proxies: {len(working_proxies)}")
    else:
        logging.info("No working proxies found.")

    logging.info(f"Finished processing in {time.time() - start_time:.2f} seconds")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check and filter working proxies.')
    parser.add_argument('input_file', type=str, help='File with list of proxies in ip:port[:username:password] format')
    parser.add_argument('output_file', type=str, help='Output file for working proxies')
    parser.add_argument('--max_workers', type=int, default=10, help='Number of threads to use for proxy checking')
    args = parser.parse_args()

    # Validate max_workers for positive integer
    if args.max_workers < 1:
        logging.error("Max workers must be a positive integer. Exiting.")
    else:
        main(args.input_file, args.output_file, args.max_workers)
