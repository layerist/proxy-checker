#!/usr/bin/env python3
"""
Proxy Validator v10

A robust validator for large HTTP, HTTPS-to-proxy, SOCKS4, SOCKS5 and
SOCKS5h proxy lists.

Supported input forms:
    host:port
    host:port:user:password
    [IPv6]:port
    [IPv6]:port:user:password
    user:password@host:port
    user:password@[IPv6]:port
    http://host:port
    https://user:password@host:port
    socks4://host:port
    socks5://user:password@host:port
    socks5h://user:password@host:port

SOCKS support requires:
    python -m pip install "requests[socks]"
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import re
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
from typing import BinaryIO, Iterable, Iterator, Sequence, TextIO
from urllib.parse import quote, unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter


VERSION = "10.0"
DEFAULT_TEST_URLS = (
    "https://api.ipify.org?format=json",
    "https://icanhazip.com/",
    "https://ifconfig.me/ip",
)
SUPPORTED_PROTOCOLS = ("http", "https", "socks5", "socks5h", "socks4")
SOCKS_PROTOCOLS = frozenset({"socks4", "socks5", "socks5h"})
MAX_RESPONSE_BYTES = 64 * 1024
TLS = local()
STOP_EVENT = Event()
BRACKETED_AUTH_RE = re.compile(
    r"^\[(?P<host>[^\]]+)]:(?P<port>\d+):(?P<user>[^:]*):(?P<password>.*)$"
)


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
    attempts: int = 0


@dataclass(slots=True)
class RunState:
    checked: int = 0
    valid: int = 0
    submitted: int = 0
    source_exhausted: bool = False
    interrupted: bool = False


def install_signal_handlers() -> None:
    def handle_stop(signum: int, _frame: object) -> None:
        if STOP_EVENT.is_set():
            return
        print(
            f"\nReceived signal {signum}; stopping new submissions...",
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
        host = host[1:-1]
    return host


def validate_host(host: str) -> str:
    host = normalize_host(host)
    if not host or any(char.isspace() for char in host):
        raise ValueError("invalid host")
    if any(char in host for char in "/?#@"):
        raise ValueError("host contains invalid URL characters")
    return host


def validate_port(port_text: str) -> int:
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("port is not an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port outside 1..65535")
    return port


def format_host_for_url(host: str) -> str:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if parsed.version == 6 else host


def parse_host_port(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or value[end + 1 : end + 2] != ":":
            raise ValueError("invalid bracketed IPv6 format")
        host = value[1:end]
        port_text = value[end + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or not host:
            raise ValueError("missing host or port")

    return validate_host(host), validate_port(port_text)


def parse_proxy_line(line: str, default_protocols: Sequence[str]) -> ProxyCandidate:
    raw = line.strip()
    if not raw:
        raise ValueError("empty line")

    if "://" in raw:
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.lower()
            if scheme not in SUPPORTED_PROTOCOLS:
                raise ValueError(f"unsupported scheme: {scheme}")
            if parsed.hostname is None or parsed.port is None:
                raise ValueError("missing host or port")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("proxy URL must not contain path, query or fragment")
            host = validate_host(parsed.hostname)
            port = parsed.port
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"invalid proxy URL: {exc}") from exc

        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
        return ProxyCandidate(raw, host, port, username, password, (scheme,))

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

    bracketed = BRACKETED_AUTH_RE.match(raw)
    if bracketed:
        return ProxyCandidate(
            raw,
            validate_host(bracketed.group("host")),
            validate_port(bracketed.group("port")),
            bracketed.group("user") or None,
            bracketed.group("password"),
            tuple(default_protocols),
        )

    # Common host:port:user:password form. Password may contain colons.
    parts = raw.split(":")
    if len(parts) >= 4 and not raw.startswith("["):
        host = parts[0].strip()
        port_text = parts[1].strip()
        if host and port_text.isdigit():
            return ProxyCandidate(
                raw,
                validate_host(host),
                validate_port(port_text),
                parts[2] or None,
                ":".join(parts[3:]),
                tuple(default_protocols),
            )

    host, port = parse_host_port(raw)
    return ProxyCandidate(raw, host, port, None, None, tuple(default_protocols))


def canonical_proxy_url(candidate: ProxyCandidate, protocol: str) -> str:
    auth = ""
    if candidate.username is not None:
        user = quote(candidate.username, safe="")
        password = quote(candidate.password or "", safe="")
        auth = f"{user}:{password}@"
    return f"{protocol}://{auth}{format_host_for_url(candidate.host)}:{candidate.port}"


def output_proxy_value(result: CheckResult, candidate: ProxyCandidate, mode: str) -> str:
    if mode == "original" or result.protocol is None:
        return result.proxy
    return canonical_proxy_url(candidate, result.protocol)


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
    except (TimeoutError, socket.timeout):
        return False, "tcp_timeout"
    except OSError as exc:
        return False, f"tcp:{exc.__class__.__name__}"


def read_limited_response(response: requests.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=4096):
        if not chunk:
            continue
        remaining = limit - size
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if size >= limit:
            break
    return b"".join(chunks)


def extract_ip(payload_bytes: bytes) -> str | None:
    text = payload_bytes.decode("utf-8", errors="replace").strip()
    payload: object | None = None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

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


def compact_error(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


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
        return CheckResult(raw_proxy, "invalid", error=compact_error(exc))

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
            error=f"{exc.__class__.__name__}: {compact_error(exc)}",
        )

    last_status = "dead"
    last_error: str | None = None
    attempts = 0

    for protocol in candidate.protocols:
        if STOP_EVENT.is_set():
            return CheckResult(raw_proxy, "cancelled", attempts=attempts)

        proxy_url = canonical_proxy_url(candidate, protocol)
        proxies = {"http": proxy_url, "https": proxy_url}

        for retry_index in range(retries + 1):
            endpoints = list(test_urls)
            if len(endpoints) > 1:
                random.shuffle(endpoints)

            for endpoint in endpoints:
                if STOP_EVENT.is_set():
                    return CheckResult(raw_proxy, "cancelled", attempts=attempts)

                attempts += 1
                started = time.perf_counter()
                try:
                    with session.get(
                        endpoint,
                        proxies=proxies,
                        timeout=(connect_timeout, read_timeout),
                        verify=verify_tls,
                        allow_redirects=True,
                        stream=True,
                    ) as response:
                        latency = time.perf_counter() - started
                        if response.status_code != 200:
                            last_status = f"http_{response.status_code}"
                            last_error = f"endpoint returned HTTP {response.status_code}"
                            continue
                        if latency > max_latency:
                            last_status = "too_slow"
                            last_error = f"{latency:.3f}s > {max_latency:.3f}s"
                            continue

                        body = read_limited_response(response, MAX_RESPONSE_BYTES)
                        exit_ip = extract_ip(body)
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
                            attempts=attempts,
                        )
                except requests.RequestException as exc:
                    last_status = classify_request_error(exc)
                    last_error = compact_error(exc)
                except Exception as exc:
                    last_status = "unexpected_error"
                    last_error = f"{exc.__class__.__name__}: {compact_error(exc)}"

            if retry_index < retries:
                delay = retry_backoff * (2**retry_index)
                delay += random.uniform(0.0, max(0.001, delay * 0.25))
                if STOP_EVENT.wait(delay):
                    return CheckResult(raw_proxy, "cancelled", attempts=attempts)

    return CheckResult(raw_proxy, last_status, error=last_error, attempts=attempts)


def iter_input_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            value = line.strip()
            if value and not value.startswith("#"):
                yield value


def create_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(fd)
    return Path(name)


def prepare_unique_source(input_path: Path, temp_dir: Path) -> tuple[Path, int, int]:
    """Deduplicate once, while creating a restartable temporary source."""
    source_path = create_temp_path(temp_dir, ".proxy-source.", ".txt")
    seen: set[str] = set()
    duplicates = 0
    try:
        with source_path.open("w", encoding="utf-8", newline="\n") as output:
            for value in iter_input_lines(input_path):
                if value in seen:
                    duplicates += 1
                    continue
                seen.add(value)
                output.write(value + "\n")
        return source_path, len(seen), duplicates
    except BaseException:
        source_path.unlink(missing_ok=True)
        raise


def parse_protocols(value: str) -> tuple[str, ...]:
    protocols: list[str] = []
    for item in value.split(","):
        protocol = item.strip().lower()
        if not protocol:
            continue
        if protocol not in SUPPORTED_PROTOCOLS:
            raise argparse.ArgumentTypeError(
                f"unsupported protocol {protocol!r}; choose from "
                f"{', '.join(SUPPORTED_PROTOCOLS)}"
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


def validate_test_urls(parser: argparse.ArgumentParser, urls: Sequence[str]) -> tuple[str, ...]:
    validated: list[str] = []
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            parser.error(f"invalid --test-url: {url!r}")
        validated.append(url)
    return tuple(validated)


def socks_dependency_available() -> bool:
    try:
        import socks  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate large HTTP/SOCKS proxy lists.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="input proxy list")
    parser.add_argument("output", type=Path, help="output file with valid proxies")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    parser.add_argument("--workers", type=positive_int, default=200)
    parser.add_argument(
        "--inflight",
        type=non_negative_int,
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
        help="test endpoint; repeat to add multiple endpoints",
    )
    parser.add_argument("--connect-timeout", type=positive_float, default=2.5)
    parser.add_argument("--read-timeout", type=positive_float, default=5.0)
    parser.add_argument("--tcp-timeout", type=positive_float, default=1.5)
    parser.add_argument("--retries", type=non_negative_int, default=1)
    parser.add_argument("--retry-backoff", type=positive_float, default=0.15)
    parser.add_argument("--max-latency", type=positive_float, default=8.0)

    parser.add_argument("--no-tcp-check", action="store_true")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS verification for test endpoints",
    )
    parser.add_argument(
        "--no-require-ip",
        action="store_true",
        help="accept any HTTP 200 response instead of requiring a valid IP",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="shuffle unique input in memory before validation",
    )
    parser.add_argument(
        "--details",
        type=Path,
        help="TSV report for every checked proxy, including failures",
    )
    parser.add_argument("--progress-every", type=positive_int, default=250)
    parser.add_argument(
        "--flush-every",
        type=positive_int,
        default=25,
        help="flush output after this many completed checks",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="sort valid output by latency; stores valid results in memory",
    )
    parser.add_argument(
        "--output-format",
        choices=("original", "url"),
        default="original",
        help="write the original line or the successful canonical proxy URL",
    )
    parser.add_argument(
        "--user-agent",
        default=f"ProxyValidator/{VERSION}",
    )
    return parser


def atomic_replace(temp_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, destination)


def tsv_cell(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def write_detail(handle: TextIO, result: CheckResult) -> None:
    latency_ms = "" if result.latency is None else f"{result.latency * 1000:.1f}"
    fields = (
        result.proxy,
        result.status,
        result.protocol,
        latency_ms,
        result.exit_ip,
        result.endpoint,
        result.attempts,
        result.error,
    )
    handle.write("\t".join(tsv_cell(item) for item in fields) + "\n")


def print_progress(state: RunState, total: int, active: int, started: float) -> None:
    elapsed = max(time.perf_counter() - started, 0.001)
    speed = state.checked / elapsed
    remaining = max(total - state.checked, 0)
    eta = remaining / speed if speed > 0 else 0.0
    print(
        f"[{state.checked}/{total}] valid={state.valid} active={active} "
        f"speed={speed:.1f}/s eta={eta:.0f}s",
        flush=True,
    )


def process_result(
    result: CheckResult,
    *,
    args: argparse.Namespace,
    output: TextIO,
    details: TextIO | None,
    valid_results: list[CheckResult],
    stats: Counter[str],
    state: RunState,
) -> None:
    state.checked += 1
    stats[result.status] += 1
    if details is not None:
        write_detail(details, result)

    if result.status != "ok":
        return

    state.valid += 1
    if args.sort:
        valid_results.append(result)
        return

    candidate = parse_proxy_line(result.proxy, args.protocols)
    output.write(output_proxy_value(result, candidate, args.output_format) + "\n")


def run_checks(
    source: Iterable[str],
    *,
    args: argparse.Namespace,
    total: int,
    output: TextIO,
    details: TextIO | None,
    started: float,
) -> tuple[RunState, Counter[str], list[CheckResult]]:
    state = RunState()
    stats: Counter[str] = Counter()
    valid_results: list[CheckResult] = []
    inflight_limit = max(args.workers, args.inflight or args.workers * 3)
    futures: dict[Future[CheckResult], str] = {}
    source_iter = iter(source)
    completed_since_flush = 0

    check_kwargs = dict(
        protocols=args.protocols,
        test_urls=args.test_urls,
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

    executor = ThreadPoolExecutor(
        max_workers=args.workers,
        thread_name_prefix="proxy-check",
    )
    try:
        while True:
            while (
                not state.source_exhausted
                and not STOP_EVENT.is_set()
                and len(futures) < inflight_limit
            ):
                try:
                    proxy = next(source_iter)
                except StopIteration:
                    state.source_exhausted = True
                    break
                future = executor.submit(check_proxy, proxy, **check_kwargs)
                futures[future] = proxy
                state.submitted += 1

            if STOP_EVENT.is_set():
                state.interrupted = True
                for future in futures:
                    future.cancel()

            if not futures:
                break

            done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                proxy = futures.pop(future)
                if future.cancelled():
                    result = CheckResult(proxy, "cancelled")
                else:
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = CheckResult(
                            proxy,
                            "worker_error",
                            error=f"{exc.__class__.__name__}: {compact_error(exc)}",
                        )
                process_result(
                    result,
                    args=args,
                    output=output,
                    details=details,
                    valid_results=valid_results,
                    stats=stats,
                    state=state,
                )
                completed_since_flush += 1

            if completed_since_flush >= args.flush_every:
                output.flush()
                if details is not None:
                    details.flush()
                completed_since_flush = 0

            if state.checked % args.progress_every == 0 or state.checked == total:
                print_progress(state, total, len(futures), started)

            if state.interrupted and not futures:
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return state, stats, valid_results


def main() -> int:
    STOP_EVENT.clear()
    parser = build_parser()
    args = parser.parse_args()

    input_path: Path = args.input
    output_path: Path = args.output
    if not input_path.is_file():
        parser.error(f"input file does not exist: {input_path}")
    if input_path.resolve() == output_path.resolve():
        parser.error("input and output paths must be different")
    if args.details and args.details.resolve() in {input_path.resolve(), output_path.resolve()}:
        parser.error("--details must differ from input and output")

    args.test_urls = validate_test_urls(parser, args.test_urls or DEFAULT_TEST_URLS)
    if any(protocol in SOCKS_PROTOCOLS for protocol in args.protocols):
        if not socks_dependency_available():
            parser.error(
                'SOCKS protocols requested, but PySocks is unavailable; install '
                'with: python -m pip install "requests[socks]"'
            )

    if args.insecure:
        requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
            requests.packages.urllib3.exceptions.InsecureRequestWarning  # type: ignore[attr-defined]
        )

    install_signal_handlers()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("Preparing unique proxy list...", flush=True)
    source_path, total, duplicates = prepare_unique_source(input_path, output_path.parent)
    if total == 0:
        output_path.write_text("", encoding="utf-8")
        if args.details:
            args.details.parent.mkdir(parents=True, exist_ok=True)
            args.details.write_text(
                "proxy\tstatus\tprotocol\tlatency_ms\texit_ip\tendpoint\tattempts\terror\n",
                encoding="utf-8",
            )
        source_path.unlink(missing_ok=True)
        print("Input contains no proxies.")
        return 0

    print(f"Unique: {total}; duplicates skipped: {duplicates}", flush=True)

    if args.shuffle:
        source_list = list(iter_input_lines(source_path))
        random.shuffle(source_list)
        source: Iterable[str] = source_list
    else:
        source = iter_input_lines(source_path)

    temp_output = create_temp_path(
        output_path.parent,
        f".{output_path.name}.",
        ".tmp",
    )
    temp_details: Path | None = None
    if args.details:
        args.details.parent.mkdir(parents=True, exist_ok=True)
        temp_details = create_temp_path(
            args.details.parent,
            f".{args.details.name}.",
            ".tmp",
        )

    started = time.perf_counter()
    state = RunState()
    stats: Counter[str] = Counter()
    valid_results: list[CheckResult] = []

    try:
        with temp_output.open("w", encoding="utf-8", buffering=1024 * 1024) as output:
            details_handle = (
                temp_details.open("w", encoding="utf-8", buffering=1024 * 1024)
                if temp_details is not None
                else None
            )
            try:
                if details_handle is not None:
                    details_handle.write(
                        "proxy\tstatus\tprotocol\tlatency_ms\texit_ip\tendpoint\tattempts\terror\n"
                    )

                state, stats, valid_results = run_checks(
                    source,
                    args=args,
                    total=total,
                    output=output,
                    details=details_handle,
                    started=started,
                )

                if args.sort and valid_results:
                    valid_results.sort(
                        key=lambda item: item.latency if item.latency is not None else float("inf")
                    )
                    for result in valid_results:
                        candidate = parse_proxy_line(result.proxy, args.protocols)
                        output.write(
                            output_proxy_value(result, candidate, args.output_format) + "\n"
                        )

                output.flush()
                if details_handle is not None:
                    details_handle.flush()
            finally:
                if details_handle is not None:
                    details_handle.close()

        atomic_replace(temp_output, output_path)
        if temp_details is not None and args.details is not None:
            atomic_replace(temp_details, args.details)
    except BaseException:
        print(f"Partial valid results remain in: {temp_output}", file=sys.stderr)
        if temp_details is not None:
            print(f"Partial details remain in: {temp_details}", file=sys.stderr)
        raise
    finally:
        source_path.unlink(missing_ok=True)

    elapsed = max(time.perf_counter() - started, 0.001)
    print()
    print(f"Finished in {elapsed:.2f}s")
    print(f"Checked: {state.checked}/{total}")
    print(f"Valid:   {state.valid}")
    print(f"Speed:   {state.checked / elapsed:.1f} proxies/s")
    print(f"Output:  {output_path}")
    if args.details:
        print(f"Details: {args.details}")
    print("Statuses:")
    for status, count in stats.most_common():
        print(f"  {status:<20} {count}")

    return 130 if state.interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
