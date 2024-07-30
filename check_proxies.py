import requests
from concurrent.futures import ThreadPoolExecutor
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def read_proxies(file_path):
    with open(file_path, 'r') as file:
        proxies = [line.strip() for line in file if line.strip()]
    return proxies

def check_proxy(proxy):
    parts = proxy.split(':')
    if len(parts) == 2:
        ip, port = parts
        username = password = None
    elif len(parts) == 4:
        ip, port, username, password = parts
    else:
        logging.warning(f"Invalid proxy format: {proxy}")
        return None

    if username and password:
        proxy_url = f"http://{username}:{password}@{ip}:{port}"
    else:
        proxy_url = f"http://{ip}:{port}"

    proxies = {
        'http': proxy_url,
        'https': proxy_url
    }

    try:
        response = requests.get('http://httpbin.org/ip', proxies=proxies, timeout=10)
        if response.status_code == 200:
            logging.info(f"Working proxy: {proxy}")
            return proxy
        else:
            logging.warning(f"Non-working proxy: {proxy} with status code {response.status_code}")
    except requests.RequestException as e:
        logging.warning(f"Exception for proxy {proxy}: {e}")
    
    return None

def write_proxies(file_path, proxies):
    with open(file_path, 'w') as file:
        for proxy in proxies:
            file.write(proxy + '\n')

def main(input_file, output_file):
    proxies = read_proxies(input_file)
    with ThreadPoolExecutor(max_workers=20) as executor:
        working_proxies = list(executor.map(check_proxy, proxies))
    working_proxies = [proxy for proxy in working_proxies if proxy]
    write_proxies(output_file, working_proxies)
    logging.info(f"Total working proxies: {len(working_proxies)}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check and filter working proxies.')
    parser.add_argument('input_file', help='File with list of proxies in ip:port[:username:password] format')
    parser.add_argument('output_file', help='Output file for working proxies')
    args = parser.parse_args()
    main(args.input_file, args.output_file)
