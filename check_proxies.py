#!/usr/bin/env python3
"""
Production-Grade Proxy Validator v9

Features:
- HTTP / HTTPS CONNECT / SOCKS4 / SOCKS5 support
- Supported input formats:
    host:port
    host:port:user:password
    user:password@host:port
    http://host:port
    http://user:password@host:port
    socks4://host:port
    socks5://user:password@host:port
    socks5h://user:password@host:port
- IPv4, hostnames and bracketed IPv6
- Thread-local requests.Session objects
- Bounded in-flight futures for huge proxy lists
- Optional TCP precheck
- Multiple test endpoints with response validation
- Retry with exponential backoff and jitter
- Graceful Ctrl+C shutdown
- Immediate buffered output of valid proxies
- Optional detailed TSV output
- Atomic final output replacement
- Detailed status/error statistics

SOCKS support:
    pip install "requests[socks]"
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import signal
import socket
import sys
import tempfile
import time

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Event, local
from typing import Iterable, Iterator, Sequence
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter


DEFAULT_TEST_URLS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
    "https://icanhazip.com/",
)

SUPPORTED_PROTOCOLS = ("http", "socks5", "socks5h", "socks4")
TLS = local()
STOP_EVENT = Event()


@dataclass(frozen=True, slots=True)
class ProxyCandidate:
    original: str
    host: str
    port: int
    username: str | None
    password: str | None
    protocols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CheckResult:
    proxy: str
    status: str
    protocol: str | None = None
    latency: float | None = None
    exit_ip: str | None = None
    endpoint: str | None = None
    error: str | None = None


def install_signal_handlers() -> None:
    def handle_stop(signum: int, _frame: object) -> None:
        if not STOP_EVENT.is_set():
            print(
                f"\nReceived signal {signum}; stopping new work and saving results...",
                file=sys.stderr,
                flush=True,
            )
            STOP_EVENT.set()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)


def normalize_host(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def format_host_for_url(host: str) -> str:
    try:
        parsed = ipaddress.ip_address(host)
        if parsed.version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


def parse_host_port(value: str) -> tuple[str, int]:
    value = value.strip()

    if value.startswith("["):
        end = value.find("]")
        if end == -1 or end + 1 >= len(value) or value[end + 1] != ":":
            raise ValueError("invalid bracketed IPv6 format")
        host = value[1:end]
        port_text = value[end + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or not host:
            raise ValueError("missing host or port")

    host = normalize_host(host)
    if not host or any(char.isspace() for char in host):
        raise ValueError("invalid host")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("port is not an integer") from exc

    if not 1 <= port <= 65535:
        raise ValueError("port outside 1..65535")

    return host, port


def parse_proxy_line(
    line: str,
    default_protocols: Sequence[str],
) -> ProxyCandidate:
    raw = line.strip()
    if not raw:
        raise ValueError("empty line")

    scheme: str | None = None
    username: str | None = None
    password: str | None = None

    if "://" in raw:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()

        if scheme == "https":
            # An HTTPS proxy in requests is still configured with an https:// URL.
            # Keep the scheme so users can explicitly test TLS-to-proxy setups.
            pass
        elif scheme not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"unsupported scheme: {scheme}")

        if parsed.hostname is None or parsed.port is None:
            raise ValueError("missing host or port")

        host = normalize_host(parsed.hostname)
        port = parsed.port
        username = parsed.username
        password = parsed.password

        protocols = (scheme,)
        return ProxyCandidate(raw, host, port, username, password, protocols)

    # user:password@host:port
    if "@" in raw:
        auth, address = raw.rsplit("@", 1)
        if ":" not in auth:
            raise ValueError("authentication must be user:password")
        username, password = auth.split(":", 1)
        host, port = parse_host_port(address)
        return ProxyCandidate(
            raw,
            host,
            port,
            username or None,
            password,
            tuple(default_protocols),
        )

    # host:port:user:password
    # This intentionally handles the common four-part IPv4/hostname format.
    parts = raw.split(":")
    if len(parts) >= 4 and not raw.startswith("["):
        host = parts[0].strip()
        port_text = parts[1].strip()
        username = parts[2]
        password = ":".join(parts[3:])

        if host and port_text.isdigit():
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError("port outside 1..65535")
            return ProxyCandidate(
                raw,
                host,
                port,
                username or None,
                password,
                tuple(default_protocols),
            )

    host, port = parse_host_port(raw)
    return ProxyCandidate(raw, host, port, None, None, tuple(default_protocols))


def canonical_proxy_url(candidate: ProxyCandidate, protocol: str) -> str:
    host = format_host_for_url(candidate.host)
    auth = ""

    if candidate.username is not None:
        user = quote(candidate.username, safe="")
        password = quote(candidate.password or "", safe="")
        auth = f"{user}:{password}@"

    return f"{protocol}://{auth}{host}:{candidate.port}"


def get_session(pool_size: int, user_agent: str) -> requests.Session:
    session = getattr(TLS, "session", None)
    if session is not None:
        return session

    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
        }
    )

    adapter = HTTPAdapter(
        pool_connections=max(8, pool_size),
        pool_maxsize=max(8, pool_size),
        max_retries=0,
        pool_block=False,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    TLS.session = session
    return session


def tcp_check(host: str, port: int, timeout: float) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except socket.gaierror as exc:
        return False, f"dns:{exc.__class__.__name__}"
    except TimeoutError:
        return False, "tcp_timeout"
    except OSError as exc:
        return False, f"tcp:{exc.__class__.__name__}"


def extract_ip(response: requests.Response) -> str | None:
    text = response.text.strip()

    try:
        payload = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        payload = None

    candidates: list[str] = []

    if isinstance(payload, dict):
        for key in ("ip", "origin", "query"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.extend(part.strip() for part in value.split(","))
    elif isinstance(payload, str):
        candidates.append(payload.strip())

    candidates.extend(part.strip() for part in text.replace("\n", ",").split(","))

    for candidate in candidates:
        candidate = candidate.strip().strip('"')
        if not candidate:
            continue

        # Some services can return "IP: 1.2.3.4".
        if " " in candidate:
            candidate = candidate.rsplit(" ", 1)[-1]

        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue

    return None


def classify_request_error(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl_error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    return exc.__class__.__name__.lower()


def check_proxy(
    raw_proxy: str,
    *,
    protocols: Sequence[str],
    test_urls: Sequence[str],
    connect_timeout: float,
    read_timeout: float,
    tcp_timeout: float,
    retries: int,
    retry_backoff: float,
    max_latency: float,
    use_tcp_check: bool,
    verify_tls: bool,
    require_ip: bool,
    pool_size: int,
    user_agent: str,
) -> CheckResult:
    if STOP_EVENT.is_set():
        return CheckResult(raw_proxy, "cancelled")

    try:
        candidate = parse_proxy_line(raw_proxy, protocols)
    except (ValueError, TypeError) as exc:
        return CheckResult(raw_proxy, "invalid", error=str(exc))

    if use_tcp_check:
        tcp_ok, tcp_error = tcp_check(candidate.host, candidate.port, tcp_timeout)
        if not tcp_ok:
            return CheckResult(raw_proxy, "tcp_fail", error=tcp_error)

    try:
        session = get_session(pool_size, user_agent)
    except Exception as exc:
        return CheckResult(
            raw_proxy,
            "session_error",
            error=f"{exc.__class__.__name__}: {exc}",
        )

    last_status = "dead"
    last_error: str | None = None

    for protocol in candidate.protocols:
        if STOP_EVENT.is_set():
            return CheckResult(raw_proxy, "cancelled")

        proxy_url = canonical_proxy_url(candidate, protocol)
        proxy_mapping = {
            "http": proxy_url,
            "https": proxy_url,
        }

        for attempt in range(retries + 1):
            if STOP_EVENT.is_set():
                return CheckResult(raw_proxy, "cancelled")

            endpoints = list(test_urls)
            if len(endpoints) > 1:
                random.shuffle(endpoints)

            for endpoint in endpoints:
                started = time.perf_counter()

                try:
                    response = session.get(
                        endpoint,
                        proxies=proxy_mapping,
                        timeout=(connect_timeout, read_timeout),
                        verify=verify_tls,
                        allow_redirects=True,
                        stream=False,
                    )
                    latency = time.perf_counter() - started

                    if response.status_code != 200:
                        last_status = f"http_{response.status_code}"
                        last_error = f"endpoint returned HTTP {response.status_code}"
                        continue

                    if latency > max_latency:
                        last_status = "too_slow"
                        last_error = f"{latency:.3f}s > {max_latency:.3f}s"
                        continue

                    exit_ip = extract_ip(response)
                    if require_ip and exit_ip is None:
                        last_status = "invalid_response"
                        last_error = "HTTP 200 received but no valid IP was found"
                        continue

                    return CheckResult(
                        proxy=raw_proxy,
                        status="ok",
                        protocol=protocol,
                        latency=latency,
                        exit_ip=exit_ip,
                        endpoint=endpoint,
                    )

                except requests.RequestException as exc:
                    last_status = classify_request_error(exc)
                    last_error = str(exc)
                except Exception as exc:
                    last_status = "unexpected_error"
                    last_error = f"{exc.__class__.__name__}: {exc}"

            if attempt < retries:
                delay = retry_backoff * (2**attempt)
                delay += random.uniform(0.0, max(0.001, delay * 0.25))
                STOP_EVENT.wait(delay)

    return CheckResult(
        proxy=raw_proxy,
        status=last_status,
        error=last_error,
    )


def iter_unique_lines(path: Path) -> Iterator[str]:
    seen: set[str] = set()

    with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
        for line in handle:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if value in seen:
                continue
            seen.add(value)
            yield value


def count_unique_lines(path: Path) -> int:
    return sum(1 for _ in iter_unique_lines(path))


def parse_protocols(value: str) -> tuple[str, ...]:
    protocols: list[str] = []

    for item in value.split(","):
        protocol = item.strip().lower()
        if not protocol:
            continue
        if protocol not in SUPPORTED_PROTOCOLS:
            raise argparse.ArgumentTypeError(
                f"unsupported protocol {protocol!r}; "
                f"choose from {', '.join(SUPPORTED_PROTOCOLS)}"
            )
        if protocol not in protocols:
            protocols.append(protocol)

    if not protocols:
        raise argparse.ArgumentTypeError("at least one protocol is required")

    return tuple(protocols)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate large HTTP/SOCKS proxy lists.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="input proxy list")
    parser.add_argument("output", type=Path, help="output file with valid proxies")

    parser.add_argument("--workers", type=positive_int, default=300)
    parser.add_argument(
        "--inflight",
        type=positive_int,
        default=0,
        help="maximum scheduled futures; 0 means workers * 3",
    )
    parser.add_argument(
        "--protocols",
        type=parse_protocols,
        default=parse_protocols("http,socks5,socks4"),
        help="protocol order for entries without an explicit scheme",
    )
    parser.add_argument(
        "--test-url",
        action="append",
        dest="test_urls",
        help="test endpoint; repeat the option to add multiple endpoints",
    )

    parser.add_argument("--connect-timeout", type=positive_float, default=2.0)
    parser.add_argument("--read-timeout", type=positive_float, default=5.0)
    parser.add_argument("--tcp-timeout", type=positive_float, default=1.5)
    parser.add_argument("--retries", type=non_negative_int, default=1)
    parser.add_argument("--retry-backoff", type=positive_float, default=0.15)
    parser.add_argument("--max-latency", type=positive_float, default=8.0)

    parser.add_argument("--no-tcp-check", action="store_true")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification for test endpoints",
    )
    parser.add_argument(
        "--no-require-ip",
        action="store_true",
        help="accept any HTTP 200 response instead of requiring a valid IP",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="shuffle input before validation; loads the unique list into memory",
    )
    parser.add_argument(
        "--details",
        type=Path,
        help="optional TSV file with proxy, protocol, latency, exit IP and endpoint",
    )
    parser.add_argument(
        "--progress-every",
        type=positive_int,
        default=500,
    )
    parser.add_argument(
        "--flush-every",
        type=positive_int,
        default=25,
        help="flush output files after this many valid results",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="sort final valid proxies by latency; uses additional memory",
    )
    parser.add_argument(
        "--user-agent",
        default="ProxyValidator/9.0 (+https://example.invalid)",
    )
    return parser


def atomic_replace(temp_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, destination)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    input_path: Path = args.input
    output_path: Path = args.output

    if not input_path.is_file():
        parser.error(f"input file does not exist: {input_path}")

    if input_path.resolve() == output_path.resolve():
        parser.error("input and output paths must be different")

    test_urls = tuple(args.test_urls or DEFAULT_TEST_URLS)
    inflight_limit = args.inflight or args.workers * 3
    inflight_limit = max(args.workers, inflight_limit)

    install_signal_handlers()

    print("Scanning unique proxies...", flush=True)
    total = count_unique_lines(input_path)
    if total == 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        print("Input contains no proxies.")
        return 0

    source: Iterable[str]
    if args.shuffle:
        shuffled = list(iter_unique_lines(input_path))
        random.shuffle(shuffled)
        source = shuffled
    else:
        source = iter_unique_lines(input_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = Path(
        tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
        )[1]
    )

    temp_details: Path | None = None
    if args.details:
        args.details.parent.mkdir(parents=True, exist_ok=True)
        temp_details = Path(
            tempfile.mkstemp(
                prefix=f".{args.details.name}.",
                suffix=".tmp",
                dir=args.details.parent,
            )[1]
        )

    started = time.perf_counter()
    checked = 0
    valid = 0
    flushed_valid = 0
    stats: Counter[str] = Counter()
    valid_results: list[CheckResult] = []

    future_to_proxy: dict[Future[CheckResult], str] = {}
    source_iter = iter(source)
    source_exhausted = False

    check_kwargs = dict(
        protocols=args.protocols,
        test_urls=test_urls,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        tcp_timeout=args.tcp_timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        max_latency=args.max_latency,
        use_tcp_check=not args.no_tcp_check,
        verify_tls=not args.insecure,
        require_ip=not args.no_require_ip,
        pool_size=max(8, min(64, args.workers)),
        user_agent=args.user_agent,
    )

    try:
        with temp_output.open("w", encoding="utf-8", buffering=1024 * 1024) as output:
            details_handle = (
                temp_details.open("w", encoding="utf-8", buffering=1024 * 1024)
                if temp_details is not None
                else None
            )

            try:
                if details_handle:
                    details_handle.write(
                        "proxy\tprotocol\tlatency_ms\texit_ip\tendpoint\n"
                    )

                with ThreadPoolExecutor(
                    max_workers=args.workers,
                    thread_name_prefix="proxy-check",
                ) as executor:
                    while not STOP_EVENT.is_set():
                        while (
                            not source_exhausted
                            and len(future_to_proxy) < inflight_limit
                            and not STOP_EVENT.is_set()
                        ):
                            try:
                                proxy = next(source_iter)
                            except StopIteration:
                                source_exhausted = True
                                break

                            future = executor.submit(
                                check_proxy,
                                proxy,
                                **check_kwargs,
                            )
                            future_to_proxy[future] = proxy

                        if not future_to_proxy:
                            break

                        done, _ = wait(
                            future_to_proxy,
                            timeout=0.5,
                            return_when=FIRST_COMPLETED,
                        )

                        if not done:
                            continue

                        for future in done:
                            proxy = future_to_proxy.pop(future)
                            checked += 1

                            try:
                                result = future.result()
                            except Exception as exc:
                                result = CheckResult(
                                    proxy,
                                    "worker_error",
                                    error=f"{exc.__class__.__name__}: {exc}",
                                )

                            stats[result.status] += 1

                            if result.status == "ok":
                                valid += 1
                                if args.sort:
                                    valid_results.append(result)
                                else:
                                    output.write(result.proxy + "\n")

                                if details_handle:
                                    details_handle.write(
                                        f"{result.proxy}\t"
                                        f"{result.protocol or ''}\t"
                                        f"{(result.latency or 0.0) * 1000:.1f}\t"
                                        f"{result.exit_ip or ''}\t"
                                        f"{result.endpoint or ''}\n"
                                    )

                                flushed_valid += 1
                                if flushed_valid >= args.flush_every:
                                    output.flush()
                                    if details_handle:
                                        details_handle.flush()
                                    flushed_valid = 0

                            if (
                                checked % args.progress_every == 0
                                or checked == total
                            ):
                                elapsed = max(
                                    time.perf_counter() - started,
                                    0.001,
                                )
                                print(
                                    f"[{checked}/{total}] "
                                    f"valid={valid} "
                                    f"active={len(future_to_proxy)} "
                                    f"speed={checked / elapsed:.1f}/s",
                                    flush=True,
                                )

                    if STOP_EVENT.is_set():
                        for future in future_to_proxy:
                            future.cancel()

                if args.sort and valid_results:
                    valid_results.sort(
                        key=lambda item: (
                            item.latency
                            if item.latency is not None
                            else float("inf")
                        )
                    )
                    for result in valid_results:
                        output.write(result.proxy + "\n")

                output.flush()
                if details_handle:
                    details_handle.flush()

            finally:
                if details_handle:
                    details_handle.close()

        atomic_replace(temp_output, output_path)
        if temp_details is not None and args.details is not None:
            atomic_replace(temp_details, args.details)

    except BaseException:
        print(
            f"Partial valid results remain in: {temp_output}",
            file=sys.stderr,
        )
        if temp_details is not None:
            print(
                f"Partial details remain in: {temp_details}",
                file=sys.stderr,
            )
        raise

    elapsed = max(time.perf_counter() - started, 0.001)

    print()
    print(f"Finished in {elapsed:.2f}s")
    print(f"Checked: {checked}/{total}")
    print(f"Valid:   {valid}")
    print(f"Speed:   {checked / elapsed:.1f} proxies/s")
    print(f"Output:  {output_path}")

    if args.details:
        print(f"Details: {args.details}")

    print("Statuses:")
    for status, count in stats.most_common():
        print(f"  {status:<20} {count}")

    return 130 if STOP_EVENT.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
