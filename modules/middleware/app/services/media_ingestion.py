import hashlib
import ipaddress
import logging
import os
import socket
import tempfile
from dataclasses import dataclass, asdict
from typing import Any, List
from urllib.parse import urlparse

import requests
from PIL import Image

from .enrichment_contracts import ProductSource, MediaIngestionResult
from .error_codes import ErrorCode

logger = logging.getLogger(__name__)

try:
    import imghdr  # py<3.13
except Exception:  # pragma: no cover
    imghdr = None

_ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


@dataclass
class IngestedImage:
    source_url: str
    local_path: str
    sha256: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    position: int
    status: str
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MediaIngestionService:
    def __init__(self, download_dir: str, max_image_size: int = 10 * 1024 * 1024, timeout: int = 15, max_redirects: int = 3):
        self.download_dir = download_dir
        self.max_image_size = max_image_size
        self.timeout = timeout
        self.max_redirects = max_redirects
        try:
            os.makedirs(self.download_dir, exist_ok=True)
        except Exception:
            # Read-only or missing bind mount: fall back to a writable temp dir
            # so media ingestion never crashes the whole middleware at startup.
            fallback = os.path.join(tempfile.gettempdir(), "omni-media")
            try:
                os.makedirs(fallback, exist_ok=True)
            except Exception:
                pass
            self.download_dir = fallback

    def ingest(self, product: ProductSource) -> MediaIngestionResult:
        result = self.ingest_urls(getattr(product, "images", None) or [], sku=getattr(product, "sku", None), job_id=None)
        return MediaIngestionResult(cover_url=result.get("cover_url"), image_urls=result.get("image_urls", []), errors=result.get("errors", []))

    def ingest_urls(self, image_urls: list[Any], *, sku: str | None = None, job_id: str | None = None) -> dict[str, Any]:
        cover_url = None
        out_urls: List[str] = []
        assets: List[dict[str, Any]] = []
        errors: List[str] = []
        seen_hashes: set[str] = set()
        position = 0

        for raw in image_urls or []:
            image_url = str((raw or {}).get("url") if isinstance(raw, dict) else raw or "").strip()
            if not image_url:
                continue
            position += 1
            try:
                asset = self._download_one(image_url, position=position, sku=sku, job_id=job_id)
                if asset.status in {"ok", "duplicate"}:
                    if asset.sha256 and asset.sha256 in seen_hashes:
                        errors.append(ErrorCode.WARN_IMAGE_DEDUPED.value)
                        continue
                    if asset.sha256:
                        seen_hashes.add(asset.sha256)
                    assets.append(asset.to_dict())
                    out_urls.append(image_url)
                    if cover_url is None:
                        cover_url = image_url
                else:
                    if asset.error_code:
                        errors.append(asset.error_code)
                    logger.warning("media_ingest_failed", extra={"job_id": job_id, "sku": sku, "image_url": image_url, "sha256": asset.sha256, "error_code": asset.error_code})
            except Exception as exc:
                errors.append(ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)
                logger.warning("media_ingest_exception", extra={"job_id": job_id, "sku": sku, "image_url": image_url, "error": str(exc)})

        # dedupe assets by sha, preserve first position
        deduped: list[dict[str, Any]] = []
        seen_asset_hash: set[str] = set()
        for a in assets:
            h = str(a.get("sha256") or "")
            if h and h in seen_asset_hash:
                continue
            if h:
                seen_asset_hash.add(h)
            deduped.append(a)
        if cover_url is None and deduped:
            cover_url = str(deduped[0].get("source_url") or "")
        return {"cover_url": cover_url, "image_urls": out_urls, "errors": errors, "assets": deduped}

    def _download_one(self, image_url: str, *, position: int, sku: str | None, job_id: str | None) -> IngestedImage:
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return IngestedImage(image_url, "", "", "", 0, None, None, position, "failed", ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)
        if self._is_blocked_host(parsed.hostname) or self._is_private_ip(parsed.hostname):
            return IngestedImage(image_url, "", "", "", 0, None, None, position, "failed", ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)

        response = self._safe_get(image_url)
        content_length = int(response.headers.get("content-length", 0) or 0)
        if content_length and content_length > self.max_image_size:
            return IngestedImage(image_url, "", "", "", 0, None, None, position, "failed", ErrorCode.ERR_IMAGE_SIZE_EXCEEDED.value)
        image_data = response.content
        if len(image_data) > self.max_image_size:
            return IngestedImage(image_url, "", "", "", 0, None, None, position, "failed", ErrorCode.ERR_IMAGE_SIZE_EXCEEDED.value)
        if not image_data:
            return IngestedImage(image_url, "", "", "", 0, None, None, position, "failed", ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)

        mime_type = self._detect_mime(image_data, response.headers.get("content-type", ""))
        ext = _ALLOWED_MIME.get(mime_type)
        if not ext:
            return IngestedImage(image_url, "", "", "", 0, None, None, position, "failed", ErrorCode.ERR_INVALID_IMAGE_FORMAT.value)

        image_hash = hashlib.sha256(image_data).hexdigest()
        image_path = os.path.join(self.download_dir, f"{image_hash}.{ext}")
        existed = os.path.exists(image_path)
        if not existed:
            with open(image_path, "wb") as f:
                f.write(image_data)
        try:
            with Image.open(image_path) as img:
                img.verify()
            with Image.open(image_path) as img2:
                width, height = img2.size
        except Exception:
            if not existed:
                try: os.remove(image_path)
                except OSError: pass
            return IngestedImage(image_url, "", image_hash, mime_type, len(image_data), None, None, position, "failed", ErrorCode.ERR_INVALID_IMAGE_FORMAT.value)

        logger.info("media_ingest_ok", extra={"job_id": job_id, "sku": sku, "image_url": image_url, "sha256": image_hash, "status": "duplicate" if existed else "ok", "local_path": image_path})
        return IngestedImage(image_url, image_path, image_hash, mime_type, len(image_data), width, height, position, "duplicate" if existed else "ok", None)

    def _safe_get(self, url: str) -> requests.Response:
        current = url
        for _ in range(self.max_redirects + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("invalid_url")
            if self._is_blocked_host(parsed.hostname) or self._is_private_ip(parsed.hostname):
                raise ValueError("blocked_host")
            resp = requests.get(current, stream=False, allow_redirects=False, timeout=self.timeout, headers={"User-Agent": "OMNI-MediaIngestion/1.0"})
            status_code = int(getattr(resp, "status_code", 200) or 200)
            is_redirect = bool(getattr(resp, "is_redirect", False)) or status_code in {301, 302, 303, 307, 308}
            if is_redirect:
                headers = getattr(resp, "headers", {}) or {}
                loc = headers.get("location") if hasattr(headers, "get") else None
                if not loc:
                    raise ValueError("redirect_missing_location")
                current = requests.compat.urljoin(current, loc)
                continue
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            return resp
        raise ValueError("too_many_redirects")

    def _detect_mime(self, data: bytes, content_type: str) -> str:
        ct = (content_type or "").split(";")[0].strip().lower()
        if ct in _ALLOWED_MIME:
            # verify with magic/Pillow to prevent spoofed extension/content-type
            pass
        kind = imghdr.what(None, h=data)
        mapped = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(kind, "")
        if mapped:
            return mapped
        try:
            with Image.open(os.path.join(self.download_dir, "__probe.tmp"),) as _:
                pass
        except Exception:
            pass
        try:
            import io
            with Image.open(io.BytesIO(data)) as img:
                fmt = (img.format or "").upper()
            return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt, ct)
        except Exception:
            return ct

    def _is_blocked_host(self, hostname: str) -> bool:
        h = (hostname or "").strip().lower().rstrip('.')
        return h in _BLOCKED_HOSTS or h.endswith('.internal') or h.endswith('.local')

    def _is_private_ip(self, hostname: str) -> bool:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except Exception:
            return True
        for info in infos:
            ip = info[4][0]
            try:
                addr = ipaddress.ip_address(ip.split('%')[0])
            except ValueError:
                return True
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
                return True
            # explicit cloud metadata/common blocked IPs
            if str(addr) in {"169.254.169.254", "169.254.170.2", "100.100.100.200"}:
                return True
        return False
