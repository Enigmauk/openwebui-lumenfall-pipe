"""SSRF-aware, bounded streaming downloader for Lumenfall result URLs."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import stat
import uuid
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from .models import PermanentVideoError


MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MIME_TYPES = {"video/mp4", "video/webm"}


class _HttpxURLRedactor(logging.Filter):
    """Keep third-party HTTP request logs from recording signed URL material."""

    _URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._URL.sub("[URL redacted]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


for _http_logger_name in ("httpx", "httpcore"):
    _http_logger = logging.getLogger(_http_logger_name)
    if not any(isinstance(item, _HttpxURLRedactor) for item in _http_logger.filters):
        _http_logger.addFilter(_HttpxURLRedactor())


class DownloadError(PermanentVideoError):
    """A sanitized download rejection. Its message never contains a URL."""


@dataclass(frozen=True)
class DownloadedArtifact:
    artifact_path: str
    actual_bytes: int


async def _system_resolve(host: str, port: int) -> list[str]:
    try:
        rows = await asyncio.to_thread(
            socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
        )
    except OSError:
        raise DownloadError("DOWNLOAD_DNS_FAILED") from None
    return list(dict.fromkeys(row[4][0].split("%", 1)[0] for row in rows))


class SecureResultDownloader:
    """Stream a result into a private worker directory, then atomically publish it.

    DNS is checked before every HTTP request, including every redirect hop. The
    standard HTTP connector resolves the hostname again when it connects, so
    validation and connection are not pinned to one DNS answer; that residual
    rebinding/TOCTOU window is documented in the architecture note.
    """

    def __init__(self, artifact_directory: str | Path, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Callable[[str, int], Iterable[str]] | None = None,
                 max_bytes: int = MAX_DOWNLOAD_BYTES,
                 maximum_redirects: int = 3):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("Invalid worker download limit.")
        if maximum_redirects < 0 or maximum_redirects > 5:
            raise ValueError("Invalid worker redirect limit.")
        self.artifact_directory = Path(artifact_directory)
        self._resolver = resolver or _system_resolve
        self.max_bytes = max_bytes
        self.maximum_redirects = maximum_redirects
        self._prepare_directory()
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    def _prepare_directory(self) -> None:
        try:
            self.artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = self.artifact_directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise DownloadError("ARTIFACT_DIRECTORY_UNSAFE")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise DownloadError("ARTIFACT_DIRECTORY_UNSAFE")
            os.chmod(self.artifact_directory, 0o700)
        except DownloadError:
            raise
        except OSError:
            raise DownloadError("ARTIFACT_DIRECTORY_UNAVAILABLE") from None

    @staticmethod
    def _validate_url(url: str) -> tuple[str, int]:
        if (not isinstance(url, str) or len(url) > 8192 or "\\" in url
                or any(ord(c) < 32 for c in url)):
            raise DownloadError("DOWNLOAD_URL_INVALID")
        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
        except ValueError:
            raise DownloadError("DOWNLOAD_URL_INVALID") from None
        if (parsed.scheme.lower() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or not 1 <= port <= 65535):
            raise DownloadError("DOWNLOAD_URL_INVALID")
        host = parsed.hostname.rstrip(".")
        if not host or "%" in host:
            raise DownloadError("DOWNLOAD_URL_INVALID")
        return host, port

    async def _validate_destination(self, url: str) -> None:
        host, port = self._validate_url(url)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                resolved = self._resolver(host, port)
                if isawaitable(resolved):
                    resolved = await resolved
            except DownloadError:
                raise
            except Exception:
                raise DownloadError("DOWNLOAD_DNS_FAILED") from None
            try:
                addresses = list(resolved)
            except Exception:
                raise DownloadError("DOWNLOAD_DNS_FAILED") from None
            if not addresses:
                raise DownloadError("DOWNLOAD_DNS_FAILED")
            if len(addresses) > 32:
                raise DownloadError("DOWNLOAD_DNS_UNSAFE")
            parsed_addresses = []
            for item in addresses:
                try:
                    parsed_addresses.append(ipaddress.ip_address(item))
                except (ValueError, TypeError):
                    raise DownloadError("DOWNLOAD_DNS_UNSAFE") from None
            # Reject the whole answer set if even one address is not globally
            # routable. Never pick the public subset of a mixed answer.
            if not all(self._is_public(address) for address in parsed_addresses):
                raise DownloadError("DOWNLOAD_DNS_UNSAFE")
            return
        if not self._is_public(address):
            raise DownloadError("DOWNLOAD_DNS_UNSAFE")

    @staticmethod
    def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_global
        return address.is_global

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        raw = response.headers.get("content-length")
        if raw is None:
            return None
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
            raise DownloadError("DOWNLOAD_LENGTH_INVALID")
        return int(raw)

    @staticmethod
    def _mime(value: str | None) -> str | None:
        return value.split(";", 1)[0].strip().lower() if value else None

    async def download(self, *, job_id: str, url: str, mime: str,
                       expected_bytes: int | None = None) -> DownloadedArtifact:
        try:
            canonical_id = str(uuid.UUID(job_id))
        except (ValueError, TypeError, AttributeError):
            raise DownloadError("ARTIFACT_ID_INVALID") from None
        expected_mime = self._mime(mime)
        if expected_mime not in _MIME_TYPES:
            raise DownloadError("DOWNLOAD_MIME_UNSUPPORTED")
        if expected_bytes is not None:
            if (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int)
                    or expected_bytes <= 0):
                raise DownloadError("DOWNLOAD_SIZE_INVALID")
            if expected_bytes > self.max_bytes:
                raise DownloadError("DOWNLOAD_TOO_LARGE")

        extension = ".mp4" if expected_mime == "video/mp4" else ".webm"
        final = self.artifact_directory / f"{canonical_id}{extension}"
        partial = self.artifact_directory / f".{canonical_id}.part"
        partial.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        descriptor = None
        try:
            descriptor = os.open(
                partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                count, prefix = await self._stream_to_file(
                    url=url, output=output, expected_mime=expected_mime,
                    expected_bytes=expected_bytes,
                )
                if count == 0 or not self._valid_signature(prefix, expected_mime):
                    raise DownloadError("DOWNLOAD_SIGNATURE_INVALID")
                if expected_bytes is not None and count != expected_bytes:
                    raise DownloadError("DOWNLOAD_SIZE_MISMATCH")
                output.flush()
                os.fsync(output.fileno())
            os.replace(partial, final)
            directory_fd = os.open(self.artifact_directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return DownloadedArtifact(final.name, count)
        except DownloadError:
            raise
        except httpx.HTTPError:
            raise DownloadError("DOWNLOAD_TRANSPORT_FAILED") from None
        except OSError:
            raise DownloadError("ARTIFACT_WRITE_FAILED") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            partial.unlink(missing_ok=True)

    async def _stream_to_file(self, *, url: str, output, expected_mime: str,
                              expected_bytes: int | None) -> tuple[int, bytes]:
        current_url = url
        redirects = 0
        total = 0
        prefix = bytearray()
        while True:
            await self._validate_destination(current_url)
            try:
                async with self._client.stream("GET", current_url, headers={"Accept": expected_mime}) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        if redirects >= self.maximum_redirects:
                            raise DownloadError("DOWNLOAD_REDIRECT_LIMIT")
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError("DOWNLOAD_REDIRECT_INVALID")
                        current_url = urljoin(current_url, location)
                        redirects += 1
                        continue
                    if response.status_code != 200:
                        raise DownloadError("DOWNLOAD_HTTP_REJECTED")
                    response_mime = self._mime(response.headers.get("content-type"))
                    if response_mime not in _MIME_TYPES or response_mime != expected_mime:
                        raise DownloadError("DOWNLOAD_MIME_MISMATCH")
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding not in {"", "identity"}:
                        raise DownloadError("DOWNLOAD_ENCODING_UNSUPPORTED")
                    declared = self._content_length(response)
                    if declared is not None and declared > self.max_bytes:
                        raise DownloadError("DOWNLOAD_TOO_LARGE")
                    if expected_bytes is not None and declared is not None and declared != expected_bytes:
                        raise DownloadError("DOWNLOAD_SIZE_MISMATCH")
                    async for chunk in response.aiter_bytes(chunk_size=_CHUNK_BYTES):
                        if total + len(chunk) > self.max_bytes:
                            raise DownloadError("DOWNLOAD_TOO_LARGE")
                        output.write(chunk)
                        total += len(chunk)
                        if len(prefix) < 32:
                            prefix.extend(chunk[:32 - len(prefix)])
                    if declared is not None and total != declared:
                        raise DownloadError("DOWNLOAD_SIZE_MISMATCH")
                    break
            except DownloadError:
                raise
            except httpx.HTTPError:
                raise DownloadError("DOWNLOAD_TRANSPORT_FAILED") from None
        return total, bytes(prefix)

    @staticmethod
    def _valid_signature(prefix: bytes, mime: str) -> bool:
        if mime == "video/webm":
            return prefix.startswith(b"\x1a\x45\xdf\xa3")
        if mime == "video/mp4" and len(prefix) >= 16:
            box_size = int.from_bytes(prefix[:4], "big")
            return box_size >= 16 and prefix[4:8] == b"ftyp"
        return False

    async def aclose(self) -> None:
        await self._client.aclose()
