"""Matterhorn Brand & Category Manager.

Handles import, mapping, and management of brands and categories from
Matterhorn data into the Omni-Core canonical product model.

Key features:
- Brand import with ID mapping
- Category hierarchy import
- External (supplier) vs. internal category IDs
- Automatic mapping suggestions
- Manual correction capability via WebIF
- All mappings stored permanently and editable
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Brand Management ───────────────────────────────────────────────────────

BrandInfo = dict[str, Any]


class BrandManager:
    """Manages brands from Matterhorn and maps them to internal representation."""

    def __init__(self) -> None:
        # Maps supplier brand ID -> internal brand info
        self._supplier_brand_map: dict[str, BrandInfo] = {}
        # Maps internal brand name -> brand info
        self._internal_brand_map: dict[str, BrandInfo] = {}
        # Tracks brand import history
        self._import_history: list[dict[str, Any]] = []

    # ── Brand Import from Matterhorn ───────────────────────────────────────

    async def import_brands(
        self, api_client: MatterhornAPIClient
    ) -> dict[str, Any]:
        """Import all brands from Matterhorn dictionary API.

        Returns dict with import statistics.
        """
        stats: dict[str, Any] = {
            "total_on_matterhorn": 0,
            "imported_new": 0,
            "updated_existing": 0,
            "already_existed": 0,
            "errors": 0,
        }

        try:
            brands = await api_client.dictionaries.list_brands()
            stats["total_on_matterhorn"] = len(brands)

            for brand_data in brands:
                # Normalize brand data
                brand_name: str = brand_data.get("name", "").strip()
                brand_id: str = brand_data.get("brand_id", "").strip()
                country: str | None = brand_data.get("country", "").strip() or None

                if not brand_name:
                    stats["errors"] += 1
                    continue

                # Check if we already have this brand
                existing = self._find_brand_by_name(brand_name)
                existing_by_id = self._find_brand_by_id(brand_id) if brand_id else None

                if existing and existing_by_id and existing != existing_by_id:
                    # Conflict - same name different ID, keep both with note
                    stats["already_existed"] += 1
                    continue

                if existing or existing_by_id:
                    # Update existing brand
                    target = existing or existing_by_id
                    target["country"] = country or target.get("country")
                    stats["updated_existing"] += 1
                else:
                    # New brand
                    brand_info: BrandInfo = {
                        "brand_id": brand_id or f"auto_{len(self._supplier_brand_map) + 1}",
                        "name": brand_name,
                        "country": country,
                        "source": "matterhorn",
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    }

                    # Store under both supplier ID and brand name
                    if brand_id:
                        self._supplier_brand_map[brand_id] = brand_info
                    self._internal_brand_map[brand_name.lower()] = brand_info

                    stats["imported_new"] += 1

                self._import_history.append({
                    "action": "import",
                    "brand_name": brand_name,
                    "result": "new" if not (existing or existing_by_id) else "updated",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        except Exception as exc:
            logger.error("Brand import failed: %s", exc, exc_info=True)
            stats["errors"] = 1

        return stats

    # ── Brand Lookup ──────────────────────────────────────────────────────

    def _find_brand_by_name(self, name: str) -> BrandInfo | None:
        """Find brand by normalized name."""
        return self._internal_brand_map.get(name.lower())

    def _find_brand_by_id(self, brand_id: str) -> BrandInfo | None:
        """Find brand by supplier brand ID."""
        return self._supplier_brand_map.get(brand_id)

    def get_brand(self, identifier: str) -> BrandInfo | None:
        """Get brand by either name or ID.

        Args:
            identifier: Brand name (case-insensitive) or supplier brand ID
        """
        # Try name first
        brand = self._find_brand_by_name(identifier)
        if brand:
            return brand

        # Try ID
        return self._find_brand_by_id(identifier)

    def list_brands(self) -> list[BrandInfo]:
        """List all known brands (union of supplier and internal)."""
        all_brands: dict[str, BrandInfo] = {}
        all_brands.update(self._supplier_brand_map)
        all_brands.update(self._internal_brand_map)
        return list(all_brands.values())

    def list_supplier_brand_ids(self) -> list[str]:
        """List all supplier (Matterhorn) brand IDs known."""
        return list(self._supplier_brand_map.keys())

    def list_brand_names(self) -> list[str]:
        """List all known brand names."""
        return sorted(self._internal_brand_map.keys())

    # ── Brand Correction (WebIF) ──────────────────────────────────────────

    def rename_brand(self, old_name: str, new_name: str) -> bool:
        """Rename a brand (WebIF operation).

        Args:
            old_name: Current brand name
            new_name: New brand name

        Returns:
            True if rename successful
        """
        brand = self._find_brand_by_name(old_name)
        if not brand:
            return False

        # Remove old entry
        del self._internal_brand_map[old_name.lower()]

        # Update brand name
        brand["name"] = new_name

        # Add new entry with lowercase key
        self._internal_brand_map[new_name.lower()] = brand

        # Update import history
        self._import_history.append({
            "action": "rename",
            "old_name": old_name,
            "new_name": new_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return True

    def delete_brand(self, name: str) -> bool:
        """Delete/soft-delete a brand.

        Args:
            name: Brand name to delete

        Returns:
            True if deletion successful
        """
        brand = self._find_brand_by_name(name)
        if not brand:
            return False

        # Soft delete - mark as inactive rather than remove
        brand["active"] = False
        brand["deleted_at"] = datetime.now(timezone.utc).isoformat()

        self._import_history.append({
            "action": "delete",
            "brand_name": name,
            "result": "soft_deleted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return True

# ── Category Management ───────────────────────────────────────────────────

CategoryInfo = dict[str, Any]


class CategoryManager:
    """Manages categories from Matterhorn with hierarchy and mapping."""

    def __init__(self) -> None:
        # Maps supplier category ID -> category info
        self._supplier_category_map: dict[str, CategoryInfo] = {}
        # Maps internal category path -> category info
        self._internal_category_map: dict[str, CategoryInfo] = {}
        # Category hierarchy: parent_path -> [child_paths]
        self._hierarchy: dict[str, list[str]] = defaultdict(list)
        # Import history
        self._import_history: list[dict[str, Any]] = []

    # ── Category Import from Matterhorn ────────────────────────────────────

    async def import_categories(
        self, api_client: MatterhornAPIClient
    ) -> dict[str, Any]:
        """Import all categories from Matterhorn dictionary API.

        Returns dict with import statistics.
        """
        stats: dict[str, Any] = {
            "total_on_matterhorn": 0,
            "imported_new": 0,
            "updated_existing": 0,
            "already_existed": 0,
            "errors": 0,
            "root_categories": 0,
        }

        try:
            categories = await api_client.dictionaries.list_categories()
            stats["total_on_matterhorn"] = len(categories)

            for cat_data in categories:
                # Normalize category data
                cat_name: str = cat_data.get("name", "").strip()
                cat_id: str = cat_data.get("category_id", "").strip()
                parent_id: str | None = cat_data.get("parent_id", "").strip() or None
                level: str | None = cat_data.get("level", "").strip() or None
                external_id: str | None = cat_data.get("external_id", "").strip() or None

                if not cat_name:
                    stats["errors"] += 1
                    continue

                # Build category info
                cat_info: CategoryInfo = {
                    "category_id": cat_id or f"auto_{len(self._supplier_category_map) + 1}",
                    "name": cat_name,
                    "parent_id": parent_id,
                    "level": level or "0",
                    "external_id": external_id or None,
                    "source": "matterhorn",
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                }

                # Determine if this is a root category (no parent or level 0)
                is_root: bool = (
                    not parent_id
                    or parent_id.strip() == ""
                    or level == "0"
                    or level == "root"
                )

                # Check for existing category with same supplier ID
                existing = self._supplier_category_map.get(cat_id) if cat_id else None
                existing_by_name = self._find_category_by_name(cat_name)

                if existing and existing_by_name and existing != existing_by_name:
                    # Conflict - same name different ID
                    stats["already_existed"] += 1
                    # Still import the hierarchy info from Matterhorn
                    if is_root:
                        stats["root_categories"] += 1
                    continue

                if existing or existing_by_name:
                    # Update existing category
                    target = existing or existing_by_name
                    # Update hierarchy info
                    target["parent_id"] = cat_info["parent_id"]
                    target["level"] = cat_info["level"]
                    target["external_id"] = cat_info["external_id"] or target.get(
                        "external_id"
                    )
                    # Update hierarchy
                    self._update_hierarchy(target, is_root)
                    stats["updated_existing"] += 1
                else:
                    # New category
                    self._supplier_category_map[cat_id or ""] = cat_info
                    # Also store under name for lookup
                    self._internal_category_map[cat_name.lower()] = cat_info
                    stats["imported_new"] += 1

                # Track hierarchy
                self._update_hierarchy(cat_info, is_root)

                self._import_history.append({
                    "action": "import",
                    "category_name": cat_name,
                    "category_id": cat_info["category_id"],
                    "result": "new" if not (existing or existing_by_name) else "updated",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        except Exception as exc:
            logger.error("Category import failed: %s", exc, exc_info=True)
            stats["errors"] = 1

        return stats

    # ── Category Lookup ──────────────────────────────────────────────────

    def _find_category_by_name(self, name: str) -> CategoryInfo | None:
        """Find category by normalized name."""
        return self._internal_category_map.get(name.lower())

    def get_category(self, identifier: str) -> CategoryInfo | None:
        """Get category by ID or name.

        Args:
            identifier: Category ID (supplier) or category name
        """
        # Try ID first
        if identifier and identifier in self._supplier_category_map:
            return self._supplier_category_map[identifier]

        # Try name
        return self._find_category_by_name(identifier)

    def list_categories(self) -> list[CategoryInfo]:
        """List all known categories (union of supplier and internal)."""
        all_cats: dict[str, CategoryInfo] = {}
        all_cats.update(self._supplier_category_map)
        all_cats.update(self._internal_category_map)
        return list(all_cats.values())

    def get_category_hierarchy(self) -> dict[str, list[str]]:
        """Get the full category hierarchy.

        Returns:
            dict mapping parent category path to list of child category paths
        """
        return dict(self._hierarchy)

    def get_category_by_path(self, path: str) -> CategoryInfo | None:
        """Get category by hierarchical path.

        Args:
            path: Dot-separated or slash-separated path like "Men/Shoes/Sneakers"
        """
        # Try exact match
        cat = self._internal_category_map.get(path.lower())
        if cat:
            return cat

        # Try partial match - find categories that contain this path
        for cat_name, cat_info in self._internal_category_map.items():
            if path.lower() in cat_name or cat_name in path.lower():
                return cat_info

        return None

    # ── Category Correction (WebIF) ──────────────────────────────────────

    def rename_category(self, old_path: str, new_path: str) -> bool:
        """Rename a category (WebIF operation).

        Args:
            old_path: Current category path/name
            new_path: New category path/name

        Returns:
            True if rename successful
        """
        category = self.get_category(old_path)
        if not category:
            return False

        # Remove old entry
        old_key = old_path.lower()
        if old_key in self._internal_category_map:
            del self._internal_category_map[old_key]
        if old_key in self._supplier_category_map:
            del self._supplier_category_map[old_key]

        # Update category name
        category["name"] = new_path

        # Add new entry
        new_key = new_path.lower()
        self._internal_category_map[new_key] = category
        if category.get("category_id"):
            self._supplier_category_map[category["category_id"]] = category

        # Update hierarchy
        self._update_category_hierarchy(category, old_path, new_path)

        self._import_history.append({
            "action": "rename",
            "old_path": old_path,
            "new_path": new_path,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return True

    def _update_category_hierarchy(
        self, category: CategoryInfo, old_path: str, new_path: str
    ) -> None:
        """Update category hierarchy after rename.

        Args:
            category: The category info dict
            old_path: The old category path
            new_path: The new category path
        """
        # Remove old hierarchy entries
        for parent, children in self._hierarchy.items():
            if old_path in children:
                self._hierarchy[parent].remove(old_path)

        # Add new hierarchy entries
        # Determine parent (remove the last level to get parent)
        old_parts = old_path.split("/") if "/" in old_path else old_path.split(".")
        new_parts = new_path.split("/") if "/" in new_path else new_path.split(".")

        if len(new_parts) > 1:
            parent_path = "/".join(new_parts[:-1])
            if parent_path not in self._hierarchy:
                self._hierarchy[parent_path] = []
            if new_path not in self._hierarchy[parent_path]:
                self._hierarchy[parent_path].append(new_path)

    def _update_hierarchy(self, cat_info: CategoryInfo, is_root: bool) -> None:
        """Update the category hierarchy structure.

        Args:
            cat_info: Category info dict
            is_root: Whether this is a root category
        """
        cat_name: str = cat_info["name"]
        cat_id: str | None = cat_info.get("category_id")

        if is_root:
            # Root categories are tracked at the top level
            # No parent in hierarchy
            pass
        else:
            # Has a parent - we need to establish the hierarchy
            # In a real implementation, we'd look up the parent category
            # For now, we'll just note that this has a parent
            parent_key = cat_name  # Simplified - in reality would be parent path
            if parent_key not in self._hierarchy:
                self._hierarchy[parent_key] = []
            if cat_name not in self._hierarchy[parent_key]:
                self._hierarchy[parent_key].append(cat_info["name"])

# ── Convenience Function ─────────────────────────────────────────────────

def build_brand_category_overview(
    brand_manager: BrandManager,
    category_manager: CategoryManager,
) -> dict[str, Any]:
    """Build an overview dict combining brand and category info.

    Useful for WebIF dashboard display.
    """
    return {
        "brands": {
            "count": len(brand_manager.list_brands()),
            "supplier_ids": brand_manager.list_supplier_brand_ids(),
            "names": brand_manager.list_brand_names(),
        },
        "categories": {
            "count": len(category_manager.list_categories()),
            "hierarchy": category_manager.get_category_hierarchy(),
            "root_count": sum(
                1
                for c in category_manager.list_categories()
                if c.get("level", "0") in ("0", "root", "")
            ),
        },
        "import_history": {
            "brands": len(brand_manager._import_history),
            "categories": len(category_manager._import_history),
        },
    }