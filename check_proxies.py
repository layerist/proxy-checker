import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def read_proxies(file_path):
    """
    Reads proxies from a given file and returns a list of proxies.
    
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

def check_proxy(proxy, retries=2):
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

    for attempt in range(retries):
        try:
            response = requests.get('http://httpbin.org/ip', proxies=proxies, timeout=10)
            if response.status_code == 200:
                logging.info(f"Working proxy: {proxy}")
                return proxy
        except requests.RequestException as e:
            logging.warning(f"Request failed for proxy {proxy} (attempt {attempt + 1}): {e}")

    logging.info(f"Non-working proxy: {proxy}")
    return None

def write_proxies(file_path, proxies):
    """
    Writes working proxies to the specified output file.

    Args:
        file_path (str): Path to the file where working proxies will be written.
        proxies (list): List of working proxies.
    """
    try:
        with open(file_path, 'w') as file:
            file.writelines(f"{proxy}\n" for proxy in proxies)
        logging.info(f"Successfully wrote {len(proxies)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Error writing proxies to file {file_path}: {e}")

def main(input_file, output_file):
    """
    Main function to load, check, and save working proxies.

    Args:
        input_file (str): File path to input proxy list.
        output_file (str): File path to save working proxies.
    """
    proxies = read_proxies(input_file)
    if not proxies:
        logging.error("No proxies to process. Exiting.")
        return

    working_proxies = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_proxy = {executor.submit(check_proxy, proxy): proxy for proxy in proxies}

        for future in as_completed(future_to_proxy):
            proxy = future_to_proxy[future]
            try:
                result = future.result()
                if result:
                    working_proxies.append(result)
            except Exception as e:
                logging.error(f"Error processing proxy {proxy}: {e}")

    write_proxies(output_file, working_proxies)
    logging.info(f"Total working proxies: {len(working_proxies)}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check and filter working proxies.')
    parser.add_argument('input_file', help='File with list of proxies in ip:port[:username:password] format')
    parser.add_argument('output_file', help='Output file for working proxies')
    args = parser.parse_args()
    main(args.input_file, args.output_file)
