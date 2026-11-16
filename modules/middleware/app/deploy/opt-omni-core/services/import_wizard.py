"""Matterhorn Import Wizard.

A multi-step wizard for importing products from Matterhorn through the
WebIF. Guides the user through 10 steps from source selection through
to import completion.

Import Modes:
- Vollimport (Full import)
- Delta-Import (Incremental update)
- Bestandsupdate (Stock update)
- Preisupdate (Price update)
- Produktupdate (Product update)
- Nur neue Produkte (New products only)
- Nur EAN-Produkte (EAN products only)
- Markenimport (Brand import)
- Kategorienimport (Category import)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Import Mode Enum ──────────────────────────────────────────────────────

class ImportMode(Enum):
    """Available import modes."""
    FULL = auto()          # Vollimport - all products
    DELTA = auto()         # Delta-Import - only changed products
    STOCK_UPDATE = auto()  # Bestandsupdate - only stock levels
    PRICE_UPDATE = auto()  # Preisupdate - only prices
    PRODUCT_UPDATE = auto() # Produktupdate - product data only
    NEW_ONLY = auto()      # Nur neue Produkte - only products not in system
    EAN_ONLY = auto()      # Nur EAN-Produkte - only products with EAN codes
    BRAND_IMPORT = auto()  # Markenimport - only brand data
    CATEGORY_IMPORT = auto() # Kategorienimport - only category data

    def __str__(self) -> str:
        # German names for WebIF display
        names = {
            ImportMode.FULL: "Vollimport",
            ImportMode.DELTA: "Delta-Import",
            ImportMode.STOCK_UPDATE: "Bestandsupdate",
            ImportMode.PRICE_UPDATE: "Preisupdate",
            ImportMode.PRODUCT_UPDATE: "Produktupdate",
            ImportMode.NEW_ONLY: "Nur neue Produkte",
            ImportMode.EAN_ONLY: "Nur EAN-Produkte",
            ImportMode.BRAND_IMPORT: "Markenimport",
            ImportMode.CATEGORY_IMPORT: "Kategorienimport",
        }
        return names.get(self, self.name)

# ── Import Step Enum ──────────────────────────────────────────────────────

class ImportStep(Enum):
    """Wizard steps."""
    SOURCE_SELECT = auto()
    FORMAT_SELECT = auto()
    LANGUAGE_CURRENCY = auto()
    CATEGORY_BRAND = auto()
    MAPPING_PREVIEW = auto()
    VALIDATION = auto()
    CONFIRMATION = auto()
    EXECUTION = auto()
    RESULT = auto()
    FINISH = auto()

# ── Import Wizard State ─────────────────────────────────────────────────

@dataclass
class WizardState:
    current_step: int = 1
    mode: ImportMode = ImportMode.FULL
    state: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

CurrentWizardState = dict[str, Any]

# ── Import Wizard Class ─────────────────────────────────────────────────

class ImportWizard:
    """Multi-step import wizard for Matterhorn products.

    Guides the user through 10 steps:
    1. Datenquelle auswählen (Source selection)
    2. Feed-Format auswählen (Format selection)
    3. Sprache, Währung und Größensystem auswählen (Language/currency/size)
    4. Kategorien und Marken auswählen (Category/brand selection)
    5. Mapping überprüfen (Mapping review)
    6. Validierungsfehler anzeigen (Show validation errors)
    7. Import freigeben (Approve import)
    8. Import ausführen (Execute import)
    9. Ergebnisbericht anzeigen (Show result report)
    10. Fertigstellen (Finish)
    """

    def __init__(self, control_center: MatterhornControlCenter) -> None:
        self.control_center = control_center
        self.mode: ImportMode = ImportMode.FULL
        self.current_step: ImportStep = ImportStep.SOURCE_SELECT
        self.state: CurrentWizardState = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.import_result: dict[str, Any] | None = None

    # ── Step Navigation ─────────────────────────────────────────────────

    def can_advance(self) -> bool:
        """Check if we can advance to the next step.

        Returns True if the current step has valid data to proceed.
        """
        # Each step has different validation requirements
        if self.current_step == ImportStep.SOURCE_SELECT:
            return self.state.get("source_selected", False)
        elif self.current_step == ImportStep.FORMAT_SELECT:
            return self.state.get("format_selected", False)
        elif self.current_step == ImportStep.LANGUAGE_CURRENCY:
            return bool(self.state.get("language")) and bool(self.state.get("currency"))
        elif self.current_step == ImportStep.CATEGORY_BRAND:
            return bool(self.state.get("categories_selected")) or bool(
                self.state.get("brands_selected")
            )
        elif self.current_step == ImportStep.MAPPING_PREVIEW:
            return bool(self.state.get("mapping_valid", False))
        elif self.current_step == ImportStep.VALIDATION:
            return len(self.errors) == 0  # Can proceed if no critical errors
        elif self.current_step == ImportStep.CONFIRMATION:
            return self.state.get("confirmed", False)
        return False

    def advance(self) -> bool:
        """Advance to the next wizard step.

        Returns True if advance was successful.
        """
        # Determine next step
        step_order: list[ImportStep] = [
            ImportStep.SOURCE_SELECT,
            ImportStep.FORMAT_SELECT,
            ImportStep.LANGUAGE_CURRENCY,
            ImportStep.CATEGORY_BRAND,
            ImportStep.MAPPING_PREVIEW,
            ImportStep.VALIDATION,
            ImportStep.CONFIRMATION,
            ImportStep.EXECUTION,
            ImportStep.RESULT,
            ImportStep.FINISH,
        ]

        current_idx: int | None = None
        for i, step in enumerate(step_order):
            if step == self.current_step:
                current_idx = i
                break

        if current_idx is None or current_idx + 1 >= len(step_order):
            return False  # At last step or invalid

        self.current_step = step_order[current_idx + 1]
        return True

    def retreat(self) -> bool:
        """Retreat to the previous wizard step.

        Returns True if retreat was successful.
        """
        step_order: list[ImportStep] = [
            ImportStep.SOURCE_SELECT,
            ImportStep.FORMAT_SELECT,
            ImportStep.LANGUAGE_CURRENCY,
            ImportStep.CATEGORY_BRAND,
            ImportStep.MAPPING_PREVIEW,
            ImportStep.VALIDATION,
            ImportStep.CONFIRMATION,
            ImportStep.EXECUTION,
            ImportStep.RESULT,
            ImportStep.FINISH,
        ]

        current_idx: int | None = None
        for i, step in enumerate(step_order):
            if step == self.current_step:
                current_idx = i
                break

        if current_idx is None or current_idx == 0:
            return False  # At first step

        self.current_step = step_order[current_idx - 1]
        return True

    # ── Step Data Management ────────────────────────────────────────────

    def set_state(self, key: str, value: Any) -> None:
        """Set wizard state data."""
        self.state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        """Get wizard state data."""
        return self.state.get(key, default)

    def set_error(self, error: str) -> None:
        """Add an error message."""
        self.errors.append(error)

    def clear_errors(self) -> None:
        """Clear all error messages."""
        self.errors = []

    def set_warning(self, warning: str) -> None:
        """Add a warning message."""
        self.warnings.append(warning)

    def clear_warnings(self) -> None:
        """Clear all warning messages."""
        self.warnings = []

# ── Import Mode Helpers ─────────────────────────────────────────────────

def get_import_mode_by_name(name: str) -> ImportMode:
    """Convert a string name to an ImportMode enum."""
    mode_map: dict[str, ImportMode] = {
        "Vollimport": ImportMode.FULL,
        "Delta-Import": ImportMode.DELTA,
        "Bestandsupdate": ImportMode.STOCK_UPDATE,
        "Preisupdate": ImportMode.PRICE_UPDATE,
        "Produktupdate": ImportMode.PRODUCT_UPDATE,
        "Nur neue Produkte": ImportMode.NEW_ONLY,
        "Nur EAN-Produkte": ImportMode.EAN_ONLY,
        "Markenimport": ImportMode.BRAND_IMPORT,
        "Kategorienimport": ImportMode.CATEGORY_IMPORT,
    }
    return mode_map.get(name, ImportMode.FULL)

def get_import_mode_display(mode: ImportMode) -> str:
    """Get the display name for an ImportMode."""
    return str(mode)

# ── Convenience Function for WebIF ──────────────────────────────────────

def start_import_wizard(control_center: MatterhornControlCenter,
                        mode: ImportMode = ImportMode.FULL) -> ImportWizard:
    """Initialize and return a new ImportWizard instance.

    Args:
        control_center: The MatterhornControlCenter instance
        mode: The import mode to use

    Returns:
        A new ImportWizard instance ready to start at step 1
    """
    wizard = ImportWizard(control_center)
    wizard.mode = mode
    wizard.current_step = ImportStep.SOURCE_SELECT
    # Initialize source selection state
    wizard.state["source_selected"] = False
    wizard.state["source_type"] = "matterhorn"
    return wizard