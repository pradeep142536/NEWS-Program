from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.robotparser
import urllib.request
from dataclasses import dataclass


class UnsafeURL(ValueError):
    pass


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def host_matches(host: str, allowed_hosts: tuple[str, ...] | list[str] | set[str]) -> bool:
    host = host.rstrip(".").lower()
    return any(host == allowed.lower() or host.endswith("." + allowed.lower()) for allowed in allowed_hosts)


def validate_url(url: str, allowed_hosts: tuple[str, ...] | list[str] | set[str]) -> urllib.parse.SplitResult:
    if len(url) > 2048 or any(character.isspace() or ord(character) < 32 for character in url):
        raise UnsafeURL("URL is too long or contains whitespace/control characters")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeURL("Only HTTPS URLs without embedded credentials are allowed")
    if parsed.port not in (None, 443):
        raise UnsafeURL("Only the standard HTTPS port is allowed")
    if not host_matches(parsed.hostname, allowed_hosts):
        raise UnsafeURL("The URL host is not in the configured trusted-domain list")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and (not address.is_global or address.is_multicast or address.is_reserved):
        raise UnsafeURL("Private and non-public IP addresses are not allowed")
    return parsed


def _resolve_public(host: str, port: int) -> None:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"DNS lookup failed for {host}: {exc}") from exc
    if not records:
        raise FetchError(f"DNS lookup returned no addresses for {host}")
    for record in records:
        address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise UnsafeURL(f"Host {host} resolved to a non-public address")


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class _RestrictedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...] | list[str] | set[str]):
        super().__init__()
        self.allowed_hosts = tuple(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.allowed_hosts)
        _resolve_public(urllib.parse.urlsplit(newurl).hostname or "", 443)
        if code not in (301, 302, 303, 307, 308):
            raise FetchError(f"Unsupported redirect status: {code}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(
    url: str,
    allowed_hosts: tuple[str, ...] | list[str] | set[str],
    *,
    max_bytes: int,
    timeout: float = 15,
    headers: dict[str, str] | None = None,
) -> FetchResponse:
    parsed = validate_url(url, allowed_hosts)
    _resolve_public(parsed.hostname or "", parsed.port or 443)
    request_headers = {"User-Agent": "PersonalFutureIntelligenceBot/0.1 (+RSS reader; contact configured by operator)"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    opener = urllib.request.build_opener(_RestrictedRedirects(allowed_hosts))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResponse(url, 304, {k.lower(): v for k, v in exc.headers.items()}, b"")
        raise FetchError(f"HTTP {exc.code} from {parsed.hostname}", status_code=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Request failed for {parsed.hostname}: {exc}") from exc
    with response:
        response_headers = {k.lower(): v for k, v in response.headers.items()}
        final_url = response.geturl()
        validate_url(final_url, allowed_hosts)
        raw_length = response_headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    raise FetchError("Response exceeds configured size limit")
            except ValueError:
                pass
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise FetchError("Response exceeds configured size limit")
        return FetchResponse(final_url, response.status, response_headers, body)


_ROBOTS_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}


def robots_allowed(url: str, allowed_hosts: tuple[str, ...] | list[str] | set[str]) -> tuple[bool, str]:
    """Fail closed when robots policy cannot be read; cache successful files for one hour."""
    parsed = validate_url(url, allowed_hosts)
    origin = f"https://{parsed.netloc}"
    cached = _ROBOTS_CACHE.get(origin)
    if cached and cached[0] > time.time():
        parser = cached[1]
    else:
        robots_url = origin + "/robots.txt"
        try:
            response = fetch_url(robots_url, allowed_hosts, max_bytes=256_000)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
                _ROBOTS_CACHE[origin] = (time.time() + 3600, parser)
            else:
                return False, f"Could not read robots.txt safely ({exc})"
        else:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
            _ROBOTS_CACHE[origin] = (time.time() + 3600, parser)
    allowed = parser.can_fetch("PersonalFutureIntelligenceBot", url)
    return allowed, "Allowed by robots.txt" if allowed else "Blocked by robots.txt"
