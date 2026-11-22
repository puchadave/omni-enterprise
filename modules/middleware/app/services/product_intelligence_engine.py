"""Product Intelligence & Sales AI Engine (MVP).
Analyzes products (trend/fashion, social, finance/margin, A-B signals, quality/stock),
produces explainable scorecards, recommends/modifies listings (guardrailed), and
controls export approval (gate). Uses OmniRoute combos when available.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_HOLD = "hold"
DECISION_REVIEW = "review_required"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


class ProductIntelligenceEngine:
    def __init__(self, omniroute_client: Any = None):
        self.omniroute_client = omniroute_client
        self.combo = os.getenv("OMNIROUTE_SALES_COMBO") or "omnicore-sales-product-intelligence"
        self.auto_approve = (os.getenv("PRODUCT_INTELLIGENCE_AUTO_APPROVE") or "false").lower() in {"1","true","yes","on"}
        self.min_margin_pct = _safe_float(os.getenv("PI_MIN_MARGIN_PCT", "10"), 10.0)
        self.min_score = _safe_float(os.getenv("PI_MIN_APPROVE_SCORE", "70"), 70.0)
        self.min_stock = int(_safe_float(os.getenv("PI_MIN_STOCK", "1"), 1))

    async def _fetch_product(self, sku: str) -> Optional[dict]:
        try:
            from database import fetch_product
            return await fetch_product(sku)
        except Exception:
            return None

    def _score_finance(self, product: dict) -> tuple[float, list[str], dict]:
        reasons = []
        buy = _safe_float(product.get("supplier_price") or product.get("purchase_price") or 0)
        sell = _safe_float(product.get("sale_price") or product.get("retail_price") or product.get("supplier_price_brutto") or 0)
        margin_abs = sell - buy if sell > 0 else 0.0
        margin_pct = (margin_abs / sell * 100.0) if sell > 0 else 0.0
        if sell <= 0:
            reasons.append("no_sell_price")
            score = 20.0
        elif buy <= 0:
            reasons.append("missing_purchase_price")
            score = 45.0
        elif margin_pct < 0:
            reasons.append("negative_margin")
            score = 5.0
        elif margin_pct < self.min_margin_pct:
            reasons.append(f"low_margin:{margin_pct:.1f}%")
            score = 40.0 + min(20.0, margin_pct)
        else:
            score = 60.0 + min(40.0, margin_pct)
            reasons.append(f"margin_ok:{margin_pct:.1f}%")
        return _clamp(score), reasons, {"buy": buy, "sell": sell, "margin_abs": round(margin_abs,2), "margin_pct": round(margin_pct,2)}

    def _score_quality(self, product: dict) -> tuple[float, list[str], dict]:
        reasons = []
        score = 0.0
        title = (product.get("title") or product.get("product_name") or "").strip()
        desc = (product.get("raw_description") or product.get("description") or "").strip()
        imgs = product.get("gallery_json") or product.get("images") or []
        if isinstance(imgs, str):
            try: imgs = json.loads(imgs)
            except Exception: imgs = [imgs] if imgs else []
        stock = int(_safe_float(product.get("stock") or product.get("stock_quantity") or 0))
        if title: score += 20
        else: reasons.append("missing_title")
        if len(desc) >= 40: score += 20
        else: reasons.append("weak_description")
        if imgs: score += 15
        else: reasons.append("missing_images")
        if product.get("ean") or product.get("gtin"): score += 10
        else: reasons.append("missing_gtin")
        if product.get("category"): score += 10
        else: reasons.append("missing_category")
        if stock >= self.min_stock: score += 25
        else: reasons.append("out_of_stock_or_low")
        return _clamp(score), reasons, {"stock": stock, "has_images": bool(imgs)}

    async def _score_trend_social_ab(self, product: dict, signals: list[dict]) -> tuple[float, list[str], dict]:
        reasons = []
        details: dict[str, Any] = {"signals_count": len(signals)}
        # heuristic baseline from signals
        trend = 50.0; social = 50.0; ab = 50.0
        for s in signals or []:
            src = str(s.get("source") or s.get("type") or "").lower()
            val = _safe_float(s.get("value") or s.get("score") or 0)
            if "trend" in src or "fashion" in src or "mode" in src:
                trend = max(trend, _clamp(50 + val))
            if "social" in src:
                social = max(social, _clamp(50 + val))
            if "ab" in src or "ab_test" in src:
                ab = max(ab, _clamp(50 + val))
        # optional LLM enrichment via OmniRoute (best-effort, non-blocking on failure)
        llm_note = None
        if self.omniroute_client and (os.getenv("PRODUCT_INTELLIGENCE_USE_LLM","true").lower() in {"1","true","yes","on"}):
            try:
                prompt = (
                    "Analysiere Verkaufs-Chancen (Trends/Mode, Social, A/B-Hinweise) für dieses Produkt. "
                    "Antworte NUR als kompaktes JSON mit Feldern trend_score,social_score,ab_score,summary (0-100). Produkt: "
                    + json.dumps({k: product.get(k) for k in ["sku","title","category","raw_description","brand"] if product.get(k) is not None}, ensure_ascii=False)[:3000]
                    + " Signale: " + json.dumps(signals[:20], ensure_ascii=False)[:2000]
                )
                combo = self.combo
                # try common client method names
                resp = None
                for meth in ("chat_completion","chat","complete","generate"):
                    fn = getattr(self.omniroute_client, meth, None)
                    if callable(fn):
                        try:
                            resp = await fn(messages=[{"role":"user","content":prompt}], model=combo)
                            break
                        except TypeError:
                            try:
                                resp = await fn(prompt=prompt, model=combo)
                                break
                            except Exception:
                                continue
                        except Exception:
                            continue
                if isinstance(resp, dict):
                    # try to extract text/json
                    text = None
                    try:
                        text = resp["choices"][0]["message"]["content"]
                    except Exception:
                        text = resp.get("content") or resp.get("text") or json.dumps(resp, ensure_ascii=False)
                    if isinstance(text, str):
                        jstart = text.find("{"); jend = text.rfind("}")
                        if jstart != -1 and jend != -1:
                            obj = json.loads(text[jstart:jend+1])
                            trend = _clamp(_safe_float(obj.get("trend_score", trend), trend))
                            social = _clamp(_safe_float(obj.get("social_score", social), social))
                            ab = _clamp(_safe_float(obj.get("ab_score", ab), ab))
                            llm_note = obj.get("summary")
                details["llm_used"] = True
            except Exception as e:
                logger.debug("PI llm scoring skipped: %s", e)
                details["llm_used"] = False
        else:
            details["llm_used"] = False
        if not signals:
            reasons.append("no_market_signals_baseline")
        score = _clamp(0.4*trend + 0.35*social + 0.25*ab)
        details.update({"trend": trend, "social": social, "ab": ab, "summary": llm_note})
        return score, reasons, details

    def _recommend_modifications(self, product: dict, finance: dict, quality_reasons: list[str]) -> list[dict]:
        mods = []
        title = (product.get("title") or "").strip()
        desc = (product.get("raw_description") or product.get("description") or "").strip()
        if not title or len(title) < 10:
            sku = product.get("sku","")
            cat = product.get("category","")
            mods.append({"field": "title", "action": "suggest", "before": title, "after": f"{(cat+' ').strip()}{sku}".strip() or title, "reason": "improve_title"})
        if len(desc) < 40:
            mods.append({"field": "description", "action": "suggest", "before": desc[:120], "after": (desc + " Hochwertiges Produkt – geprüft und schnell lieferbar.").strip(), "reason": "improve_description"})
        margin_pct = _safe_float(finance.get("margin_pct"), 0)
        if margin_pct and margin_pct < self.min_margin_pct and _safe_float(finance.get("sell"),0) > 0:
            # guardrailed price suggestion (never auto-apply by default)
            target_sell = round(_safe_float(finance.get("buy")) / (1 - self.min_margin_pct/100.0), 2) if self.min_margin_pct < 100 else finance.get("sell")
            mods.append({"field": "sale_price", "action": "suggest", "before": finance.get("sell"), "after": target_sell, "reason": "margin_guardrail", "guardrail": {"min_margin_pct": self.min_margin_pct}})
        return mods

    async def analyze(self, sku: str, channel: str = "shopware", *, signals: Optional[List[dict]] = None, persist: bool = True, actor: str = "system") -> dict:
        run_id = str(uuid.uuid4())
        started = _now_iso()
        product = await self._fetch_product(sku)
        if not product:
            result = {"ok": False, "sku": sku, "channel": channel, "error": "product_not_found", "run_id": run_id}
            return result
        if signals is None:
            signals = []
            try:
                from database import list_product_market_signals
                signals = await list_product_market_signals(sku=sku, limit=50)
            except Exception:
                signals = []
        fin_score, fin_reasons, fin = self._score_finance(product)
        qual_score, qual_reasons, qual = self._score_quality(product)
        m_score, m_reasons, mdet = await self._score_trend_social_ab(product, signals)
        total = _clamp(0.35*fin_score + 0.35*qual_score + 0.30*m_score)
        reasons = fin_reasons + qual_reasons + m_reasons
        # hard blocks
        decision = DECISION_REVIEW
        hard_block = False
        if fin.get("margin_pct", 0) < 0 or qual.get("stock", 0) < self.min_stock:
            decision = DECISION_HOLD if qual.get("stock", 0) < self.min_stock else DECISION_REJECTED
            hard_block = True
        elif "no_sell_price" in reasons or "missing_title" in reasons:
            decision = DECISION_REVIEW
        elif total >= self.min_score and fin.get("margin_pct", 0) >= self.min_margin_pct:
            decision = DECISION_APPROVED if self.auto_approve else DECISION_REVIEW
            if self.auto_approve:
                reasons.append("auto_approve_policy")
        else:
            decision = DECISION_REVIEW
        modifications = self._recommend_modifications(product, fin, qual_reasons)
        expires_at = (_now() + timedelta(days=7)).isoformat() if decision == DECISION_APPROVED else None
        scorecard = {
            "total": round(total,2),
            "finance": round(fin_score,2),
            "quality": round(qual_score,2),
            "market": round(m_score,2),
            "details": {"finance": fin, "quality": qual, "market": mdet},
        }
        record = {
            "ok": True,
            "run_id": run_id,
            "sku": sku,
            "channel": channel,
            "decision": decision,
            "scorecard": scorecard,
            "reasons": reasons,
            "modifications": modifications,
            "hard_block": hard_block,
            "expires_at": expires_at,
            "analyzed_at": started,
            "combo": self.combo,
        }
        if persist:
            try:
                from database import save_product_analysis_run, save_product_analysis_result, save_product_modifications, upsert_product_export_approval
                await save_product_analysis_run({
                    "run_id": run_id, "sku": sku, "channel": channel, "status": "completed",
                    "model_combo": self.combo, "started_at": started, "finished_at": _now_iso(),
                    "summary_json": json.dumps({"total": scorecard["total"], "decision": decision}, ensure_ascii=False)
                })
                await save_product_analysis_result(record)
                if modifications:
                    await save_product_modifications(run_id, sku, channel, modifications)
                # persist gate decision (conservative: manual review unless auto-approve)
                await upsert_product_export_approval({
                    "sku": sku, "channel": channel, "decision": decision,
                    "source": "product_intelligence", "score": scorecard["total"],
                    "reasons_json": json.dumps(reasons, ensure_ascii=False),
                    "modifications_json": json.dumps(modifications, ensure_ascii=False),
                    "actor": actor, "run_id": run_id, "expires_at": expires_at,
                    "updated_at": _now_iso(),
                })
            except Exception as e:
                logger.warning("PI persist failed: %s", e)
                record["persist_warning"] = str(e)
        return record
