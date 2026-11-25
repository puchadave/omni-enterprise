import hashlib
import ipaddress
import os
import socket
from typing import List
from urllib.parse import urlparse

import requests
from PIL import Image

from .enrichment_contracts import ProductSource, MediaIngestionResult
from .error_codes import ErrorCode


class MediaIngestionService:
    def __init__(self, download_dir: str, max_image_size: int = 10 * 1024 * 1024, timeout: int = 15):
        self.download_dir = download_dir
        self.max_image_size = max_image_size
        self.timeout = timeout
        os.makedirs(self.download_dir, exist_ok=True)

    def ingest(self, product: ProductSource) -> MediaIngestionResult:
        cover_url = None
        image_urls: List[str] = []
        errors: List[str] = []
        seen_hashes = set()

        for i, image_url in enumerate(product.images or []):
            try:
                parsed = urlparse(image_url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    errors.append(ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)
                    continue
                if self._is_private_ip(parsed.hostname):
                    errors.append(ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)
                    continue

                response = requests.get(image_url, stream=True, allow_redirects=False, timeout=self.timeout)
                response.raise_for_status()
                content_length = int(response.headers.get("content-length", 0) or 0)
                if content_length and content_length > self.max_image_size:
                    errors.append(ErrorCode.ERR_IMAGE_SIZE_EXCEEDED.value)
                    continue

                image_data = response.content
                if len(image_data) > self.max_image_size:
                    errors.append(ErrorCode.ERR_IMAGE_SIZE_EXCEEDED.value)
                    continue

                image_hash = hashlib.sha256(image_data).hexdigest()
                if image_hash in seen_hashes:
                    errors.append(ErrorCode.WARN_IMAGE_DEDUPED.value)
                    continue
                seen_hashes.add(image_hash)

                image_path = os.path.join(self.download_dir, f"{image_hash}.jpg")
                with open(image_path, "wb") as f:
                    f.write(image_data)
                try:
                    with Image.open(image_path) as img:
                        img.verify()
                except Exception:
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                    errors.append(ErrorCode.ERR_INVALID_IMAGE_FORMAT.value)
                    continue

                if i == 0 and cover_url is None:
                    cover_url = image_url
                image_urls.append(image_url)
            except Exception:
                errors.append(ErrorCode.ERR_IMAGE_DOWNLOAD_FAILED.value)

        if cover_url is None and image_urls:
            cover_url = image_urls[0]
        return MediaIngestionResult(cover_url=cover_url, image_urls=image_urls, errors=errors)

    def _is_private_ip(self, hostname: str) -> bool:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except Exception:
            return True
        for info in infos:
            ip = info[4][0]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                return True
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
                return True
        return False
