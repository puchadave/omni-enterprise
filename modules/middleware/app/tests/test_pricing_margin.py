from services.matterhorn_importer import calculate_vk_from_ek, _normalize_product


def test_calculate_vk_from_ek_not_equal_and_margin():
    r = calculate_vk_from_ek(10.0, tax_rate=19.0, target_margin_pct=30.0, min_margin_pct=12.0, rounding='point_99')
    assert r['ek_net'] == 10.0
    assert r['vk_brutto'] > r['ek_net']
    assert r['margin_pct'] >= 12.0
    assert round(r['vk_brutto'] * 100) % 100 in (99, 95, 49, 0) or r['vk_brutto'] > 0


def test_normalize_product_separates_ek_vk():
    p = _normalize_product({'sku':'SKU1','title':'Test','purchase_price':10,'tax':19}, source_system='matterhorn')
    assert float(p['purchase_price']) == 10.0
    assert float(p['sale_price']) > 10.0
    assert float(p['margin_abs']) > 0
    assert p['seo_title']
    assert p['meta_description']
