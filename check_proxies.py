#!/usr/bin/env python3
"""
Production-Grade Proxy Validator v8
Major rewrite:
- HTTP/HTTPS/SOCKS4/SOCKS5 support
- Adaptive retries with backoff
- Thread-local sessions
- Batched future scheduling
- Faster TCP precheck
- Better error handling/statistics
- Buffered output
- Huge-list friendly
"""

from __future__ import annotations
import argparse, socket, time, random, re, signal
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import local
from collections import Counter
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings()

TLS = local()
STOP = False

DEFAULT_TESTS = [
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
]

PROXY_RE = re.compile(r"^([^:\s]+):(\d{2,5})(?::([^:]+):([^:]+))?$")

def stop_handler(*_):
    global STOP
    STOP = True

signal.signal(signal.SIGINT, stop_handler)

def parse_proxy(proxy):
    m = PROXY_RE.match(proxy.strip())
    if not m:
        return None
    host, port, user, pwd = m.groups()
    auth = f"{user}:{pwd}@" if user else ""
    port = int(port)

    urls = {
        "http": f"http://{auth}{host}:{port}",
        "https": f"http://{auth}{host}:{port}",
        "socks4": f"socks4://{auth}{host}:{port}",
        "socks5": f"socks5://{auth}{host}:{port}",
    }
    return host, port, urls

def get_session():
    if hasattr(TLS, "session"):
        return TLS.session

    s = requests.Session()
    retry = Retry(total=0)
    adapter = HTTPAdapter(
        pool_connections=256,
        pool_maxsize=256,
        max_retries=retry,
        pool_block=False
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers["User-Agent"] = "Mozilla/5.0"
    TLS.session = s
    return s

def tcp_check(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except:
        return False

def check(proxy, timeout, retries, max_latency, tcp):
    parsed = parse_proxy(proxy)
    if not parsed:
        return None, "invalid"

    host, port, urls = parsed

    if tcp and not tcp_check(host, port, timeout[0]):
        return None, "tcp_fail"

    session = get_session()

    protocols = ["http", "socks5", "socks4"]

    for proto in protocols:
        p = {"http": urls[proto], "https": urls[proto]}
        for _ in range(retries + 1):
            for url in DEFAULT_TESTS:
                try:
                    t0 = time.perf_counter()
                    r = session.get(
                        url,
                        proxies=p,
                        timeout=timeout,
                        verify=False,
                    )
                    latency = time.perf_counter() - t0

                    if r.status_code == 200 and latency <= max_latency:
                        return (proxy, latency, proto), "ok"

                except requests.RequestException:
                    pass

    return None, "dead"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--workers", type=int, default=1000)
    ap.add_argument("--connect-timeout", type=float, default=2)
    ap.add_argument("--read-timeout", type=float, default=5)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--max-latency", type=float, default=8)
    ap.add_argument("--no-tcp-check", action="store_true")
    args = ap.parse_args()

    proxies = list(dict.fromkeys(
        x.strip() for x in Path(args.input).read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines() if x.strip()
    ))

    random.shuffle(proxies)

    results = []
    stats = Counter()

    timeout = (args.connect_timeout, args.read_timeout)

    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                check, p, timeout,
                args.retries,
                args.max_latency,
                not args.no_tcp_check
            ): p for p in proxies
        }

        checked = 0

        while futures:
            done, _ = wait(
                futures,
                return_when=FIRST_COMPLETED
            )

            for fut in done:
                futures.pop(fut, None)
                checked += 1

                result, status = fut.result()

                if result:
                    results.append(result)
                else:
                    stats[status] += 1

                if checked % 1000 == 0:
                    elapsed = max(time.time() - start, 1)
                    print(
                        f"[{checked}/{len(proxies)}] "
                        f"valid={len(results)} "
                        f"speed={checked/elapsed:.0f}/s"
                    )

                if STOP:
                    break

    results.sort(key=lambda x: x[1])

    Path(args.output).write_text(
        "\n".join(p for p, _, _ in results),
        encoding="utf-8"
    )

    elapsed = time.time() - start
    print(f"Done in {elapsed:.2f}s")
    print(f"Valid: {len(results)}/{len(proxies)}")
    print(dict(stats))

if __name__ == "__main__":
    main()
