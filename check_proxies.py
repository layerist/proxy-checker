#!/usr/bin/env python3
"""
Proxy Validator v12

Robust, bounded-concurrency validator for large HTTP, HTTPS-to-proxy,
SOCKS4, SOCKS5 and SOCKS5h proxy lists.

Highlights:
- Correct URL defaults and strict endpoint validation.
- Explicit-scheme proxies are tested only with that scheme.
- Bounded in-flight futures and graceful interruption.
- Per-candidate requests.Session prevents unbounded ProxyManager growth.
- Deterministic failures such as HTTP 407 do not waste retries.
- Optional hard cap on HTTP attempts per proxy.
- Memory or SQLite-backed input deduplication.
- Atomic, optionally durable output replacement.
- Optional TSV diagnostics with credential redaction.
- More defensive proxy parsing, IPv6 handling and error classification.
- Streaming response reads with a bounded body size.
- Per-attempt latency budget is also applied to connect/read timeouts.
- Retry backoff is interruptible.
- Useful exit codes and final status accounting.

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
import contextlib
import ipaddress
import json
import os
import random
import re
import signal
import socket
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Iterable, Iterator, Sequence, TextIO
from urllib.parse import quote, unquote, urlsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter


VERSION = "12.0"

DEFAULT_TEST_URLS = (
    "https://api.ipify.org?format=json",
    "https://icanhazip.com/",
    "https://ifconfig.me/ip",
)

SUPPORTED_PROTOCOLS = ("http", "https", "socks5", "socks5h", "socks4")
SOCKS_PROTOCOLS = frozenset({"socks4", "socks5", "socks5h"})

MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_DEDUP_MEMORY_THRESHOLD_MB = 128

STOP_EVENT = Event()

BRACKETED_AUTH_RE = re.compile(
    r"^\[(?P<host>[^\]]+)\]:(?P<port>\d+):(?P<user>[^:]*):(?P<password>.*)$"
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
    ttfb: float | None = None
    exit_ip: str | None = None
    endpoint: str | None = None
    canonical_url: str | None = None
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
    if not host:
        raise ValueError("empty host")
    if any(char.isspace() for char in host):
        raise ValueError("host contains whitespace")
    if any(char in host for char in "/?#@"):
        raise ValueError("host contains invalid URL characters")
    if "\x00" in host:
        raise ValueError("host contains NUL byte")

    # If it looks like an IP literal, validate it. Hostnames are left to the
    # resolver because IDNA and private naming conventions vary by environment.
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(host))

    return host


def validate_port(port_text: str | int) -> int:
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
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
    if not value:
        raise ValueError("empty address")

    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("missing closing bracket in IPv6 address")
        if value[end + 1 : end + 2] != ":":
            raise ValueError("missing port after bracketed IPv6 address")
        host = value[1:end]
        port_text = value[end + 2 :]
        if ":" in port_text:
            raise ValueError("unexpected extra fields after port")
    else:
        # Unbracketed IPv6 is intentionally rejected because host:port is
        # ambiguous. Brackets make the input unambiguous and URL-safe.
        if value.count(":") > 1:
            raise ValueError("IPv6 addresses must be enclosed in brackets")
        host, separator, port_text = value.rpartition(":")
        if not separator or not host or not port_text:
            raise ValueError("missing host or port")

    return validate_host(host), validate_port(port_text)


def parse_proxy_line(line: str, default_protocols: Sequence[str]) -> ProxyCandidate:
    raw = line.strip()
    if not raw:
        raise ValueError("empty line")

    if "://" in raw:
        try:
            parsed = urlsplit(raw)
        except Exception as exc:
            raise ValueError(f"invalid proxy URL: {exc}") from exc

        scheme = parsed.scheme.lower()
        if scheme not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"unsupported scheme: {scheme}")

        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"invalid proxy URL: {exc}") from exc

        if hostname is None or port is None:
            raise ValueError("missing host or port")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("proxy URL must not contain path, query or fragment")

        host = validate_host(hostname)
        port = validate_port(port)
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None

        if password is not None and username is None:
            raise ValueError("password supplied without username")

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
            username,
            password,
            tuple(default_protocols),
        )

    bracketed = BRACKETED_AUTH_RE.match(raw)
    if bracketed:
        return ProxyCandidate(
            raw,
            validate_host(bracketed.group("host")),
            validate_port(bracketed.group("port")),
            bracketed.group("user"),
            bracketed.group("password"),
            tuple(default_protocols),
        )

    # Common host:port:user:password form. Password may contain colons.
    # Unbracketed IPv6 is not accepted here because it is ambiguous.
    parts = raw.split(":")
    if len(parts) >= 4 and not raw.startswith("["):
        host = parts[0].strip()
        port_text = parts[1].strip()
        if host and port_text.isdigit():
            return ProxyCandidate(
                raw,
                validate_host(host),
                validate_port(port_text),
                parts[2],
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

    return (
        f"{protocol}://{auth}"
        f"{format_host_for_url(candidate.host)}:{candidate.port}"
    )


def output_proxy_value(result: CheckResult, mode: str) -> str:
    if mode == "original" or not result.canonical_url:
        return result.proxy
    return result.canonical_url


def redact_proxy_value(value: str) -> str:
    """Best-effort credential masking for logs/reports."""
    try:
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.username is None:
                return value

            host = parsed.hostname or ""
            host = format_host_for_url(host)
            port = f":{parsed.port}" if parsed.port is not None else ""
            return f"{parsed.scheme}://***:***@{host}{port}"

        if "@" in value:
            _auth, address = value.rsplit("@", 1)
            return f"***:***@{address}"

        bracketed = BRACKETED_AUTH_RE.match(value)
        if bracketed:
            return (
                f"[{bracketed.group('host')}]:{bracketed.group('port')}:***:***"
            )

        parts = value.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            return f"{parts[0]}:{parts[1]}:***:***"
    except Exception:
        pass

    return value


def create_session(user_agent: str) -> requests.Session:
    """
    Create one session per proxy candidate.

    requests/urllib3 caches a ProxyManager per proxy URL in an HTTPAdapter.
    A session shared across a huge proxy list therefore grows with the number
    of proxies seen. Per-candidate sessions bound that cache while preserving
    connection reuse across retries/endpoints for the same candidate.
    """
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
        pool_connections=4,
        pool_maxsize=4,
        max_retries=0,
        pool_block=False,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
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


def read_limited_response(
    response: requests.Response,
    limit: int,
    *,
    deadline: float | None = None,
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0

    for chunk in response.iter_content(chunk_size=4096):
        if deadline is not None and time.perf_counter() > deadline:
            return b"".join(chunks), True

        if not chunk:
            continue

        remaining = limit - size
        if remaining <= 0:
            break

        piece = chunk[:remaining]
        chunks.append(piece)
        size += len(piece)

        if size >= limit:
            break

    return b"".join(chunks), False


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

    # Covers common plain-text endpoints and "a.b.c.d, e.f.g.h" origin forms.
    candidates.extend(
        part.strip()
        for part in text.replace("\r", "\n").replace("\n", ",").split(",")
    )

    for candidate in candidates:
        candidate = candidate.strip().strip('"').strip("'")
        if not candidate:
            continue

        # Some services can return a label followed by an IP.
        if " " in candidate:
            candidate = candidate.rsplit(" ", 1)[-1]

        # Be tolerant of a trailing punctuation mark in text responses.
        candidate = candidate.strip("[](){}<>;")

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
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "too_many_redirects"
    return exc.__class__.__name__.lower()


def compact_error(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def bounded_request_timeout(
    *,
    connect_timeout: float,
    read_timeout: float,
    max_latency: float,
) -> tuple[float, float]:
    # requests has no true total wall-clock timeout. Bounding both socket
    # phases by the latency limit prevents a single phase from exceeding the
    # accepted total by a large margin.
    return (
        min(connect_timeout, max_latency),
        min(read_timeout, max_latency),
    )


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
    max_attempts: int,
    use_tcp_check: bool,
    verify_tls: bool,
    require_ip: bool,
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
        session = create_session(user_agent)
    except Exception as exc:
        return CheckResult(
            raw_proxy,
            "session_error",
            error=f"{exc.__class__.__name__}: {compact_error(exc)}",
        )

    last_status = "dead"
    last_error: str | None = None
    attempts = 0

    rng = random.Random(os.urandom(16))
    request_timeout = bounded_request_timeout(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_latency=max_latency,
    )

    try:
        for protocol in candidate.protocols:
            if STOP_EVENT.is_set():
                return CheckResult(raw_proxy, "cancelled", attempts=attempts)

            proxy_url = canonical_proxy_url(candidate, protocol)
            proxy_mapping = {"http": proxy_url, "https": proxy_url}

            skip_protocol = False

            for retry_index in range(retries + 1):
                endpoints = list(test_urls)
                if len(endpoints) > 1:
                    rng.shuffle(endpoints)

                for endpoint in endpoints:
                    if STOP_EVENT.is_set():
                        return CheckResult(
                            raw_proxy,
                            "cancelled",
                            attempts=attempts,
                        )

                    if max_attempts and attempts >= max_attempts:
                        return CheckResult(
                            raw_proxy,
                            last_status,
                            error=last_error or "maximum attempts reached",
                            attempts=attempts,
                        )

                    attempts += 1
                    started = time.perf_counter()

                    try:
                        with session.get(
                            endpoint,
                            proxies=proxy_mapping,
                            timeout=request_timeout,
                            verify=verify_tls,
                            allow_redirects=True,
                            stream=True,
                        ) as response:
                            headers_received = time.perf_counter()
                            ttfb = headers_received - started

                            if response.status_code != 200:
                                if response.status_code == 407:
                                    last_status = "proxy_auth_required"
                                    last_error = "proxy returned HTTP 407"
                                    # Authentication failure is deterministic
                                    # for this protocol/credential tuple.
                                    skip_protocol = True
                                    break

                                last_status = f"http_{response.status_code}"
                                last_error = (
                                    f"endpoint returned HTTP {response.status_code}"
                                )
                                continue

                            if ttfb > max_latency:
                                last_status = "too_slow"
                                last_error = (
                                    f"TTFB {ttfb:.3f}s > {max_latency:.3f}s"
                                )
                                continue

                            body, deadline_exceeded = read_limited_response(
                                response,
                                MAX_RESPONSE_BYTES,
                                deadline=started + max_latency,
                            )
                            latency = time.perf_counter() - started

                            if deadline_exceeded or latency > max_latency:
                                last_status = "too_slow"
                                last_error = (
                                    f"total {latency:.3f}s > {max_latency:.3f}s"
                                )
                                continue

                            exit_ip = extract_ip(body)
                            if require_ip and exit_ip is None:
                                last_status = "invalid_response"
                                last_error = (
                                    "HTTP 200 received but no valid IP was found"
                                )
                                continue

                            return CheckResult(
                                proxy=raw_proxy,
                                status="ok",
                                protocol=protocol,
                                latency=latency,
                                ttfb=ttfb,
                                exit_ip=exit_ip,
                                endpoint=endpoint,
                                canonical_url=proxy_url,
                                attempts=attempts,
                            )

                    except requests.RequestException as exc:
                        last_status = classify_request_error(exc)
                        last_error = compact_error(exc)
                    except Exception as exc:
                        last_status = "unexpected_error"
                        last_error = (
                            f"{exc.__class__.__name__}: {compact_error(exc)}"
                        )

                if skip_protocol:
                    break

                if retry_index < retries:
                    base_delay = retry_backoff * (2**retry_index)
                    delay = base_delay + rng.uniform(
                        0.0,
                        max(0.001, base_delay * 0.25),
                    )
                    if STOP_EVENT.wait(delay):
                        return CheckResult(
                            raw_proxy,
                            "cancelled",
                            attempts=attempts,
                        )
    finally:
        session.close()

    return CheckResult(
        raw_proxy,
        last_status,
        error=last_error,
        attempts=attempts,
    )


def iter_input_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            value = line.strip()
            if value and not value.startswith("#"):
                yield value


def create_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(fd)
    return Path(name)


def deduplicate_memory(
    input_path: Path,
    source_path: Path,
) -> tuple[int, int]:
    seen: set[str] = set()
    duplicates = 0

    with source_path.open("w", encoding="utf-8", newline="\n") as output:
        for value in iter_input_lines(input_path):
            if value in seen:
                duplicates += 1
                continue
            seen.add(value)
            output.write(value + "\n")

    return len(seen), duplicates


def deduplicate_sqlite(
    input_path: Path,
    source_path: Path,
    temp_dir: Path,
) -> tuple[int, int]:
    """
    Deduplicate without retaining the full unique set in Python memory.

    SQLite preserves uniqueness with a primary key while the source file keeps
    first-seen order. This is slower than a set but much safer for huge lists.
    """
    db_path = create_temp_path(temp_dir, ".proxy-dedup.", ".sqlite3")
    total = 0
    duplicates = 0

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("CREATE TABLE seen (value TEXT PRIMARY KEY) WITHOUT ROWID")

            with source_path.open("w", encoding="utf-8", newline="\n") as output:
                conn.execute("BEGIN")
                pending = 0

                for value in iter_input_lines(input_path):
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO seen(value) VALUES (?)",
                        (value,),
                    )

                    if cursor.rowcount == 1:
                        total += 1
                        output.write(value + "\n")
                    else:
                        duplicates += 1

                    pending += 1
                    if pending >= 10_000:
                        conn.commit()
                        conn.execute("BEGIN")
                        pending = 0

                conn.commit()
        finally:
            conn.close()
    finally:
        db_path.unlink(missing_ok=True)

    return total, duplicates


def prepare_unique_source(
    input_path: Path,
    temp_dir: Path,
    *,
    mode: str,
    memory_threshold_mb: int,
) -> tuple[Path, int, int, str]:
    source_path = create_temp_path(temp_dir, ".proxy-source.", ".txt")

    if mode == "auto":
        threshold_bytes = memory_threshold_mb * 1024 * 1024
        effective_mode = (
            "sqlite"
            if input_path.stat().st_size >= threshold_bytes
            else "memory"
        )
    else:
        effective_mode = mode

    try:
        if effective_mode == "sqlite":
            total, duplicates = deduplicate_sqlite(
                input_path,
                source_path,
                temp_dir,
            )
        else:
            total, duplicates = deduplicate_memory(input_path, source_path)

        return source_path, total, duplicates, effective_mode
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
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def validate_test_urls(
    parser: argparse.ArgumentParser,
    urls: Sequence[str],
) -> tuple[str, ...]:
    validated: list[str] = []

    for url in urls:
        parsed = urlsplit(url)

        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            parser.error(f"invalid --test-url: {url!r}")
        if parsed.username is not None or parsed.password is not None:
            parser.error(f"--test-url must not contain credentials: {url!r}")

        validated.append(url)

    if not validated:
        parser.error("at least one test URL is required")

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
    parser.add_argument(
        "--max-attempts",
        type=non_negative_int,
        default=6,
        help=(
            "maximum HTTP attempts per proxy across protocols/endpoints/retries; "
            "0 means unlimited"
        ),
    )
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
        help="accept HTTP 200 even if no valid IP can be extracted",
    )

    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="shuffle unique input in memory before validation",
    )
    parser.add_argument(
        "--dedup-mode",
        choices=("auto", "memory", "sqlite"),
        default="auto",
        help="deduplication backend",
    )
    parser.add_argument(
        "--dedup-memory-threshold-mb",
        type=positive_int,
        default=DEFAULT_DEDUP_MEMORY_THRESHOLD_MB,
        help="in auto mode, use SQLite when input file is at least this large",
    )

    parser.add_argument(
        "--details",
        type=Path,
        help="TSV report for every completed proxy, including failures",
    )
    parser.add_argument(
        "--redact-details",
        action="store_true",
        help="mask proxy credentials in the TSV details file",
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
        help="write original input or successful canonical proxy URL",
    )
    parser.add_argument(
        "--durable-output",
        action="store_true",
        help="fsync temporary output files and destination directories",
    )
    parser.add_argument(
        "--user-agent",
        default=f"ProxyValidator/{VERSION}",
    )

    return parser


def fsync_file(handle: TextIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return

    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_replace(
    temp_path: Path,
    destination: Path,
    *,
    durable: bool,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, destination)

    if durable:
        fsync_directory(destination.parent)


def tsv_cell(value: object | None) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("\t", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def write_detail(
    handle: TextIO,
    result: CheckResult,
    *,
    redact_credentials: bool,
) -> None:
    latency_ms = (
        ""
        if result.latency is None
        else f"{result.latency * 1000:.1f}"
    )
    ttfb_ms = (
        ""
        if result.ttfb is None
        else f"{result.ttfb * 1000:.1f}"
    )

    proxy_value = (
        redact_proxy_value(result.proxy)
        if redact_credentials
        else result.proxy
    )

    canonical_url = result.canonical_url
    if redact_credentials and canonical_url:
        canonical_url = redact_proxy_value(canonical_url)

    fields = (
        proxy_value,
        result.status,
        result.protocol,
        latency_ms,
        ttfb_ms,
        result.exit_ip,
        result.endpoint,
        canonical_url,
        result.attempts,
        result.error,
    )
    handle.write("\t".join(tsv_cell(item) for item in fields) + "\n")


def print_progress(
    state: RunState,
    total: int,
    active: int,
    started: float,
) -> None:
    elapsed = max(time.perf_counter() - started, 0.001)
    speed = state.checked / elapsed
    remaining = max(total - state.checked, 0)
    eta = remaining / speed if speed > 0 else 0.0

    print(
        f"[{state.checked}/{total}] "
        f"valid={state.valid} "
        f"active={active} "
        f"speed={speed:.1f}/s "
        f"eta={eta:.0f}s",
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
        write_detail(
            details,
            result,
            redact_credentials=args.redact_details,
        )

    if result.status != "ok":
        return

    state.valid += 1

    if args.sort:
        valid_results.append(result)
        return

    output.write(output_proxy_value(result, args.output_format) + "\n")


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

    inflight_limit = max(
        args.workers,
        args.inflight or args.workers * 3,
    )

    futures: dict[Future[CheckResult], str] = {}
    source_iter = iter(source)
    completed_since_flush = 0
    next_progress = args.progress_every

    check_kwargs = dict(
        protocols=args.protocols,
        test_urls=args.test_urls,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        tcp_timeout=args.tcp_timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        max_latency=args.max_latency,
        max_attempts=args.max_attempts,
        use_tcp_check=not args.no_tcp_check,
        verify_tls=not args.insecure,
        require_ip=not args.no_require_ip,
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

                future = executor.submit(
                    check_proxy,
                    proxy,
                    **check_kwargs,
                )
                futures[future] = proxy
                state.submitted += 1

            if STOP_EVENT.is_set() and not state.interrupted:
                state.interrupted = True
                for future in futures:
                    future.cancel()

            if not futures:
                break

            done, _ = wait(
                futures,
                timeout=0.25,
                return_when=FIRST_COMPLETED,
            )
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
                            error=(
                                f"{exc.__class__.__name__}: "
                                f"{compact_error(exc)}"
                            ),
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

            if state.checked >= next_progress or state.checked == total:
                print_progress(
                    state,
                    total,
                    len(futures),
                    started,
                )
                while next_progress <= state.checked:
                    next_progress += args.progress_every

            if state.interrupted and not futures:
                break
    finally:
        # Running requests cannot be forcefully killed by ThreadPoolExecutor.
        # Their bounded socket timeouts ensure shutdown remains finite.
        executor.shutdown(wait=True, cancel_futures=True)

    return state, stats, valid_results


def paths_conflict(
    input_path: Path,
    output_path: Path,
    details_path: Path | None,
) -> str | None:
    input_resolved = input_path.resolve()
    output_resolved = output_path.resolve()

    if input_resolved == output_resolved:
        return "input and output paths must be different"

    if details_path is not None:
        details_resolved = details_path.resolve()
        if details_resolved in {input_resolved, output_resolved}:
            return "--details must differ from input and output"

    return None


def main() -> int:
    STOP_EVENT.clear()

    parser = build_parser()
    args = parser.parse_args()

    input_path: Path = args.input
    output_path: Path = args.output

    if not input_path.is_file():
        parser.error(f"input file does not exist: {input_path}")

    conflict = paths_conflict(input_path, output_path, args.details)
    if conflict:
        parser.error(conflict)

    args.test_urls = validate_test_urls(
        parser,
        args.test_urls or DEFAULT_TEST_URLS,
    )

    if any(protocol in SOCKS_PROTOCOLS for protocol in args.protocols):
        if not socks_dependency_available():
            parser.error(
                'SOCKS protocols requested, but PySocks is unavailable; '
                'install with: python -m pip install "requests[socks]"'
            )

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    install_signal_handlers()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("Preparing unique proxy list...", flush=True)
    source_path, total, duplicates, dedup_mode = prepare_unique_source(
        input_path,
        output_path.parent,
        mode=args.dedup_mode,
        memory_threshold_mb=args.dedup_memory_threshold_mb,
    )

    if total == 0:
        try:
            output_path.write_text("", encoding="utf-8")

            if args.details:
                args.details.parent.mkdir(parents=True, exist_ok=True)
                args.details.write_text(
                    "proxy\tstatus\tprotocol\tlatency_ms\tttfb_ms\t"
                    "exit_ip\tendpoint\tcanonical_url\tattempts\terror\n",
                    encoding="utf-8",
                )
        finally:
            source_path.unlink(missing_ok=True)

        print("Input contains no proxies.")
        return 0

    print(
        f"Unique: {total}; duplicates skipped: {duplicates}; "
        f"dedup={dedup_mode}",
        flush=True,
    )

    if args.shuffle:
        source_list = list(iter_input_lines(source_path))
        random.SystemRandom().shuffle(source_list)
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
        with temp_output.open(
            "w",
            encoding="utf-8",
            buffering=1024 * 1024,
            newline="\n",
        ) as output:
            details_handle = (
                temp_details.open(
                    "w",
                    encoding="utf-8",
                    buffering=1024 * 1024,
                    newline="\n",
                )
                if temp_details is not None
                else None
            )

            try:
                if details_handle is not None:
                    details_handle.write(
                        "proxy\tstatus\tprotocol\tlatency_ms\tttfb_ms\t"
                        "exit_ip\tendpoint\tcanonical_url\tattempts\terror\n"
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
                        key=lambda item: (
                            item.latency
                            if item.latency is not None
                            else float("inf")
                        )
                    )
                    for result in valid_results:
                        output.write(
                            output_proxy_value(
                                result,
                                args.output_format,
                            )
                            + "\n"
                        )

                if args.durable_output:
                    fsync_file(output)
                    if details_handle is not None:
                        fsync_file(details_handle)
                else:
                    output.flush()
                    if details_handle is not None:
                        details_handle.flush()

            finally:
                if details_handle is not None:
                    details_handle.close()

        atomic_replace(
            temp_output,
            output_path,
            durable=args.durable_output,
        )

        if temp_details is not None and args.details is not None:
            atomic_replace(
                temp_details,
                args.details,
                durable=args.durable_output,
            )

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
    finally:
        source_path.unlink(missing_ok=True)

    elapsed = max(time.perf_counter() - started, 0.001)

    print()
    print(f"Finished in {elapsed:.2f}s")
    print(f"Checked: {state.checked}/{total}")
    print(f"Submitted: {state.submitted}/{total}")
    print(f"Valid:   {state.valid}")
    print(f"Speed:   {state.checked / elapsed:.1f} proxies/s")
    print(f"Output:  {output_path}")

    if args.details:
        print(f"Details: {args.details}")

    print("Statuses:")
    for status, count in stats.most_common():
        print(f"  {status:<22} {count}")

    return 130 if state.interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
