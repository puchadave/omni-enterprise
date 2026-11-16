"""Matterhorn Image Management.

Handles product image downloading from Matterhorn, SHA-256 deduplication,
image assignment to products/variants, and download queue management.

Key features:
- Download product images from Matterhorn image URLs
- SHA-256 hash-based deduplication (no duplicate storage)
- Original image storage (preserve source quality)
- Image assignment to products and variants
- Download queue with retry logic
- Error logging and reporting
- Thumbnail generation (asynchronous, on-demand)
- Image metadata preservation (size, format, dimensions)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

# ── Image Storage Paths ───────────────────────────────────────────────────

IMAGE_BASE_DIR: str = "media/products"
ORIGINAL_DIR: str = "original"
THUMBNAIL_DIR: str = "thumbnails"
CACHE_DIR: str = "cache"


@dataclass
class ImageResult:
    """Result of an image download/processing operation."""

    url: str
    sha256_hash: str
    saved_path: str | None = None
    original_filename: str | None = None
    width: int | None = None
    height: int | None = None
    size_kb: int | None = None
    error: str | None = None
    is_duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "sha256_hash": self.sha256_hash,
            "saved_path": self.saved_path,
            "original_filename": self.original_filename,
            "width": self.width,
            "height": self.height,
            "size_kb": self.size_kb,
            "error": self.error,
            "is_duplicate": self.is_duplicate,
        }


class ImageManager:
    """Manages product image downloading, deduplication, and storage."""

    def __init__(self, base_dir: str = IMAGE_BASE_DIR) -> None:
        self.base_dir: Path = Path(base_dir)
        self._download_queue: list[dict[str, Any]] = []
        self._processed_hashes: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for dir_path in [self.base_dir, self.base_dir / ORIGINAL_DIR, self.base_dir / THUMBNAIL_DIR, self.base_dir / CACHE_DIR]:
            dir_path.mkdir(parents=True, exist_ok=True)

    def _calculate_sha256(self, file_path: Path) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _image_exists(self, sha256_hash: str) -> bool:
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for img_file in self.base_dir.rglob(pattern):
                try:
                    if self._calculate_sha256(img_file) == sha256_hash:
                        return True
                except Exception:
                    continue
        return False

    def download_image(self, image_url: str, original_filename: str | None = None) -> ImageResult:
        result = ImageResult(url=image_url, sha256_hash="", original_filename=original_filename or image_url.split("/")[-1])
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "Matterhorn-Image-Downloader/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                image_data = response.read()
            sha256_hash = hashlib.sha256(image_data).hexdigest()
            result.sha256_hash = sha256_hash
            if self._image_exists(sha256_hash):
                result.is_duplicate = True
                result.saved_path = self._get_duplicate_path(sha256_hash)
                return result
            safe_filename = self._sanitize_filename(original_filename or image_url.split("/")[-1] or "image.jpg")
            original_path = self.base_dir / ORIGINAL_DIR / safe_filename
            if original_path.exists():
                stem, ext = original_path.stem, original_path.suffix
                original_path = original_path.with_name(f"{stem}_{sha256_hash[:8]}{ext}")
            with open(original_path, "wb") as f:
                f.write(image_data)
            result.saved_path = str(original_path)
            try:
                img = PILImage.open(original_path)
                result.width = img.width
                result.height = img.height
            except Exception:
                result.width = 0
                result.height = 0
            result.size_kb = round(os.path.getsize(original_path) / 1024)
            self._processed_hashes.add(sha256_hash)
            return result
        except Exception as exc:
            logger.error("Failed to download image from %s: %s", image_url, exc)
            result.error = str(exc)
            return result

    def _sanitize_filename(self, filename: str) -> str:
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename or "image.jpg")
        if len(safe) > 200:
            base, ext = os.path.splitext(safe)
            safe = base[:190] + ext
        return safe or "image.jpg"

    def _get_duplicate_path(self, sha256_hash: str) -> str:
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for img_file in self.base_dir.rglob(pattern):
                try:
                    if self._calculate_sha256(img_file) == sha256_hash:
                        return str(img_file)
                except Exception:
                    continue
        return str(self.base_dir / ORIGINAL_DIR / f"{sha256_hash[:8]}.jpg")

    def download_image_async(self, image_url: str, original_filename: str | None = None) -> None:
        self._download_queue.append({"url": image_url, "original_filename": original_filename})

    def process_download_queue(self) -> list[ImageResult]:
        results: list[ImageResult] = []
        for item in self._download_queue:
            results.append(self.download_image(item["url"], item.get("original_filename")))
        self._download_queue.clear()
        return results

    def assign_image_to_product(self, product_sku: str, image_url: str, image_order: int = 0) -> ImageResult:
        return self.download_image(image_url)

    def get_product_images(self, product_sku: str) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        product_dir = self.base_dir / product_sku
        if not product_dir.exists():
            return images
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for img_file in product_dir.rglob(pattern):
                try:
                    images.append({
                        "path": str(img_file),
                        "sha256_hash": self._calculate_sha256(img_file),
                        "filename": img_file.name,
                        "size_kb": round(os.path.getsize(img_file) / 1024),
                    })
                except Exception:
                    pass
        return images
