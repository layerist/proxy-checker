import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def read_proxies(file_path):
    """Reads proxies from a given file and returns a list of proxies."""
    try:
        with open(file_path, 'r') as file:
            proxies = [line.strip() for line in file if line.strip()]
        logging.info(f"Loaded {len(proxies)} proxies from {file_path}")
        return proxies
    except Exception as e:
        logging.error(f"Error reading proxies from file: {e}")
        return []

def check_proxy(proxy):
    """Checks if a proxy is working by sending a request through it."""
    try:
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

        proxies = {
            'http': proxy_url,
            'https': proxy_url
        }

        response = requests.get('http://httpbin.org/ip', proxies=proxies, timeout=10)
        if response.status_code == 200:
            logging.info(f"Working proxy: {proxy}")
            return proxy
        else:
            logging.warning(f"Non-working proxy: {proxy} with status code {response.status_code}")
            return None

    except requests.RequestException as e:
        logging.warning(f"Request exception for proxy {proxy}: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error for proxy {proxy}: {e}")
        return None

def write_proxies(file_path, proxies):
    """Writes working proxies to the specified output file."""
    try:
        with open(file_path, 'w') as file:
            for proxy in proxies:
                file.write(proxy + '\n')
        logging.info(f"Successfully wrote {len(proxies)} working proxies to {file_path}")
    except Exception as e:
        logging.error(f"Error writing proxies to file: {e}")

def main(input_file, output_file):
    """Main function to load, check, and save working proxies."""
    proxies = read_proxies(input_file)
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
