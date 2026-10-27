"""User-requested, bounded web fetching.

This is not a crawler and it does not execute page scripts. A single GET is
validated before use: only HTTP(S), no URL credentials, standard ports unless
private-network mode is explicitly enabled, resolved addresses must be global
by default, redirect targets are revalidated, response bytes are capped and
only text-like content types are decoded.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, ClassVar

from . import __version__
from .errors import ToolError
from .http_tls import create_tls_context

MAX_REDIRECTS = 5
_ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "text/json",
    "application/xml",
    "text/xml",
}
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_SPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

Resolver = Callable[..., Any]
Opener = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class WebLink:
    url: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "text": self.text}


@dataclass(frozen=True, slots=True)
class WebFetchResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    title: str | None
    text: str
    links: tuple[WebLink, ...]
    size_bytes: int
    body_sha256: str
    truncated: bool


def validate_url(
    raw_url: str,
    *,
    allow_private_network: bool = False,
    resolver: Resolver | None = None,
    resolve: bool = True,
) -> ValidatedUrl:
    value = raw_url.strip()
    if not value or len(value) > 4096 or _CONTROL_RE.search(value) or "\\" in value:
        raise ToolError("URL is empty, too long, or contains control/backslash characters.")
    parsed = urllib.parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ToolError("Only http:// and https:// URLs are allowed.")
    if parsed.username or parsed.password:
        raise ToolError("URLs containing credentials are not allowed.")
    if not parsed.hostname:
        raise ToolError("URL must contain a hostname.")
    try:
        host = parsed.hostname.strip().rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ToolError("URL hostname is not valid IDNA.") from exc
    if not host or len(host) > 253:
        raise ToolError("URL hostname is invalid.")

    default_port = 443 if scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise ToolError("URL port is invalid.") from exc
    if port < 1 or port > 65535:
        raise ToolError("URL port is invalid.")
    if port != default_port and not allow_private_network:
        raise ToolError(
            "Non-standard web ports require an explicit private-network command."
        )

    if resolve:
        _validate_resolved_addresses(
            host,
            port,
            allow_private_network=allow_private_network,
            resolver=resolver or socket.getaddrinfo,
        )

    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port != default_port:
        netloc = f"{netloc}:{port}"
    normalized = urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    return ValidatedUrl(url=normalized, scheme=scheme, host=host, port=port)


def fetch_url(
    raw_url: str,
    *,
    timeout_seconds: float = 15.0,
    max_bytes: int = 2_000_000,
    allow_private_network: bool = False,
    resolver: Resolver | None = None,
    opener: Opener | None = None,
) -> WebFetchResult:
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise ToolError("Web timeout must be greater than 0 and at most 60 seconds.")
    if max_bytes < 1024 or max_bytes > 10_000_000:
        raise ToolError("Web response limit must be between 1024 and 10000000 bytes.")

    validated = validate_url(
        raw_url,
        allow_private_network=allow_private_network,
        resolver=resolver,
    )
    request = urllib.request.Request(
        validated.url,
        headers={
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "User-Agent": f"Self-Healing-PC-Agent/{__version__} (user-requested fetch)",
        },
        method="GET",
    )
    active_opener = opener or _build_opener(
        allow_private_network=allow_private_network,
        resolver=resolver,
    )

    try:
        with active_opener(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            final_url = str(response.geturl() or validated.url)
            headers = response.headers
            content_type = (
                headers.get_content_type()
                if hasattr(headers, "get_content_type")
                else str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
            )
            body = bytearray()
            truncated = False
            while True:
                chunk = response.read(min(65_536, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    truncated = True
                    del body[max_bytes:]
                    break
    except urllib.error.HTTPError as exc:
        raise ToolError(f"Web fetch failed with HTTP {exc.code} for host {validated.host}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError) or (
            "certificate verify failed" in str(reason).lower()
        ):
            raise ToolError(
                f"TLS certificate verification failed for host {validated.host}. "
                "The agent combined the system, Windows (when present) and "
                "Certifi CA stores but still could not verify the site. "
                "Certificate verification was not disabled. Check the PC "
                "date/time and any antivirus HTTPS-filtering root, then run "
                "pc-agent doctor to inspect the TLS trust summary."
            ) from exc
        reason_text = getattr(exc, "reason", exc.__class__.__name__)
        raise ToolError(
            f"Web fetch failed for host {validated.host}: {reason_text}"
        ) from exc

    final_validated = validate_url(
        final_url,
        allow_private_network=allow_private_network,
        resolver=resolver,
    )
    if status < 200 or status >= 300:
        raise ToolError(f"Web fetch failed with HTTP {status} for host {validated.host}.")
    if content_type and content_type not in _ALLOWED_CONTENT_TYPES:
        raise ToolError(
            f"Web fetch only accepts text/HTML/JSON/XML content, not {content_type}."
        )

    charset = "utf-8"
    if hasattr(headers, "get_content_charset"):
        charset = str(headers.get_content_charset() or "utf-8")
    try:
        decoded = bytes(body).decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        decoded = bytes(body).decode("utf-8", errors="replace")

    title, text, links = _extract_content(
        decoded,
        content_type=content_type,
        final_url=final_validated.url,
    )
    return WebFetchResult(
        requested_url=validated.url,
        final_url=final_validated.url,
        status=status,
        content_type=content_type or "text/plain",
        title=title,
        text=text,
        links=links,
        size_bytes=len(body),
        body_sha256=hashlib.sha256(bytes(body)).hexdigest(),
        truncated=truncated,
    )


def _validate_resolved_addresses(
    host: str,
    port: int,
    *,
    allow_private_network: bool,
    resolver: Resolver,
) -> None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if literal is not None:
        addresses.append(literal)
    else:
        try:
            results = resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ToolError(f"Could not resolve web host: {host}") from exc
        for item in results or []:
            try:
                sockaddr = item[4]
                addresses.append(ipaddress.ip_address(str(sockaddr[0])))
            except (IndexError, TypeError, ValueError):
                continue
    if not addresses:
        raise ToolError(f"Could not resolve web host: {host}")
    if not allow_private_network and any(not address.is_global for address in addresses):
        raise ToolError(
            "Private, loopback, link-local and reserved web addresses are blocked by default."
        )


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_private_network: bool, resolver: Resolver | None) -> None:
        super().__init__()
        self._allow_private_network = allow_private_network
        self._resolver = resolver
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self._redirects += 1
        if self._redirects > MAX_REDIRECTS:
            raise ToolError("Web fetch exceeded the redirect limit.")
        validated = validate_url(
            newurl,
            allow_private_network=self._allow_private_network,
            resolver=self._resolver,
        )
        return super().redirect_request(req, fp, code, msg, headers, validated.url)


def _build_opener(*, allow_private_network: bool, resolver: Resolver | None) -> Opener:
    opener = urllib.request.build_opener(
        _ValidatingRedirectHandler(
            allow_private_network=allow_private_network,
            resolver=resolver,
        ),
        urllib.request.HTTPSHandler(context=create_tls_context()),
    )
    return opener.open


class _HtmlTextParser(HTMLParser):
    _SKIP_TAGS: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "noscript", "template", "svg"}
    )
    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "address",
            "article",
            "blockquote",
            "br",
            "dd",
            "div",
            "dt",
            "figcaption",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "p",
            "pre",
            "section",
            "table",
            "tr",
        }
    )

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._parts: list[str] = []
        self.links: list[WebLink] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []

    @property
    def title(self) -> str | None:
        title = _clean_inline(" ".join(self._title_parts))
        return title[:300] or None

    @property
    def text(self) -> str:
        lines = [_clean_inline(line) for line in "".join(self._parts).splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return _BLANK_LINES_RE.sub("\n\n", cleaned).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if normalized == "title":
            self._in_title = True
        if normalized in self._BLOCK_TAGS:
            self._parts.append("\n")
        if normalized == "a" and self._skip_depth == 0:
            href = dict(attrs).get("href")
            if href:
                self._link_href = href.strip()
                self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if normalized == "title":
            self._in_title = False
        if normalized in self._BLOCK_TAGS:
            self._parts.append("\n")
        if normalized == "a" and self._link_href:
            absolute = urllib.parse.urljoin(self._base_url, self._link_href)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                text = _clean_inline(" ".join(self._link_parts))[:200]
                if len(self.links) < 100:
                    self.links.append(WebLink(url=absolute, text=text))
            self._link_href = None
            self._link_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._parts.append(data)
        if self._link_href:
            self._link_parts.append(data)


def _extract_content(
    decoded: str, *, content_type: str, final_url: str
) -> tuple[str | None, str, tuple[WebLink, ...]]:
    if "json" in content_type:
        try:
            parsed = json.loads(decoded)
            return None, json.dumps(parsed, indent=2, ensure_ascii=False), ()
        except json.JSONDecodeError:
            return None, _clean_plain_text(decoded), ()
    if content_type in {"text/html", "application/xhtml+xml", "application/xml", "text/xml"} or (
        not content_type and "<html" in decoded[:500].lower()
    ):
        parser = _HtmlTextParser(final_url)
        parser.feed(decoded)
        parser.close()
        return parser.title, parser.text, tuple(parser.links)
    return None, _clean_plain_text(decoded), ()


def _clean_plain_text(value: str) -> str:
    lines = [_clean_inline(line) for line in value.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _clean_inline(value: str) -> str:
    return _SPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()
