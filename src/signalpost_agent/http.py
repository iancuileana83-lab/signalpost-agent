"""Polite, safe HTTP client: per-host rate limits, robots.txt, SSRF guard, retries, size cap."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = "SignalpostAgent/0.1 (+https://github.com/iancuileana83-lab/signalpost-agent; research crawler, honours robots.txt)"
MAX_REDIRECTS = 5


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Ops:
    """Per-company operation counters."""
    requests: int = 0
    bytes: int = 0
    busy_s: float = 0.0  # active processing time attributed to this company
    deadline: float = 0.0  # monotonic time after which no further requests are made for this company (0 = none)

    def runtime_ms(self) -> int:
        return int(self.busy_s * 1000)


@dataclass
class Response:
    url: str
    final_url: str
    status: int  # HTTP status, or 0 for transport error, -1 for blocked by policy
    body: bytes = b""
    content_type: str = ""
    retrieved_at: str = ""
    sha256: str = ""
    error: str | None = None
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def blocked(self) -> bool:
        return self.status == -1

    @property
    def text(self) -> str:
        if not self.body:
            return ""
        charset = "utf-8"
        if "charset=" in self.content_type.lower():
            charset = self.content_type.lower().split("charset=")[1].split(";")[0].strip() or "utf-8"
        else:
            head = self.body[:2048].lower()
            marker = head.find(b"charset=")
            if marker >= 0:
                charset = head[marker + 8:marker + 40].split(b'"')[0].split(b"'")[0].split(b";")[0].split(b">")[0].strip().decode("ascii", "ignore") or "utf-8"
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


def safe_url(url: str) -> str:
    """IDNA-encode the host and percent-encode non-ASCII path/query so urllib never raises on IDN or Nordic characters."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = host + (f":{parts.port}" if parts.port else "")
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="=&%+:/?,;@!$'()*~-._")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


class Fetcher:
    def __init__(self, *, timeout: float = 10.0, max_bytes: int = 1_500_000, retries: int = 2,
                 default_interval: float = 1.0, host_intervals: dict[str, float] | None = None,
                 respect_robots: bool = True):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.default_interval = default_interval
        self.host_intervals = host_intervals or {}
        self.respect_robots = respect_robots
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | str] = {}
        self._robots_lock = threading.Lock()
        self._dns: dict[str, bool] = {}
        self._opener = urllib.request.build_opener(_NoRedirect)

    # ----- policy helpers -------------------------------------------------
    def _interval(self, host: str) -> float:
        for suffix, value in self.host_intervals.items():
            if host == suffix or host.endswith("." + suffix):
                return value
        return self.default_interval

    def _wait_turn(self, host: str) -> None:
        interval = self._interval(host)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def host_is_public(self, host: str) -> bool:
        """SSRF guard: every resolved address must be a global unicast address."""
        with self._lock:
            cached = self._dns.get(host)
        if cached is not None:
            return cached
        ok = False
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            addrs = {info[4][0] for info in infos}
            ok = bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)
        except (socket.gaierror, ValueError, UnicodeError, OSError):
            ok = False
        with self._lock:
            self._dns[host] = ok
        return ok

    def _robots_allows(self, url: str, ops: Ops) -> tuple[bool, str]:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            entry = self._robots.get(origin)
        if entry is None:
            robots_url = origin + "/robots.txt"
            response = self._get(robots_url, ops, accept="text/plain,*/*;q=0.5", max_bytes=300_000)
            if response.status == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
                entry = parser
            elif response.status in (401, 403):
                entry = "disallow_all"  # RFC 9309: access denied to robots.txt -> treat as disallowed
            elif 400 <= response.status < 500:
                entry = "allow_all"
            elif response.status == 0:
                entry = "down:" + (response.error or "unreachable")
            else:
                entry = "unreachable"
            with self._robots_lock:
                self._robots[origin] = entry
        if entry == "allow_all":
            return True, ""
        if isinstance(entry, str) and entry.startswith("down:"):
            return True, entry  # transport failure: the main request will report the real error
        if entry in ("disallow_all", "unreachable"):
            return False, f"robots.txt {entry}"
        assert isinstance(entry, urllib.robotparser.RobotFileParser)
        if entry.can_fetch("SignalpostAgent", url) and entry.can_fetch("*", url):
            return True, ""
        return False, "disallowed by robots.txt"

    def robots_sitemaps(self, url: str) -> list[str]:
        parts = urllib.parse.urlsplit(url)
        entry = self._robots.get(f"{parts.scheme}://{parts.netloc}")
        if isinstance(entry, urllib.robotparser.RobotFileParser):
            return list(entry.site_maps() or [])
        return []

    # ----- fetching -------------------------------------------------------
    def get(self, url: str, ops: Ops, *, robots: bool = True, accept: str = "text/html,application/xhtml+xml,*/*;q=0.5",
            max_bytes: int | None = None, headers: dict[str, str] | None = None, data: bytes | None = None) -> Response:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return Response(url, url, -1, retrieved_at=utc_now(), error="unsupported URL scheme")
        if robots and self.respect_robots:
            allowed, why = self._robots_allows(url, ops)
            if allowed and why.startswith("down:"):
                return Response(url, url, 0, retrieved_at=utc_now(), error="site unreachable: " + why[5:])
            if not allowed:
                return Response(url, url, -1, retrieved_at=utc_now(), error=why)
        return self._get(url, ops, accept=accept, max_bytes=max_bytes, headers=headers, data=data)

    def _get(self, url: str, ops: Ops, *, accept: str, max_bytes: int | None = None,
             headers: dict[str, str] | None = None, data: bytes | None = None) -> Response:
        limit = max_bytes or self.max_bytes
        if ops.deadline and time.monotonic() > ops.deadline:
            return Response(url, url, 0, retrieved_at=utc_now(), error="per-company time budget exhausted")
        current = url
        redirects: list[str] = []
        last_error: str | None = None
        for _hop in range(MAX_REDIRECTS + 1):
            current = safe_url(current)
            parts = urllib.parse.urlsplit(current)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("http", "https") or not host:
                return Response(url, current, -1, retrieved_at=utc_now(), error="unsupported URL", redirects=redirects)
            if not self.host_is_public(host):
                return Response(url, current, 0, retrieved_at=utc_now(), error="dns: host does not resolve to a public address", redirects=redirects)
            for attempt in range(self.retries + 1):
                self._wait_turn(host)
                request = urllib.request.Request(current, data=data, headers={
                    "User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "nb,no;q=0.9,en;q=0.5", **(headers or {})})
                ops.requests += 1
                try:
                    with self._opener.open(request, timeout=self.timeout) as resp:
                        body = resp.read(limit + 1)
                        ops.bytes += len(body)
                        return Response(url, current, resp.status, body[:limit], resp.headers.get("Content-Type", ""),
                                        utc_now(), hashlib.sha256(body[:limit]).hexdigest(), None, redirects)
                except urllib.error.HTTPError as error:
                    status = error.code
                    if status in (301, 302, 303, 307, 308):
                        location = error.headers.get("Location")
                        if not location:
                            return Response(url, current, status, retrieved_at=utc_now(), error="redirect without Location", redirects=redirects)
                        current = urllib.parse.urljoin(current, location)
                        redirects.append(current)
                        break  # follow the redirect (outer loop)
                    if status in (429, 502, 503, 504) and attempt < self.retries:
                        retry_after = error.headers.get("Retry-After", "")
                        time.sleep(min(float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1), 20))
                        continue
                    body = b""
                    try:
                        body = error.read(20_000)
                    except Exception:  # noqa: BLE001
                        pass
                    return Response(url, current, status, body, error.headers.get("Content-Type", "") if error.headers else "",
                                    utc_now(), hashlib.sha256(body).hexdigest(), f"HTTP {status}", redirects)
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                    last_error = f"{type(error).__name__}: {error}"
                    timed_out = isinstance(error, TimeoutError) or "timed out" in str(error).lower()
                    if attempt < self.retries and not timed_out:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    return Response(url, current, 0, retrieved_at=utc_now(), error=last_error, redirects=redirects)
            else:
                return Response(url, current, 0, retrieved_at=utc_now(), error=last_error or "fetch failed", redirects=redirects)
            continue
        return Response(url, current, 0, retrieved_at=utc_now(), error="too many redirects", redirects=redirects)
