"""Matterhorn product importer: lossless multi-format preview/import with sync rules."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import httpx

from database import (
    finish_import_run,
    get_setting,
    matterhorn_list_feeds,
    matterhorn_save_preview,
    refresh_all_bundle_stocks,
    start_import_run,
    upsert_product_aggregate,
    write_log,
)

logger = logging.getLogger(__name__)

MATTERHORN_XML_FEED: str = "https://matterhorn-moda.de/?str=xmloffer"
STOCK_BUFFER_PERCENT: float = 0.10

KNOWN_PRODUCT_FIELDS = {
    "sku","article_number","artnr","article_no","id","product_id","supplier_product_id","external_id","external_matterhorn_id",
    "parent_sku","parent_id","master_sku","variant_sku","variant_id",
    "title","name","product_name","bezeichnung","short_description","description","raw_description","desc","beschreibung",
    "brand","manufacturer","hersteller","marke",
    "category","categories","kategorie","cat","category_path",
    "supplier_price","purchase_price","ek","ekp","ek_price","price","sale_price","retail_price","uvp","compare_at_price",
    "tax_rate","tax","mwst","currency",
    "stock","stock_quantity","quantity","qty","bestand","vorrat","available_quantity","reserved_quantity","reorder_level",
    "lead_time_days","supplier_stock","warehouse_location","bin_code","lagerort","lagerplatz",
    "ean","gtin","barcode","upc",
    "requires_serial_number","is_fragile","is_hazmat",
    "weight","weight_net_kg","weight_gross_kg","length","width","height","length_cm","width_cm","height_cm",
    "hs_code","country_of_origin","herkunftsland",
    "image","image_url","main_image_url","images","gallery","gallery_json","videos","documents",
    "seo_title","meta_description","slug","canonical_url",
    "status","source_hash","variants","attributes","properties","marketplace_mappings","marketplace_mappings_json",
}


async def _publish_import_event(event_type: str, payload: dict) -> None:
    try:
        from services import event_bus as _eb_mod
        bus = getattr(_eb_mod, "event_bus", None)
        if bus is not None and getattr(bus, "_running", False):
            await bus.publish(event_type=event_type, source_module="matterhorn_importer", payload=payload, target_module="*", entity_id=str(payload.get("run_id") or payload.get("preview_id") or ""))
    except Exception:
        pass


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", ".").strip())
    except Exception:
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(str(v).replace(",", ".").strip()))
    except Exception:
        return default


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1","true","yes","ja","y","on"}


def _first(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        # case-insensitive
        for dk, dv in d.items():
            if str(dk).lower() == k.lower() and dv not in (None, ""):
                return dv
    return default


def _flatten_xml_element(el: ET.Element) -> dict[str, Any]:
    data: dict[str, Any] = {}
    # attributes
    for k, v in (el.attrib or {}).items():
        data[f"@{k}"] = v
    children = list(el)
    if not children:
        text = (el.text or "").strip()
        return {"_text": text} if not el.attrib else {**data, "_text": text}
    for child in children:
        key = child.tag
        val: Any
        sub = _flatten_xml_element(child)
        if list(child):
            # complex child -> dict of its fields; also keep text map
            val = {k: v for k, v in sub.items() if k != "_text"} if any(k != "_text" for k in sub) else sub.get("_text", "")
        else:
            val = sub.get("_text", "") if isinstance(sub, dict) else sub
        if key in data:
            if not isinstance(data[key], list):
                data[key] = [data[key]]
            data[key].append(val)
        else:
            data[key] = val
    return data


def _parse_xml_lossless(text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(text)
    items = root.findall(".//product") or root.findall(".//item") or root.findall(".//offer") or root.findall(".//artikel") or list(root)
    out: list[dict[str, Any]] = []
    for item in items:
        if not list(item) and not (item.attrib or {}):
            continue
        flat = _flatten_xml_element(item)
        # normalize to simple dict where possible
        norm: dict[str, Any] = {}
        for k, v in flat.items():
            if k == "_text":
                continue
            norm[k] = v
        if norm:
            out.append(norm)
    return out


def _parse_csv_lossless(text: str) -> list[dict[str, Any]]:
    # try delimiters
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t,")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [dict(row) for row in reader if any((v or "").strip() for v in row.values())]


def _parse_json_lossless(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("products","items","data","offers","articles","rows"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # single object
        return [data]
    return []


def detect_format(text: str, filename: str | None = None) -> str:
    name = (filename or "").lower()
    s = text.lstrip('\ufeff \t\r\n')
    if name.endswith(".json") or s.startswith("{") or s.startswith("["):
        return "json"
    low = s[:500].lower()
    if low.startswith("<!doctype html") or low.startswith("<html") or "<html" in low:
        return "html"
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".xml") or s.startswith("<"):
        return "xml"
    # heuristic CSV
    first = s.splitlines()[0] if s.splitlines() else ""
    if "\n" in s and (";" in first or "," in first) and "<" not in s[:50]:
        return "csv"
    if s.startswith("<"):
        return "xml"
    return "xml"


def parse_lossless(text: str, filename: str | None = None, format_hint: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    stripped = (text or "").lstrip('\ufeff \t\r\n')
    if not stripped:
        raise ValueError("Feed ist leer (keine Daten empfangen). Bitte Quelle/Datei prüfen.")
    fmt = (format_hint or detect_format(text, filename) or "xml").lower()
    # hard guard: HTML/error pages are not valid feeds
    low = stripped[:500].lower()
    if fmt == "html" or low.startswith("<!doctype html") or low.startswith("<html") or "<html" in low:
        snippet = stripped[:120].replace("\n", " ").strip()
        raise ValueError(
            "Quelle liefert HTML statt XML/CSV/JSON (z. B. Login-/Fehlerseite oder falsche/geschützte URL). "
            f"Auszug: {snippet!r}"
        )
    if fmt == "json":
        return "json", _parse_json_lossless(text)
    if fmt == "csv":
        return "csv", _parse_csv_lossless(text)
    try:
        return "xml", _parse_xml_lossless(text)
    except ET.ParseError as exc:
        snippet = stripped[:120].replace("\n", " ").strip()
        raise ValueError(f"Feed ist kein gültiges XML/Format ({exc}). Auszug: {snippet!r}") from exc


def _normalize_product(raw: dict[str, Any], *, source_system: str = "matterhorn") -> dict[str, Any]:
    r = dict(raw)
    # lower-key lookup helper
    lower = {str(k).lower(): k for k in r.keys()}
    def get(*keys, default=""):
        v = _first(r, *keys, default=default)
        return v

    def _unwrap(v):
        # unwrap common CDATA/text containers from XML flattening
        if isinstance(v, dict):
            if set(v.keys()) <= {"_text",}:
                return v.get("_text", "")
            # prefer meaningful text if mixed
            if "_text" in v and len(v) == 1:
                return v.get("_text", "")
        return v

    def _as_text(v, default="") -> str:
        v = _unwrap(v)
        if v is None:
            return str(default)
        if isinstance(v, (str, int, float)):
            return str(v).strip()
        if isinstance(v, list):
            for it in v:
                t = _as_text(it, "")
                if t:
                    return t
            return str(default)
        if isinstance(v, dict):
            for k in ("_text","value","#text","text"):
                if k in v and v[k] not in (None, ""):
                    return str(v[k]).strip()
            # first scalar
            for it in v.values():
                t = _as_text(it, "")
                if t:
                    return t
        return str(v).strip() if v not in ("", None) else str(default)

    def _lower_map(obj: Any) -> dict[str, Any]:
        return {str(k).lower(): v for k, v in obj.items()} if isinstance(obj, dict) else {}

    sku = _as_text(get("sku","article_number","artnr","article_no","product_id","supplier_product_id","variant_sku","id","@id", default="")).strip()
    external_id = _as_text(get("external_matterhorn_id","external_id","supplier_product_id","product_id","id","@id", default=sku)).strip()
    if not sku and external_id:
        sku = external_id
    title = _as_text(get("title","name","product_name","bezeichnung", default="")).strip()
    description = _as_text(get("description","raw_description","desc","beschreibung", default=""))
    short_description = _as_text(get("short_description","kurzbeschreibung", default=""))

    category = get("category","kategorie","cat","category_path", default="")
    def _cat_collect(obj):
        out=[]
        if obj is None: return out
        obj=_unwrap(obj)
        if isinstance(obj, list):
            for it in obj: out.extend(_cat_collect(it))
        elif isinstance(obj, dict):
            # <category id='26'><![CDATA[..]]></category> -> {_text/name}
            for k in ("_text","name","title","path","category_path","#text","value"):
                if k in obj and obj[k] not in (None, ""):
                    out.extend(_cat_collect(obj[k]))
                    return out
            # attributes/nested
            for k,v in obj.items():
                if not str(k).startswith('@'):
                    out.extend(_cat_collect(v))
        elif isinstance(obj, str) and obj.strip():
            out.append(obj.strip())
        return out
    cat_items = _cat_collect(category)
    if cat_items:
        # if path-like string present, prefer its segments
        path_like = next((c for c in cat_items if "/" in c or ">" in c), None)
        if path_like:
            sep = "/" if "/" in path_like else ">"
            categories = [c.strip() for c in path_like.split(sep) if c.strip()]
        else:
            categories = cat_items
        category_str = categories[-1] if categories else ""
    else:
        categories = []
        category_str = ""
    extra_cats = get("categories", default=None)
    if isinstance(extra_cats, list) and extra_cats:
        categories = [_as_text(c) for c in extra_cats if _as_text(c)]

    # prices: support <prices><price currency="EUR">..</price></prices> or simple fields
    supplier_price = _to_float(get("supplier_price","purchase_price","ek","ekp","ek_price", default=0), 0.0)
    sale_price = _to_float(get("sale_price","retail_price","uvp","vk", default=0), 0.0)
    currency = _as_text(get("currency", default="EUR") or "EUR")
    prices_raw = get("prices","price", default=None)
    def _price_collect(obj):
        items=[]
        if obj is None: return items
        if isinstance(obj, list):
            for it in obj: items.extend(_price_collect(it))
        elif isinstance(obj, dict):
            lk = _lower_map(obj)
            if "price" in lk and ("currency" not in lk and "@currency" not in lk):
                items.extend(_price_collect(lk.get("price")))
            elif "currency" in lk or "@currency" in lk or "_text" in lk or "value" in lk or "#text" in lk:
                cur = _as_text(obj.get("@currency") or obj.get("currency") or currency or "EUR")
                amt = _as_text(obj.get("_text") or obj.get("value") or obj.get("#text") or obj.get("amount") or 0)
                if amt != "":
                    items.append((cur.upper(), _to_float(amt, 0.0)))
            else:
                # maybe {"EUR": 1.2} or nested
                scalars = {k:v for k,v in obj.items() if not isinstance(v,(dict,list))}
                nonscal = {k:v for k,v in obj.items() if isinstance(v,(dict,list))}
                if scalars and not nonscal:
                    for k,v in scalars.items():
                        if str(k).startswith('@'): continue
                        if str(k).lower() in {"type","kind","name"}: continue
                        items.append((str(k).upper(), _to_float(v,0.0)))
                for v in obj.values(): items.extend(_price_collect(v))
        elif isinstance(obj, (str,int,float)) and str(obj).strip() != "":
            items.append((currency.upper(), _to_float(obj, 0.0)))
        return items
    pitems = _price_collect(prices_raw)
    if pitems:
        chosen = next((amt for cur,amt in pitems if cur=="EUR" and amt>0), None)
        if chosen is None:
            chosen = next((amt for _,amt in pitems if amt>0), pitems[0][1])
        if chosen and chosen>0:
            if sale_price <= 0: sale_price = chosen
            if supplier_price <= 0: supplier_price = chosen
        if sale_price <= 0:
            simple = _to_float(get("price", default=0), 0.0)
            if simple>0: sale_price = simple
    else:
        simple = _to_float(get("price", default=0), 0.0)
        if simple>0 and sale_price <= 0: sale_price = simple
        if simple>0 and supplier_price <= 0: supplier_price = simple

    raw_stock = _to_int(get("stock","stock_quantity","quantity","qty","bestand","vorrat","supplier_stock", default=0), 0)
    stock_quantity = _to_int(get("stock_quantity","stock","quantity","qty","bestand","vorrat", default=raw_stock), raw_stock)
    ean = _as_text(get("ean","gtin","barcode","upc", default="")).strip()

    images_raw = get("images","gallery","gallery_json","image_urls", default=None)
    images: list[Any] = []
    main_image = _as_text(get("main_image_url","image_url","image","img","bild","picture", default="")).strip()
    if main_image.lower() in {"none","null"}: main_image = ""
    def _collect_media(obj):
        if obj is None: return
        if isinstance(obj, list):
            for it in obj: _collect_media(it)
        elif isinstance(obj, dict):
            lk = _lower_map(obj)
            # direct url holders
            for key in ("image_url","url","src","image","bild","picture","_text","value","#text"):
                if key in lk and lk[key] not in (None, ""):
                    val = lk[key]
                    if isinstance(val, (list,dict)):
                        _collect_media(val)
                    elif str(val).strip():
                        images.append(str(val).strip())
                    # if we consumed image_url/src-like, don't recurse same scalar-only container redundantly
                    if key in ("image_url","url","src","image","bild","picture"):
                        return
            # wrapper containers
            for k,v in obj.items():
                if str(k).startswith('@'): continue
                _collect_media(v)
        elif isinstance(obj, str) and obj.strip():
            for sep in [",","|",";","\n"]:
                if sep in obj:
                    images.extend([x.strip() for x in obj.split(sep) if x.strip()])
                    return
            images.append(obj.strip())
    _collect_media(images_raw)
    # dedupe preserve order
    seen_img=set(); norm_imgs=[]
    for im in images:
        u = str(im.get("url") if isinstance(im, dict) else im).strip()
        if not u or u.lower() in {"none","null"}: continue
        if u in seen_img: continue
        seen_img.add(u); norm_imgs.append(u)
    images = norm_imgs
    if main_image and main_image not in images:
        images = [main_image] + images
    if not main_image and images:
        main_image = str(images[0].get("url") if isinstance(images[0], dict) else images[0])

    variants_raw = get("variants","varianten","options","optionen", default=[])
    variants: list[dict[str, Any]] = []
    def _collect_vars(obj):
        if obj is None: return
        if isinstance(obj, list):
            for it in obj: _collect_vars(it)
        elif isinstance(obj, dict):
            keys_lower = {str(k).lower() for k in obj.keys()}
            # wrapper like {"option":[...]} / {"variant":[...]}
            if keys_lower & {"variant","item","row","productvariant","option"} and not ({"sku","variant_sku","ean","stock","size","color","option_name","@id"} & keys_lower):
                for v in obj.values(): _collect_vars(v)
            else:
                variants.append(obj)
        # ignore scalars
    _collect_vars(variants_raw)

    norm_variants = []
    if variants:
        for idx, v in enumerate(variants, start=1):
            if not isinstance(v, dict): continue
            vl = _lower_map(v)
            v_id = _as_text(v.get("@id") or v.get("id") or v.get("external_id") or v.get("variant_id") or "")
            v_sku = _as_text(v.get("sku") or v.get("variant_sku") or v.get("article_number") or v.get("artnr") or v_id or (f"{sku}-{idx}" if sku else ""))
            opt_name = v.get("option_name") or v.get("name") or {}
            opt_attrs = {}
            if isinstance(opt_name, dict):
                for ak,av in opt_name.items():
                    if str(ak).startswith('@'):
                        opt_attrs[str(ak)[1:]] = _as_text(av)
                    elif str(ak).lower() in {"_text","value","#text"} and _as_text(av):
                        opt_attrs["name"] = _as_text(av)
            elif _as_text(opt_name):
                opt_attrs["name"] = _as_text(opt_name)
            # extra size-like attrs
            for ak in ("size","groesse","color","farbe","length","width"):
                if ak in vl and _as_text(vl[ak]):
                    opt_attrs.setdefault(ak, _as_text(vl[ak]))
            v_stock = _to_int(v.get("STOCK") or v.get("stock") or v.get("stock_quantity") or v.get("qty") or v.get("quantity") or 0, 0)
            v_ean = _as_text(v.get("ean") or v.get("gtin") or v.get("barcode") or "")
            v_price = _to_float(v.get("price") or v.get("supplier_price") or v.get("sale_price") or 0, 0.0)
            if v_price <= 0: v_price = sale_price or supplier_price
            norm_variants.append({
                "sku": v_sku,
                "external_matterhorn_id": v_id or v_sku,
                "title": opt_attrs.get("name") or title,
                "ean": v_ean,
                "barcode": v_ean,
                "supplier_price": v_price,
                "sale_price": v_price,
                "stock_quantity": v_stock,
                "attributes_json": opt_attrs,
                "raw_source_data_json": v,
                "status": "active",
            })
        variants = norm_variants
    else:
        variants = []

    # derive stock from variants if missing
    if stock_quantity <= 0 and variants:
        stock_quantity = sum(_to_int(v.get("stock_quantity",0),0) for v in variants)
        raw_stock = stock_quantity

    # derive ean from first variant if missing
    if not ean and variants:
        ean = _as_text(variants[0].get("ean") or "")

    attrs_in = get("attributes","properties","eigenschaften","merkmale", default=[])
    attributes: list[dict[str, Any]] = []
    if isinstance(attrs_in, dict):
        attributes = [{"name": k, "value": v} for k, v in attrs_in.items()]
    elif isinstance(attrs_in, list):
        for a in attrs_in:
            if isinstance(a, dict):
                attributes.append({"name": a.get("name") or a.get("key") or a.get("attribute"), "value": a.get("value") or a.get("val"), **{k:v for k,v in a.items() if k not in {"name","key","attribute","value","val"}}})
            elif isinstance(a, (list, tuple)) and len(a)>=2:
                attributes.append({"name": a[0], "value": a[1]})

    # dynamic/unmapped attributes (lossless capture)
    for k, v in r.items():
        lk = str(k).lower()
        if lk in {x.lower() for x in KNOWN_PRODUCT_FIELDS} | {"@id", "prices", "images", "options", "optionen"}:
            continue
        if isinstance(v, (dict, list)):
            attributes.append({"name": str(k), "value": json.dumps(v, ensure_ascii=False, default=str), "source_field": str(k), "extra_json": {"complex": True}})
        else:
            attributes.append({"name": str(k), "value": "" if v is None else str(v), "source_field": str(k), "is_filterable": True})

    product = {
        "sku": sku,
        "external_matterhorn_id": external_id or sku,
        "parent_sku": str(get("parent_sku","parent_id","master_sku", default="")).strip(),
        "title": title or sku,
        "brand": str(get("brand","marke", default="")),
        "manufacturer": str(get("manufacturer","hersteller", default="")),
        "category": category_str,
        "categories": categories,
        "short_description": short_description,
        "raw_description": description,
        "supplier_price": supplier_price,
        "purchase_price": supplier_price,
        "sale_price": sale_price,
        "compare_at_price": _to_float(get("compare_at_price","uvp", default=0), 0.0),
        "tax_rate": _to_float(get("tax_rate","tax","mwst", default=0), 0.0),
        "currency": str(get("currency", default="EUR") or "EUR"),
        "stock": stock_quantity,
        "stock_quantity": stock_quantity,
        "supplier_stock": raw_stock,
        "available_quantity": stock_quantity,
        "reserved_quantity": _to_int(get("reserved_quantity", default=0), 0),
        "reorder_level": _to_int(get("reorder_level", default=0), 0),
        "lead_time_days": _to_int(get("lead_time_days", default=0), 0),
        "warehouse_location": str(get("warehouse_location","lagerort", default="")),
        "bin_code": str(get("bin_code","lagerplatz", default="")),
        "ean": ean,
        "barcode": str(get("barcode", default=ean) or ean),
        "requires_serial_number": _to_bool(get("requires_serial_number", default=False)),
        "is_fragile": _to_bool(get("is_fragile", default=False)),
        "is_hazmat": _to_bool(get("is_hazmat", default=False)),
        "weight_net_kg": _to_float(get("weight_net_kg","weight", default=0), 0.0),
        "weight_gross_kg": _to_float(get("weight_gross_kg", default=0), 0.0),
        "length_cm": _to_float(get("length_cm","length", default=0), 0.0),
        "width_cm": _to_float(get("width_cm","width", default=0), 0.0),
        "height_cm": _to_float(get("height_cm","height", default=0), 0.0),
        "hs_code": str(get("hs_code", default="")),
        "country_of_origin": str(get("country_of_origin","herkunftsland", default="")),
        "main_image_url": "" if str(main_image).lower() in {"none","null"} else (main_image or (str(images[0].get("url") if isinstance(images[0], dict) else images[0]) if images else "")),
        "gallery_json": images,
        "videos_json": (lambda v: v if isinstance(v,list) else ([v] if isinstance(v,dict) and v else list(v.values()) if isinstance(v,dict) else ([] if not v else [v])))(get("videos", default=[])),
        "documents_json": (lambda v: v if isinstance(v,list) else ([v] if isinstance(v,dict) and v else list(v.values()) if isinstance(v,dict) else ([] if not v else [v])))(get("documents", default=[])),
        "seo_title": str(get("seo_title", default="")),
        "meta_description": str(get("meta_description", default="")),
        "slug": str(get("slug", default="")),
        "canonical_url": str(get("canonical_url", default="")),
        "marketplace_mappings_json": get("marketplace_mappings_json","marketplace_mappings", default={}),
        "source_system": source_system,
        "raw_source_data_json": raw,
        "variants": variants,
        "attributes": [a for a in attributes if str(a.get("name") or "").strip()],
        "status": str(get("status", default="active") or "active"),
    }
    return product


def _validate_products(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_skus: set[str] = set()
    valid = 0
    for idx, p in enumerate(products):
        sku = str(p.get("sku") or "").strip()
        title = str(p.get("title") or "").strip()
        price = _to_float(p.get("supplier_price", p.get("price", 0)), 0.0)
        stock = _to_int(p.get("stock_quantity", p.get("stock", 0)), 0)
        ean = str(p.get("ean") or "").strip()
        row_err = []
        if not sku: row_err.append("missing_sku")
        if not title: row_err.append("missing_title")
        if price < 0: row_err.append("negative_price")
        if stock < 0: row_err.append("negative_stock")
        if sku and sku in seen_skus: row_err.append("duplicate_sku")
        if sku: seen_skus.add(sku)
        if row_err:
            errors.append({"index": idx, "sku": sku, "issues": row_err})
        else:
            valid += 1
        if ean and not (8 <= len(ean) <= 14 and ean.isdigit()):
            warnings.append({"index": idx, "sku": sku, "issues": ["ean_format_suspicious"], "ean": ean})
        if price == 0:
            warnings.append({"index": idx, "sku": sku, "issues": ["zero_or_missing_price"]})
    status = "ok" if not errors else ("partial" if valid > 0 else "invalid")
    return {"status": status, "total": len(products), "valid": valid, "invalid": len(errors), "error_count": len(errors), "warning_count": len(warnings), "errors": errors[:200], "warnings": warnings[:200]}


class MatterhornUniversalImporter:
    """Importer for Matterhorn/supplier feeds with preview/import modes."""

    def __init__(self, api_token: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> None:
        self.api_token: Optional[str] = api_token
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def _resolve_feed_url(self, feed_url: str | None = None) -> str:
        if feed_url:
            return feed_url
        cfg_url = await get_setting("matterhorn_feed_url")
        if cfg_url:
            return cfg_url
        try:
            feeds = await matterhorn_list_feeds()
            for f in feeds:
                enabled = f.get("enabled", True)
                if isinstance(enabled, str):
                    enabled = enabled.lower() in ("1", "true", "yes")
                if enabled and f.get("feed_url"):
                    return str(f.get("feed_url"))
        except Exception:
            pass
        return MATTERHORN_XML_FEED

    async def _load_source(self, *, feed_url: str | None = None, content: str | bytes | None = None, filename: str | None = None) -> tuple[str, str, str]:
        """Return (text, resolved_source, filename)."""
        if content is not None:
            if isinstance(content, bytes):
                text = content.decode("utf-8-sig", errors="replace")
            else:
                text = str(content)
            return text, (feed_url or filename or "upload"), (filename or "upload")
        resolved = await self._resolve_feed_url(feed_url)
        # local file path support
        if resolved and not resolved.lower().startswith(("http://","https://")) and os.path.exists(resolved):
            with open(resolved, "r", encoding="utf-8-sig", errors="replace") as f:
                return f.read(), resolved, os.path.basename(resolved)
        headers = {"User-Agent": "OMNI-CORE Matterhorn Importer/1.0", "Accept": "application/xml,text/xml,application/json,text/csv,*/*"}
        resp = await self.client.get(resolved, timeout=30.0, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        text = resp.text or ""
        if not text.strip():
            ct = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feed-Antwort ist leer (HTTP {resp.status_code}, Content-Type: {ct or 'unbekannt'}, "
                f"Bytes: {len(resp.content)}). Die Quelle liefert ohne gültige Zugangsdaten/Session ggf. keine Daten "
                f"oder eine leere Seite. Bitte Feed-URL/Parameter/Zugang prüfen."
            )
        return text, resolved, (filename or resolved.rsplit("/",1)[-1] or "feed")

    async def fetch_products(self, feed_url: str | None = None, prefer: str = "xml", *, content: str | bytes | None = None, filename: str | None = None, format_hint: str | None = None) -> List[Dict[str, Any]]:
        text, resolved, fname = await self._load_source(feed_url=feed_url, content=content, filename=filename)
        fmt, rows = parse_lossless(text, fname, format_hint or prefer)
        normalized = []
        for row in rows:
            p = _normalize_product(row, source_system="matterhorn")
            p["_source_format"] = fmt
            p["_source_ref"] = resolved
            if p.get("sku") or p.get("title"):
                normalized.append(p)
        return normalized

    async def preview_products(self, *, feed_url: str | None = None, save: bool = True, limit: int = 50, content: str | bytes | None = None, filename: str | None = None, format_hint: str | None = None) -> Dict[str, Any]:
        try:
            text, resolved_url, fname = await self._load_source(feed_url=feed_url, content=content, filename=filename)
            fmt, rows = parse_lossless(text, fname, format_hint)
            products = []
            for row in rows:
                p = _normalize_product(row, source_system="matterhorn")
                p["_source_format"] = fmt
                if p.get("sku") or p.get("title"):
                    products.append(p)
            validation = _validate_products(products)
            src_material = f"{resolved_url}|{fmt}|{len(products)}|" + "|".join([str(p.get('sku',''))+':'+str(p.get('supplier_price',''))+':'+str(p.get('stock_quantity','')) for p in products[:200]])
            source_hash = hashlib.sha256(src_material.encode("utf-8", errors="ignore")).hexdigest()
            summary = {
                "feed_url": resolved_url,
                "filename": fname,
                "format": fmt,
                "total": len(products),
                "valid": validation.get("valid", 0),
                "invalid": validation.get("invalid", 0),
                "variants_detected": sum(len(p.get("variants") or []) for p in products),
                "dynamic_attributes_detected": sum(len(p.get("attributes") or []) for p in products),
                "sample_count": min(limit, len(products)),
            }
            preview_id = str(uuid.uuid4())
            sample = products[: max(0, min(limit, 200))]
            if save:
                await matterhorn_save_preview(preview_id=preview_id, feed_url=resolved_url, status=validation.get("status", "ok"), summary=summary, items=sample, validation=validation, source_hash=source_hash)
            await write_log("INFO", "matterhorn_preview", f"Preview generated: {len(products)} items ({validation.get('status')})", {"feed_url": resolved_url, "preview_id": preview_id, "format": fmt})
            await _publish_import_event("matterhorn.preview_completed", {"preview_id": preview_id, "feed_url": resolved_url, "summary": summary, "validation": validation})
            return {"status": "ok", "preview_id": preview_id if save else None, "feed_url": resolved_url, "summary": summary, "validation": validation, "items": sample, "source_hash": source_hash}
        except Exception as exc:
            logger.exception("Matterhorn preview failed")
            emsg = str(exc) or exc.__class__.__name__
            await write_log("ERROR", "matterhorn_preview_failed", f"Preview failed: {emsg}", {"feed_url": feed_url or filename or ""})
            await _publish_import_event("matterhorn.preview_failed", {"feed_url": feed_url or filename or "", "error": emsg})
            return {"status": "error", "feed_url": feed_url or filename or "", "error": emsg, "summary": {}, "validation": {"status": "invalid", "errors": [{"issues": ["fetch_or_parse_failed"], "message": emsg}]}, "items": []}

    async def import_products(self, source: str = "matterhorn_xml", *, feed_url: str | None = None, mode: str = "safe", dry_run: bool = False, preview_id: str | None = None, confirm_full_without_preview: bool = False, sync_rules: dict[str, Any] | None = None, content: str | bytes | None = None, filename: str | None = None, format_hint: str | None = None) -> Dict[str, Any]:
        mode = (mode or "safe").lower()
        resolved_source = feed_url or filename or "upload"
        run_id = await start_import_run(source, mode=mode, feed_url=str(resolved_source))
        imported = updated = errors = skipped = created_count = updated_count = variants_linked = 0
        category_links = 0
        issues: list[dict[str, Any]] = []
        try:
            products = await self.fetch_products(feed_url=feed_url, content=content, filename=filename, format_hint=format_hint)
            validation = _validate_products(products)
            if mode == "full" and not preview_id and not confirm_full_without_preview and not dry_run:
                protocol = {"reason": "preview_confirmation_required", "validation": validation}
                await finish_import_run(run_id, imported=0, updated=0, errors=1, error_message="Full import requires preview confirmation", status="blocked", summary={"total": len(products)}, protocol=protocol)
                return {"status": "blocked", "reason": "preview_confirmation_required", "message": "Full-Import ohne Preview erfordert confirm_full_without_preview=true oder vorherige Preview.", "validation": validation, "run_id": run_id}
            if validation.get("status") == "invalid" and mode != "dry_run":
                protocol = {"reason": "validation_failed", "validation": validation}
                await finish_import_run(run_id, imported=0, updated=0, errors=validation.get("error_count", 0), error_message="Validation failed", status="failed", summary={"total": len(products)}, protocol=protocol)
                return {"status": "failed", "reason": "validation_failed", "validation": validation, "imported": 0, "updated": 0, "errors": validation.get("error_count", 0), "run_id": run_id}
            if dry_run or mode == "dry_run":
                summary = {"total": len(products), "valid": validation.get("valid", 0), "invalid": validation.get("invalid", 0), "variants_detected": sum(len(p.get("variants") or []) for p in products), "dynamic_attributes_detected": sum(len(p.get("attributes") or []) for p in products)}
                await finish_import_run(run_id, imported=0, updated=0, errors=0, error_message="dry_run", status="dry_run", summary=summary, protocol={"validation": validation, "dry_run": True})
                return {"status": "dry_run", "imported": 0, "updated": 0, "created": 0, "errors": 0, "skipped": 0, "variants_linked": 0, "validation": validation, "total": len(products), "summary": summary, "protocol": {"validation": validation, "issues": []}, "preview_id": preview_id, "run_id": run_id}

            invalid_skus = {e.get("sku") for e in (validation.get("errors") or []) if e.get("sku")}
            eff_rules = {"mode": "full" if mode in ("full",) else ("stock_only" if mode == "stock_only" else ("ek_only" if mode == "ek_only" else "full")), "preserve_manual_seo": True, "preserve_marketplace_mappings": True}
            if isinstance(sync_rules, dict):
                eff_rules.update(sync_rules)
            # map UI/import modes: safe/partial behave like full but skip invalid
            for product in products:
                sku = str(product.get("sku") or "").strip()
                try:
                    if not sku:
                        skipped += 1; issues.append({"sku": sku, "reason": "missing_sku"}); continue
                    if mode in ("safe", "partial") and sku in invalid_skus:
                        skipped += 1; issues.append({"sku": sku, "reason": "validation_error_skipped", "issues": [e for e in validation.get("errors", []) if e.get("sku")==sku]}); continue
                    raw_stock = _to_int(product.get("supplier_stock", product.get("stock_quantity", product.get("stock", 0))), 0)
                    buffered_stock = max(0, int(raw_stock * (1 - STOCK_BUFFER_PERCENT)))
                    # preserve lossless raw but store effective buffered stock
                    product_payload = dict(product)
                    product_payload["stock"] = buffered_stock
                    product_payload["stock_quantity"] = buffered_stock
                    product_payload["source_system"] = str(product_payload.get("source_system") or "matterhorn")
                    src_hash = hashlib.sha256(json.dumps({"sku": sku, "ext": product_payload.get("external_matterhorn_id"), "title": product_payload.get("title"), "price": product_payload.get("supplier_price"), "sale": product_payload.get("sale_price"), "stock": raw_stock, "ean": product_payload.get("ean"), "cat": product_payload.get("category"), "attrs": product_payload.get("attributes"), "vars": product_payload.get("variants"), "log": {k: product_payload.get(k) for k in ("warehouse_location","bin_code","hs_code","country_of_origin","weight_net_kg","weight_gross_kg","length_cm","width_cm","height_cm","lead_time_days","reorder_level")}}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
                    product_payload["source_hash"] = src_hash
                    res = await upsert_product_aggregate(product_payload, sync_rules=eff_rules)
                    imported += 1
                    if res.get("changed"):
                        updated += 1; updated_count += 1
                    else:
                        # new or unchanged; treat new insert as created best-effort not directly known except changed flag; count imported regardless
                        created_count += 1 if not res.get("changed") else 0
                    variants_linked += int(res.get("variants_linked") or 0)
                    category_links += int(res.get("categories_linked") or 0)
                except Exception as exc:
                    errors += 1
                    issues.append({"sku": sku, "reason": "import_failed", "message": str(exc)})
                    logger.exception("Error importing product %s", sku or "?")

            await refresh_all_bundle_stocks()
            run_status = "success" if errors == 0 else "failed"
            summary = {"total": len(products), "imported": imported, "created": max(imported - updated_count, 0), "updated": updated_count, "errors": errors, "skipped": skipped, "variants_linked": variants_linked, "categories_linked": category_links}
            protocol = {"validation": validation, "issues": issues[:500], "sync_rules": eff_rules, "source": resolved_source}
            await finish_import_run(run_id, imported=imported, updated=updated_count, errors=errors, skipped=skipped, created_count=max(imported-updated_count,0), updated_count=updated_count, variants_linked=variants_linked, status=run_status, summary=summary, protocol=protocol, error_message="" if errors==0 else f"{errors} Produkte mit Fehlern")
            await write_log("INFO" if errors == 0 else "WARNING", "matterhorn_import_complete", f"Matterhorn import finished: {imported} products, {updated_count} changed, {errors} errors, {skipped} skipped (mode={mode})", {"run_id": run_id, "mode": mode, **summary})
            await _publish_import_event("matterhorn.import_completed", {"run_id": run_id, **summary, "mode": mode, "status": run_status})
            return {"imported": imported, "updated": updated_count, "created": max(imported-updated_count,0), "errors": errors, "skipped": skipped, "variants_linked": variants_linked, "categories_linked": category_links, "status": run_status, "mode": mode, "validation": validation, "summary": summary, "protocol": protocol, "run_id": run_id}
        except Exception as exc:
            logger.exception("Matterhorn import run failed: %s", exc)
            await write_log("ERROR", "matterhorn_import_failed", f"Matterhorn import failed: {exc}", {"run_id": run_id})
            await finish_import_run(run_id, imported=imported, updated=updated_count, errors=errors or 1, error_message=str(exc), status="failed", skipped=skipped, variants_linked=variants_linked, summary={"imported": imported, "errors": errors or 1}, protocol={"error": str(exc), "issues": issues[:200]})
            await _publish_import_event("matterhorn.import_failed", {"run_id": run_id, "error": str(exc)})
            return {"imported": imported, "updated": updated_count, "errors": errors or 1, "skipped": skipped, "status": "failed", "error": str(exc), "run_id": run_id}
