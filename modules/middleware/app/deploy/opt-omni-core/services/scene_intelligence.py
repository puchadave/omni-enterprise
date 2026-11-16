"""Scene Intelligence aggregation over stored, real data records.

No external scraping or fabricated signals: derives pulse/radar/trends from
persisted news_items, news_sources and knowledge_entries.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from database import knowledge_list, news_list_items, news_list_sources


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _recency_score(dt: datetime | None, now: datetime) -> float:
    if not dt:
        return 0.2
    age_h = max((now - dt).total_seconds() / 3600.0, 0.0)
    if age_h <= 24:
        return 1.0
    if age_h <= 72:
        return 0.8
    if age_h <= 168:
        return 0.5
    if age_h <= 720:
        return 0.3
    return 0.1


def _classify_kind(item: dict[str, Any]) -> str:
    text = f"{item.get('title','')} {item.get('summary','')} {item.get('category','')}".lower()
    if any(k in text for k in ("festival", "event", "live", "tour", "show", "konzert")):
        return "event"
    if any(k in text for k in ("release", "album", "single", "ep", "drop")):
        return "release"
    if any(k in text for k in ("artist", "dj", "band", "producer")):
        return "artist"
    if any(k in text for k in ("label", "imprint")):
        return "label"
    return "news"


async def overview(limit: int = 200) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    sources = await news_list_sources()
    items = await news_list_items(limit=limit)
    knowledge = await knowledge_list(limit=100)
    cats = Counter(str(i.get("category") or "general") for i in items)
    kinds = Counter(_classify_kind(i) for i in items)
    enabled_sources = [s for s in sources if bool(s.get("enabled", True))]
    recent = []
    for i in items:
        dt = _ts(i.get("published_at") or i.get("created_at"))
        score = round(_recency_score(dt, now), 3)
        recent.append({**i, "_kind": _classify_kind(i), "_relevance": score})
    recent.sort(key=lambda x: x.get("_relevance", 0), reverse=True)
    # pulse: simple deterministic index from volume/recency/source coverage (stored data only)
    vol = len(items)
    src_cov = len({str(i.get("source_id") or "") for i in items if i.get("source_id")})
    rec_avg = (sum(r["_relevance"] for r in recent) / len(recent)) if recent else 0.0
    pulse = round(min(100.0, (vol * 2.0) + (src_cov * 10.0) + (rec_avg * 40.0)), 2)
    trends = [{"category": k, "count": v} for k, v in cats.most_common(10)]
    radar = [{"kind": k, "count": v} for k, v in kinds.most_common()]
    return {
        "status": "ok",
        "generated_at": now.isoformat(),
        "data_basis": "stored_records_only",
        "sources": {"total": len(sources), "enabled": len(enabled_sources)},
        "items": {"total": len(items)},
        "knowledge": {"total": len(knowledge)},
        "pulse": {"score": pulse, "explanation": "Deterministischer Index aus gespeichertem Volumen, Quellenabdeckung und Aktualität; keine externen Live-Daten."},
        "trends": trends,
        "radar": radar,
        "top_signals": recent[:20],
        "notes": [] if items else ["Keine Scene-Daten gespeichert. Quellen/Items anlegen oder konfigurierte Feeds ingestieren; es werden keine Daten erfunden."],
    }
