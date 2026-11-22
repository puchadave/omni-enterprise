"""Helper scoring utilities for product intelligence (extensible)."""
from __future__ import annotations
from typing import Any, Dict


def score_product_signals(finance: Dict[str, Any], quality: Dict[str, Any], market: Dict[str, Any]) -> float:
    f = float(finance.get("score", 50) or 50)
    q = float(quality.get("score", 50) or 50)
    m = float(market.get("score", 50) or 50)
    total = 0.35*f + 0.35*q + 0.30*m
    return max(0.0, min(100.0, total))
